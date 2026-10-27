package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"guardiand/config"
)

func TestStartupMissedIsBatched(t *testing.T) {
	c := config.Config{Events: []config.Event{
		{ID: "a", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:01:00"}},
		{ID: "b", Trigger: config.Trigger{Type: "window", Start: "09:10:00", Deadline: "09:11:00"}},
	}}
	date, _ := time.Parse("2006-01-02 15:04:05", "2026-10-20 10:34:00")
	missed, active := startupClassify(c, date)
	if len(missed) != 2 || len(active) != 0 {
		t.Fatalf("startup classification missed=%d active=%d", len(missed), len(active))
	}
}

func TestPhaseAtUsesInjectedFakeClock(t *testing.T) {
	fc := newFakeClock(time.Date(2026, 10, 20, 9, 20, 0, 0, time.FixedZone("CST", 8*3600)))
	phases := config.DefaultPhases()
	if got, _ := phaseIn(fc.Now(), phases, true); got != "AUCTION" {
		t.Fatalf("phase=%s", got)
	}
	fc.Advance(4 * time.Hour)
	if got, _ := phaseIn(fc.Now(), phases, true); got != "INTRADAY_PM" {
		t.Fatalf("phase after advance=%s", got)
	}
	if got, _ := phaseIn(fc.Now(), phases, false); got != "NON_TRADING" {
		t.Fatalf("non-trading phase=%s", got)
	}
}

func TestHeavyPreMarketCutoffUsesConfiguredTime(t *testing.T) {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	event := &config.Event{ID: "fetch_item", Cost: "heavy", Phases: []string{"PRE_MARKET", "MIDDAY_BREAK"}}
	c := config.Config{HeavyCutoffs: map[string]string{"PRE_MARKET": "09:10:00"}}
	before := time.Date(2026, 10, 20, 9, 9, 59, 0, loc)
	after := before.Add(time.Second)
	if !phaseAllowedAt(event, "PRE_MARKET", before, c, loc) {
		t.Fatal("heavy item rejected before cutoff")
	}
	if phaseAllowedAt(event, "PRE_MARKET", after, c, loc) {
		t.Fatal("heavy item allowed at cutoff")
	}
}

func TestDeferredAndQueuedFetchRetainsRetryPartitionAndAttempts(t *testing.T) {
	root, date := t.TempDir(), "2026-10-21"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	input := filepath.Join(root, "input.jsonl")
	script := filepath.Join(root, "fetch.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\ncat >> '"+input+"'\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	e := config.Event{ID: "fetch_item", Handler: "h", Cost: "heavy", Trigger: config.Trigger{Type: "startup"}, Phases: []string{"MIDDAY_BREAK"}, OnPhaseMismatch: "defer", Retry: config.Retry{MaxAttempts: 3}}
	calendar := "/home/exedev/core-data/trade_dates.json"
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}, Calendars: map[string]struct {
		File string `json:"file"`
	}{"trading": {File: calendar}}}, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	id := "fetch_item:daily_kline"
	r.setState(Instance{ID: id, EventID: e.ID, TriggerType: "startup"}, date, instanceState{Status: "RETRY_WAIT", Attempts: 1, NextAttemptAt: now.Format(time.RFC3339), DataDate: "2026-10-20"})
	r.execute(Instance{ID: id, EventID: e.ID, TriggerType: "startup", DataDate: "2026-10-20"}, date)
	deferred := runtimeTestState(r, date+"/"+id)
	if deferred.Status != "DEFERRED" || deferred.Attempts != 1 || deferred.DataDate != "2026-10-20" || deferred.NextAttemptAt == "" {
		t.Fatalf("deferred state lost retry metadata: %+v", deferred)
	}
	fc.Advance(2 * time.Hour)
	r.mu.Lock()
	r.heavyRunning = true
	r.mu.Unlock()
	done := make(chan struct{})
	go func() {
		r.execute(Instance{ID: id, EventID: e.ID, TriggerType: "startup", DataDate: "2026-10-20"}, date)
		close(done)
	}()
	waitFor(t, func() bool { return runtimeTestState(r, date+"/"+id).Status == "QUEUED_HEAVY" })
	queued := runtimeTestState(r, date+"/"+id)
	if queued.Status != "QUEUED_HEAVY" || queued.Attempts != 1 || queued.DataDate != "2026-10-20" || queued.NextAttemptAt == "" {
		t.Fatalf("queued state lost retry metadata: %+v", queued)
	}
	r.mu.Lock()
	r.heavyRunning = false
	r.signalHeavyChangedLocked()
	r.mu.Unlock()
	<-done
	got := runtimeTestState(r, date+"/"+id)
	if got.Status != "SUCCEEDED" || got.Attempts != 2 || got.DataDate != "2026-10-20" {
		t.Fatalf("released state=%+v", got)
	}
	b, err := os.ReadFile(input)
	if err != nil {
		t.Fatal(err)
	}
	var in map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(b), &in); err != nil {
		t.Fatal(err)
	}
	if in["attempt"] != float64(2) || in["data_date"] != "2026-10-20" {
		t.Fatalf("redispatch input=%v", in)
	}
}

func TestInvokeUsesTradingCalendarForPreviousTradeDate(t *testing.T) {
	root, date := t.TempDir(), "2026-10-26"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 08:00:00", loc)
	fc := newFakeClock(now)
	calendar := filepath.Join(root, "calendar.json")
	if err := os.WriteFile(calendar, []byte(`["2026-10-23","2026-10-26","2026-10-27"]`), 0600); err != nil {
		t.Fatal(err)
	}
	input := filepath.Join(root, "input.json")
	handler := filepath.Join(root, "handler.sh")
	script := "#!/bin/sh\ncat > '" + input + "'\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"
	if err := os.WriteFile(handler, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	e := config.Event{ID: "daily_kline_backfill", Handler: "h", Timeout: "3s", Args: map[string]any{"target": "prev_trade_day"}, Trigger: config.Trigger{Type: "window", Start: "01:00:00", Deadline: "01:05:00"}}
	c := config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{handler}}}, Calendars: map[string]struct {
		File string `json:"file"`
	}{"trading": {File: calendar}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l,
		states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	if _, _, err := r.invokeDetailedWithDegrade([]string{handler}, &e, Instance{ID: e.ID, EventID: e.ID, Start: "01:00:00", Deadline: "01:05:00"}, date, 1, false, false); err != nil {
		t.Fatalf("invoke: %v", err)
	}
	b, err := os.ReadFile(input)
	if err != nil {
		t.Fatal(err)
	}
	var request map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(b), &request); err != nil {
		t.Fatal(err)
	}
	if request["trade_date"] != date || request["data_date"] != "2026-10-23" {
		t.Fatalf("request dates trade_date=%v data_date=%v; want %s and 2026-10-23", request["trade_date"], request["data_date"], date)
	}
}

func TestCalendarRecoveryRerunsFailedBootChainWithFakeClock(t *testing.T) {
	root, date := t.TempDir(), "2026-10-21"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now := time.Date(2026, 10, 20, 8, 30, 0, 0, loc)
	fc := newFakeClock(now)
	calendar := filepath.Join(root, "calendar.json")
	tradeDay := filepath.Join(root, "trade_day.sh")
	health := filepath.Join(root, "health.sh")
	if err := os.WriteFile(tradeDay, []byte("#!/bin/sh\nif [ ! -f \""+calendar+"\" ]; then printf '%s\\n' '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"CALENDAR_UNKNOWN\",\"retryable\":true}'; exit 0; fi\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(health, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	c := config.Config{ConfigVersion: 1, Timezone: "Asia/Shanghai",
		Calendars: map[string]struct {
			File string `json:"file"`
		}{"trading": {File: calendar}},
		Handlers: map[string]config.Handler{"trade_day": {Argv: []string{tradeDay}}, "health": {Argv: []string{health}}},
		Events: []config.Event{
			{ID: "boot_trade_day", Trigger: config.Trigger{Type: "startup", Order: 10}, Handler: "trade_day"},
			{ID: "boot_data_health", Trigger: config.Trigger{Type: "startup", Order: 20, After: []string{"boot_trade_day"}}, Handler: "health"},
		},
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{},
		running: map[string]bool{}, paused: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	if err := r.runBootEvents(now); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	initial := r.bootSummary["boot_trade_day"]
	r.mu.Unlock()
	if initial != "FAILED_FINAL" {
		t.Fatalf("initial boot status=%s", initial)
	}
	if err := os.WriteFile(calendar, []byte(`["2026-10-20"]`), 0600); err != nil {
		t.Fatal(err)
	}
	r.scheduleBootRerun(date)
	waitFor(t, func() bool { return fc.waitCount() > 0 })
	fc.Advance(time.Minute)
	waitFor(t, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.bootSummary["boot_trade_day"] == "SUCCEEDED" && r.bootSummary["boot_data_health"] == "SUCCEEDED"
	})
	waitFor(t, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return !r.running["boot_trade_day@boot"+r.bootSequence] && !r.running["boot_data_health@boot"+r.bootSequence]
	})
	if r.bootSequence == "" || r.bootReruns != 1 {
		t.Fatalf("rerun identity/count sequence=%q reruns=%d", r.bootSequence, r.bootReruns)
	}
	b, err := os.ReadFile(filepath.Join(root, "ledger", date+".jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), `"type":"BOOT_RERUN"`) {
		t.Fatalf("BOOT_RERUN missing from ledger: %s", b)
	}
}

func TestScheduleDefersPhaseMismatchUntilWhitelistedPhase(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	calendar := filepath.Join(root, "calendar.json")
	if err := os.WriteFile(calendar, []byte(`["2026-10-20"]`), 0600); err != nil {
		t.Fatal(err)
	}
	script := filepath.Join(root, "ok.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	decoded, issues := config.Decode([]byte(`{"schema_version":1,"config_version":1,"name":"test","timezone":"Asia/Shanghai","events":[{"id":"heavy","trigger":{"type":"window","start":"09:00:00","deadline":"09:10:00"},"late_until":"13:00:00","catchup":"run_in_window","phases":["MIDDAY_BREAK"],"on_phase_mismatch":"defer","on_start":"reconcile","handler":"h"}],"handlers":{"h":{"argv":["` + script + `"]}}}`))
	if len(issues) != 0 {
		t.Fatalf("defer config rejected: %+v", issues)
	}
	c := decoded
	c.Calendars = map[string]struct {
		File string `json:"file"`
	}{"trading": {File: calendar}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	getState := func(id string) instanceState { r.mu.Lock(); defer r.mu.Unlock(); return r.states[date+"/"+id] }
	if err := r.runBootEvents(now); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return getState("heavy").Status == "DEFERRED" })
	r.schedule(now)
	st := getState("heavy")
	if st.Status != "DEFERRED" || st.LastError != "phase INTRADAY_AM is not allowed" || st.Attempts != 0 {
		t.Fatalf("deferred state=%+v", st)
	}
	b, _ := json.Marshal(st)
	if !strings.Contains(string(b), `"next_phase":"MIDDAY_BREAK"`) {
		t.Fatalf("deferred state omitted next phase: %s", b)
	}
	fc.Advance(90 * time.Minute)
	waitFor(t, func() bool { r.mu.Lock(); defer r.mu.Unlock(); return !r.running["heavy"] })
	r.schedule(fc.Now())
	waitFor(t, func() bool { return isTerminal(getState("heavy").Status) })
	waitFor(t, func() bool { r.mu.Lock(); defer r.mu.Unlock(); return !r.running["heavy"] })
	if got := getState("heavy").Status; got != "SUCCEEDED" {
		t.Fatalf("resumed status=%s", got)
	}
}

func TestBootApplyPlanSchedulesDeferredFetchItemOnceAllowed(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	boot := filepath.Join(root, "boot.sh")
	fetch := filepath.Join(root, "fetch.sh")
	if err := os.WriteFile(boot, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\",\"detail\":{\"deferred\":[{\"id\":\"daily_kline\",\"partition\":\"2026-10-19\",\"run_in_phases\":[\"MIDDAY_BREAK\"]}]}}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(fetch, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	calendar := filepath.Join(root, "calendar.json")
	if err := os.WriteFile(calendar, []byte(`["2026-10-20"]`), 0600); err != nil {
		t.Fatal(err)
	}
	c := config.Config{Events: []config.Event{
		{ID: "boot_apply_plan", Handler: "boot", Trigger: config.Trigger{Type: "startup"}},
		{ID: "fetch_item", Handler: "fetch", Trigger: config.Trigger{Type: "startup"}, Phases: []string{"MIDDAY_BREAK"}},
	}, Handlers: map[string]config.Handler{"boot": {Argv: []string{boot}}, "fetch": {Argv: []string{fetch}}},
		Calendars: map[string]struct {
			File string `json:"file"`
		}{"trading": {File: calendar}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	getState := func(id string) instanceState { r.mu.Lock(); defer r.mu.Unlock(); return r.states[date+"/"+id] }
	r.execute(Instance{ID: "boot_apply_plan", EventID: "boot_apply_plan", Start: "00:00:00", Deadline: "23:59:59", TriggerType: "startup"}, date)
	deferred := getState("fetch_item:daily_kline")
	if deferred.Status != "DEFERRED" || deferred.NextPhase != "MIDDAY_BREAK" || deferred.DataDate != "2026-10-19" {
		t.Fatalf("deferred item state=%+v", deferred)
	}
	fc.Advance(90 * time.Minute)
	r.schedule(fc.Now())
	waitFor(t, func() bool { return getState("fetch_item:daily_kline").Status == "SUCCEEDED" })
	if got := getState("fetch_item:daily_kline"); got.DataDate != "2026-10-19" {
		t.Fatalf("fetch item lost target partition: %+v", got)
	}
	r.schedule(fc.Now())
	time.Sleep(20 * time.Millisecond)
	if got := getState("fetch_item:daily_kline"); got.Attempts != 1 {
		t.Fatalf("successful fetch repeated: %+v", got)
	}
}

func TestConfiguredRuntimeReconcileHandlerSchedulesDeferredFetchItem(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:00", loc)
	fc := newFakeClock(now)
	boot := filepath.Join(root, "reconcile.sh")
	if err := os.WriteFile(boot, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\",\"detail\":{\"results\":[{\"id\":\"auction_baseline\",\"status\":\"DEPENDENCY_FAILED\",\"partition\":\"2026-10-20\",\"retryable\":true,\"error_code\":\"DEPENDENCY_FAILED\",\"failed_dependencies\":[\"symbol_universe\"],\"cost\":\"light\"}]}}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	c := config.Config{Events: []config.Event{
		{ID: "runtime_reconcile_0900", Handler: "legacy_boot_apply_plan", Trigger: config.Trigger{Type: "window"}},
		{ID: "fetch_item", Handler: "fetch", Trigger: config.Trigger{Type: "startup"}, Phases: []string{"PRE_MARKET"}},
	}, Handlers: map[string]config.Handler{"legacy_boot_apply_plan": {Argv: []string{boot}}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l,
		states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	r.execute(Instance{ID: "runtime_reconcile_0900", EventID: "runtime_reconcile_0900", Start: "09:00:00",
		Deadline: "09:10:00", TriggerType: "window"}, date)
	r.mu.Lock()
	got := r.states[date+"/fetch_item:auction_baseline"]
	r.mu.Unlock()
	if got.Status != "DEFERRED" || got.DataDate != date || got.Cost != "light" || len(got.WaitFor) != 1 || got.WaitFor[0] != "fetch_item:symbol_universe" {
		t.Fatalf("runtime reconcile did not enqueue fetch item: %+v", got)
	}
}

func TestRuntimeReconcileDoesNotRearmAnInFlightFetchItem(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:00", loc)
	fc := newFakeClock(now)
	boot := filepath.Join(root, "reconcile.sh")
	if err := os.WriteFile(boot, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\",\"detail\":{\"deferred\":[{\"id\":\"symbol_universe\",\"partition\":\"2026-10-20\",\"run_in_phases\":[\"PRE_MARKET\"],\"cost\":\"light\"}]}}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	c := config.Config{Events: []config.Event{
		{ID: "runtime_reconcile_0900", Handler: "legacy_boot_apply_plan", Trigger: config.Trigger{Type: "window"}},
		{ID: "fetch_item", Handler: "fetch", Trigger: config.Trigger{Type: "startup"}, Phases: []string{"PRE_MARKET"}},
	}, Handlers: map[string]config.Handler{"legacy_boot_apply_plan": {Argv: []string{boot}}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	key := date + "/fetch_item:symbol_universe"
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l,
		states:  map[string]instanceState{key: {Status: "RUNNING", Attempts: 1, DataDate: date, Cost: "light"}},
		running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	r.execute(Instance{ID: "runtime_reconcile_0900", EventID: "runtime_reconcile_0900", Start: "09:00:00",
		Deadline: "09:10:00", TriggerType: "window"}, date)
	r.mu.Lock()
	got := r.states[key]
	r.mu.Unlock()
	if got.Status != "RUNNING" || got.Attempts != 1 {
		t.Fatalf("runtime reconcile duplicated in-flight fetch: %+v", got)
	}
}

func TestPendingOwnerEventsSuppressDuplicateSameDateFetches(t *testing.T) {
	root, date := t.TempDir(), "2026-10-22"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T08:30:00+08:00", loc)
	r := &liveRunner{c: config.Config{Events: []config.Event{{
		ID: "startup_0830", Trigger: config.Trigger{Type: "window", Start: "08:30:00", Deadline: "08:45:00"},
		LateUntil: "09:10:00",
	}, {
		ID: "postmarket_publish", Trigger: config.Trigger{Type: "window", Start: "15:00:00", Deadline: "16:00:00"},
		LateUntil: "21:00:00",
	}}}, root: root, loc: loc, clock: newFakeClock(now), states: map[string]instanceState{}}
	if got := r.pendingOwnerEvents(date, now); got["startup_0830"] != date {
		t.Fatalf("open owner window was not reported: %v", got)
	}
	if got := r.pendingOwnerEvents(date, now); len(got) != 1 {
		t.Fatalf("future owner window must not suppress current reconciliation: %v", got)
	}
	r.states[date+"/startup_0830"] = instanceState{Status: "FAILED_FINAL", Attempts: 3}
	if got := r.pendingOwnerEvents(date, now); len(got) != 0 {
		t.Fatalf("terminal owner event must allow reconciliation to backfill: %v", got)
	}
}

func TestLightFetchItemUsesInstanceCostAndMayRunDuringIntraday(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	fetch := filepath.Join(root, "fetch.sh")
	if err := os.WriteFile(fetch, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	c := config.Config{Events: []config.Event{
		{ID: "fetch_item", Handler: "fetch", Trigger: config.Trigger{Type: "startup"}, Cost: "heavy", Phases: []string{"NIGHT"}, OnPhaseMismatch: "defer"},
	}, Handlers: map[string]config.Handler{"fetch": {Argv: []string{fetch}}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l,
		states:  map[string]instanceState{date + "/fetch_item:auction_baseline": {Status: "DEFERRED", DataDate: date, Cost: "light"}},
		running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	r.execute(Instance{ID: "fetch_item:auction_baseline", EventID: "fetch_item", Start: "00:00:00", Deadline: "23:59:59", DataDate: date, TriggerType: "startup"}, date)
	r.mu.Lock()
	got := r.states[date+"/fetch_item:auction_baseline"]
	heavyRunning := r.heavyRunning
	r.mu.Unlock()
	if got.Status != "SUCCEEDED" || heavyRunning {
		t.Fatalf("light fetch item inherited template restrictions: state=%+v heavy_running=%v", got, heavyRunning)
	}
}

func TestHeavyDeferredFetchMovesToNextAllowedPhaseAfterCutoff(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:10:00", loc)
	fc := newFakeClock(now)
	calendar := filepath.Join(root, "calendar.json")
	if err := os.WriteFile(calendar, []byte(`["2026-10-20"]`), 0600); err != nil {
		t.Fatal(err)
	}
	c := config.Config{
		Events: []config.Event{{ID: "fetch_item", Handler: "fetch", Trigger: config.Trigger{Type: "startup"},
			Cost: "heavy", Phases: []string{"PRE_MARKET", "MIDDAY_BREAK"}, OnPhaseMismatch: "defer"}},
		HeavyCutoffs: map[string]string{"PRE_MARKET": "09:10:00"},
		Calendars: map[string]struct {
			File string `json:"file"`
		}{"trading": {File: calendar}},
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l,
		states:  map[string]instanceState{date + "/fetch_item:daily_kline": {Status: "DEFERRED", DataDate: "2026-10-19", NextPhase: "PRE_MARKET", Cost: "heavy"}},
		running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	r.schedule(fc.Now())
	r.mu.Lock()
	got := r.states[date+"/fetch_item:daily_kline"]
	r.mu.Unlock()
	if got.Status != "DEFERRED" || got.NextPhase != "MIDDAY_BREAK" {
		t.Fatalf("heavy fetch was not moved past PRE_MARKET cutoff: %+v", got)
	}
}

func TestPlanListingAnItemWhoseRecordedFetchSucceededRearmsIt(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	boot := filepath.Join(root, "boot.sh")
	fetch := filepath.Join(root, "fetch.sh")
	if err := os.WriteFile(boot, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\",\"detail\":{\"deferred\":[{\"id\":\"daily_kline\",\"partition\":\"2026-10-19\",\"run_in_phases\":[\"MIDDAY_BREAK\"]}]}}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(fetch, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	calendar := filepath.Join(root, "calendar.json")
	if err := os.WriteFile(calendar, []byte(`["2026-10-20"]`), 0600); err != nil {
		t.Fatal(err)
	}
	c := config.Config{Events: []config.Event{
		{ID: "boot_apply_plan", Handler: "boot", Trigger: config.Trigger{Type: "startup"}},
		{ID: "fetch_item", Handler: "fetch", Trigger: config.Trigger{Type: "startup"}, Phases: []string{"MIDDAY_BREAK"}},
	}, Handlers: map[string]config.Handler{"boot": {Argv: []string{boot}}, "fetch": {Argv: []string{fetch}}},
		Calendars: map[string]struct {
			File string `json:"file"`
		}{"trading": {File: calendar}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	getState := func(id string) instanceState { r.mu.Lock(); defer r.mu.Unlock(); return r.states[date+"/"+id] }
	r.states[date+"/fetch_item:daily_kline"] = instanceState{Status: "SUCCEEDED", Attempts: 1, DataDate: "2026-10-19"}
	r.execute(Instance{ID: "boot_apply_plan", EventID: "boot_apply_plan", Start: "00:00:00", Deadline: "23:59:59", TriggerType: "startup"}, date)
	if got := getState("fetch_item:daily_kline"); got.Status != "DEFERRED" || got.Attempts != 0 || got.DataDate != "2026-10-19" {
		t.Fatalf("stale success not rearmed: %+v", got)
	}
}

func TestBootApplyPlanRequeuesFailedItemAsFetchItemWithPartition(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 11:40:00", loc)
	fc := newFakeClock(now)
	boot := filepath.Join(root, "boot.sh")
	fetch := filepath.Join(root, "fetch.sh")
	if err := os.WriteFile(boot, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\",\"detail\":{\"results\":[{\"id\":\"limit_reasons\",\"status\":\"failed\",\"partition\":\"2026-10-19\",\"retryable\":true,\"error_code\":\"FETCH_JOB_FAILED\",\"detail\":{\"item_id\":\"limit_reasons\",\"elapsed_ms\":5}}]}}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(fetch, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	calendar := filepath.Join(root, "calendar.json")
	if err := os.WriteFile(calendar, []byte(`["2026-10-20"]`), 0600); err != nil {
		t.Fatal(err)
	}
	c := config.Config{Events: []config.Event{
		{ID: "boot_apply_plan", Handler: "boot", Trigger: config.Trigger{Type: "startup"}},
		{ID: "fetch_item", Handler: "fetch", Trigger: config.Trigger{Type: "startup"}, Phases: []string{"MIDDAY_BREAK"}},
	}, Handlers: map[string]config.Handler{"boot": {Argv: []string{boot}}, "fetch": {Argv: []string{fetch}}},
		Calendars: map[string]struct {
			File string `json:"file"`
		}{"trading": {File: calendar}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	getState := func(id string) instanceState { r.mu.Lock(); defer r.mu.Unlock(); return r.states[date+"/"+id] }
	r.execute(Instance{ID: "boot_apply_plan", EventID: "boot_apply_plan", Start: "00:00:00", Deadline: "23:59:59", TriggerType: "startup"}, date)
	deferred := getState("fetch_item:limit_reasons")
	if deferred.Status != "DEFERRED" || deferred.NextPhase != "MIDDAY_BREAK" || deferred.DataDate != "2026-10-19" {
		t.Fatalf("deferred item state=%+v", deferred)
	}
	fc.Advance(time.Second)
	r.schedule(fc.Now())
	waitFor(t, func() bool { return getState("fetch_item:limit_reasons").Status == "SUCCEEDED" })
	if got := getState("fetch_item:limit_reasons"); got.DataDate != "2026-10-19" {
		t.Fatalf("fetch item lost target partition: %+v", got)
	}
	r.schedule(fc.Now())
	time.Sleep(20 * time.Millisecond)
	if got := getState("fetch_item:limit_reasons"); got.Attempts != 1 {
		t.Fatalf("successful fetch repeated: %+v", got)
	}
}

func TestQueuedFetchItemWaitsForItsQueuedDependency(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 11:40:00", loc)
	fc := newFakeClock(now)
	boot := filepath.Join(root, "boot.sh")
	fetch := filepath.Join(root, "fetch.sh")
	if err := os.WriteFile(boot, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\",\"detail\":{\"deferred\":[{\"id\":\"daily_kline\",\"partition\":\"2026-10-20\",\"run_in_phases\":[\"MIDDAY_BREAK\"]},{\"id\":\"daily_indicators\",\"partition\":\"2026-10-20\",\"run_in_phases\":[\"MIDDAY_BREAK\"],\"depends_on\":[\"daily_kline\"]}]}}'\n"), 0700); err != nil {
		t.Fatal(err)
	}

	if err := os.WriteFile(fetch, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	calendar := filepath.Join(root, "calendar.json")
	if err := os.WriteFile(calendar, []byte(`["2026-10-20"]`), 0600); err != nil {
		t.Fatal(err)
	}
	c := config.Config{Events: []config.Event{
		{ID: "boot_apply_plan", Handler: "boot", Trigger: config.Trigger{Type: "startup"}},
		{ID: "fetch_item", Handler: "fetch", Trigger: config.Trigger{Type: "startup"}, Phases: []string{"MIDDAY_BREAK"}},
	}, Handlers: map[string]config.Handler{"boot": {Argv: []string{boot}}, "fetch": {Argv: []string{fetch}}},
		Calendars: map[string]struct {
			File string `json:"file"`
		}{"trading": {File: calendar}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	getState := func(id string) instanceState { r.mu.Lock(); defer r.mu.Unlock(); return r.states[date+"/"+id] }
	r.execute(Instance{ID: "boot_apply_plan", EventID: "boot_apply_plan", Start: "00:00:00", Deadline: "23:59:59", TriggerType: "startup"}, date)
	if got := getState("fetch_item:daily_indicators"); len(got.WaitFor) != 1 || got.WaitFor[0] != "fetch_item:daily_kline" {
		t.Fatalf("dependent lost its wait list: %+v", got)
	}
	// Only the independent item may start; the dependent stays queued without burning an attempt.
	r.mu.Lock()
	r.states[date+"/fetch_item:daily_kline"] = instanceState{Status: "RUNNING", Attempts: 1, DataDate: "2026-10-20", NextPhase: "MIDDAY_BREAK"}
	r.running["fetch_item:daily_kline"] = true
	r.mu.Unlock()
	r.schedule(fc.Now())
	time.Sleep(50 * time.Millisecond)
	if got := getState("fetch_item:daily_indicators"); got.Status != "DEFERRED" || got.Attempts != 0 {
		t.Fatalf("dependent started before its dependency finished: %+v", got)
	}
	r.mu.Lock()
	r.states[date+"/fetch_item:daily_kline"] = instanceState{Status: "SUCCEEDED", Attempts: 1, DataDate: "2026-10-20"}
	r.running["fetch_item:daily_kline"] = false
	r.mu.Unlock()
	r.schedule(fc.Now())
	waitFor(t, func() bool { return getState("fetch_item:daily_indicators").Status == "SUCCEEDED" })
}

func TestHeavyLoadDeferralUsesFakeClockWithoutAttempts(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	script := filepath.Join(root, "ok.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	loads := 0
	e := config.Event{ID: "heavy", Handler: "h", Cost: "heavy", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "10:30:00"}, LateUntil: "11:00:00"}
	c := config.Config{HeavyMaxLoad1: json.RawMessage(`1`), Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, loadavg: func() (float64, error) {
		loads++
		if loads == 1 {
			return 2, nil
		}
		return 0, nil
	}}
	getState := func() instanceState { r.mu.Lock(); defer r.mu.Unlock(); return r.states[date+"/heavy"] }
	go r.execute(Instance{ID: "heavy", EventID: "heavy", Start: "09:00:00", Deadline: "10:30:00", LateUntil: "11:00:00"}, date)
	waitFor(t, func() bool { return getState().Status == "DEFER_LOAD" })
	waitFor(t, func() bool { return fc.waitCount() > 0 })
	if st := getState(); st.Attempts != 0 || ledgerTypeCounts(t, root, date)["ALERT_OUT"] != 0 {
		t.Fatalf("load deferral state=%+v alerts=%d", st, ledgerTypeCounts(t, root, date)["ALERT_OUT"])
	}
	fc.Advance(time.Minute)
	waitFor(t, func() bool { return isTerminal(getState().Status) })
	if st := getState(); st.Status != "SUCCEEDED" || st.Attempts != 1 || loads != 2 {
		t.Fatalf("heavy result=%+v load checks=%d", st, loads)
	}
}

func TestHeavySlotCoversHandlerAttemptButNotGatePolling(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	ready := filepath.Join(root, "gate-ready")
	gate := filepath.Join(root, "gate.sh")
	gateSource := "#!/bin/sh\nif [ -f '" + ready + "' ]; then printf '%s\\n' '{\"status\":\"ready\"}'; else printf '%s\\n' '{\"status\":\"not_ready\",\"reason\":\"waiting\"}'; fi\n"
	if err := os.WriteFile(gate, []byte(gateSource), 0700); err != nil {
		t.Fatal(err)
	}
	handler := filepath.Join(root, "handler.sh")
	if err := os.WriteFile(handler, []byte("#!/bin/sh\nsleep 0.3\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	e := config.Event{ID: "gated", Handler: "h", Cost: "heavy", Gate: config.Gate{Ref: "input_ready", Poll: "1s"}, Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "10:00:10"}, Retry: config.Retry{MaxAttempts: 1}}
	c := config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{handler}, GateArgv: []string{gate}}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	x := Instance{ID: "gated", EventID: "gated", Start: "09:00:00", Deadline: "10:00:10"}
	done := make(chan struct{})
	go func() {
		defer close(done)
		r.execute(x, date)
	}()
	defer func() {
		// Join the executor before TempDir cleanup. On an early assertion failure,
		// move the fake clock past the gate deadline so the poller can exit too.
		fc.Advance(time.Minute)
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("executor did not exit before TempDir cleanup")
		}
	}()
	waitFor(t, func() bool { return ledgerTypeCounts(t, root, date)["GATE_POLL"] > 0 })
	waitFor(t, func() bool { return fc.waitCount() > 0 })
	r.mu.Lock()
	gateHeldHeavy := r.heavyRunning
	r.mu.Unlock()
	if gateHeldHeavy {
		t.Fatal("heavy slot held while gate polling was waiting")
	}
	if err := os.WriteFile(ready, []byte("ready\n"), 0600); err != nil {
		t.Fatal(err)
	}
	fc.Advance(time.Second)
	waitFor(t, func() bool { return ledgerTypeCounts(t, root, date)["ATTEMPT_START"] == 1 })
	r.mu.Lock()
	attemptHeldHeavy := r.heavyRunning
	r.mu.Unlock()
	if !attemptHeldHeavy {
		t.Fatal("heavy slot was not held during handler attempt")
	}
	waitFor(t, func() bool { return isTerminal(runtimeTestState(r, date+"/gated").Status) })
	r.mu.Lock()
	deferredHeavy := r.heavyRunning
	r.mu.Unlock()
	if deferredHeavy {
		t.Fatal("heavy slot remained held after handler attempt ended")
	}
}

func TestHeavySlotReleasedDuringRetryBackoff(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	marker := filepath.Join(root, "first-attempt")
	script := filepath.Join(root, "retry.sh")
	source := "#!/bin/sh\nif [ ! -f '" + marker + "' ]; then touch '" + marker + "'; printf '%s\\n' '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"TEMP\",\"retryable\":true}'; exit 30; else printf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'; fi\n"
	if err := os.WriteFile(script, []byte(source), 0700); err != nil {
		t.Fatal(err)
	}
	e := config.Event{ID: "retry", Handler: "h", Cost: "heavy", Retry: config.Retry{MaxAttempts: 2, Backoff: "2s"}, Trigger: config.Trigger{Type: "startup"}}
	c := config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	go r.execute(Instance{ID: "retry", EventID: "retry", TriggerType: "startup"}, date)
	waitFor(t, func() bool { return ledgerTypeCounts(t, root, date)["ATTEMPT_END"] == 1 && fc.waitCount() > 0 })
	r.mu.Lock()
	backoffHeldHeavy := r.heavyRunning
	r.mu.Unlock()
	if backoffHeldHeavy {
		t.Fatal("heavy slot held during retry backoff")
	}
	fc.Advance(2 * time.Second)
	waitFor(t, func() bool { return ledgerTypeCounts(t, root, date)["ATTEMPT_START"] == 2 })
	r.mu.Lock()
	secondAttemptHeldHeavy := r.heavyRunning
	r.mu.Unlock()
	if !secondAttemptHeldHeavy {
		t.Fatal("heavy slot was not held during retry attempt")
	}
	waitFor(t, func() bool { return isTerminal(runtimeTestState(r, date+"/retry").Status) })
	r.mu.Lock()
	finalHeldHeavy := r.heavyRunning
	r.mu.Unlock()
	if finalHeldHeavy {
		t.Fatal("heavy slot remained held after final attempt")
	}
}

func TestHeavyInstancesQueueOutsideClassSlots(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	releaseHandler := filepath.Join(root, "release-handler")
	script := filepath.Join(root, "slow.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\ni=0\nwhile [ ! -e '"+releaseHandler+"' ] && [ \"$i\" -lt 1000 ]; do i=$((i + 1)); sleep 0.01; done\n[ -e '"+releaseHandler+"' ] || exit 75\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	events := []config.Event{
		{ID: "heavy_a", Handler: "h", Cost: "heavy", Class: "shared"},
		{ID: "heavy_b", Handler: "h", Cost: "heavy", Class: "shared"},
	}
	c := config.Config{Concurrency: map[string]json.RawMessage{"shared": json.RawMessage(`{"max":10}`)}, Events: events, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	defer func() { _ = os.WriteFile(releaseHandler, []byte("release"), 0600) }()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	getState := func(id string) instanceState { r.mu.Lock(); defer r.mu.Unlock(); return r.states[date+"/"+id] }
	x1 := Instance{ID: "heavy_a", EventID: "heavy_a", Start: "09:00:00", Deadline: "11:00:00"}
	x2 := Instance{ID: "heavy_b", EventID: "heavy_b", Start: "09:00:00", Deadline: "11:00:00"}
	go r.execute(x1, date)
	waitFor(t, func() bool { return getState("heavy_a").Status == "RUNNING" })
	waitFor(t, func() bool { return runtimeTestHeavyRunning(r) })
	go r.execute(x2, date)
	waitFor(t, func() bool { return getState("heavy_b").Status == "QUEUED_HEAVY" })
	if got := getState("heavy_b").Status; got != "QUEUED_HEAVY" {
		t.Fatalf("second heavy status=%s", got)
	}
	if err := os.WriteFile(releaseHandler, []byte("release"), 0600); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return isTerminal(getState("heavy_a").Status) })
	waitFor(t, func() bool { return isTerminal(getState("heavy_b").Status) })
	if got := getState("heavy_b").Status; got != "SUCCEEDED" {
		t.Fatalf("queued heavy final status=%s", got)
	}
	if len(r.classSlots) != 0 {
		t.Fatalf("heavy used ordinary class slots: %v", r.classSlots)
	}
}

func TestLightInstancesBypassClassConcurrencyLimit(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	firstStarted := filepath.Join(root, "first.started")
	firstEnded := filepath.Join(root, "first.ended")
	secondStarted := filepath.Join(root, "second.started")
	first := filepath.Join(root, "first.sh")
	second := filepath.Join(root, "second.sh")
	if err := os.WriteFile(first, []byte("#!/bin/sh\nprintf started > '"+firstStarted+"'\nsleep 0.5\nprintf ended > '"+firstEnded+"'\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(second, []byte("#!/bin/sh\nprintf started > '"+secondStarted+"'\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	events := []config.Event{{ID: "light_a", Handler: "a", Class: "shared", Cost: "light"}, {ID: "light_b", Handler: "b", Class: "shared", Cost: "light"}}
	c := config.Config{Concurrency: map[string]json.RawMessage{"shared": json.RawMessage(`{"max":1}`)}, Events: events, Handlers: map[string]config.Handler{"a": {Argv: []string{first}}, "b": {Argv: []string{second}}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	go r.execute(Instance{ID: "light_a", EventID: "light_a", Start: "09:00:00", Deadline: "11:00:00"}, date)
	waitFor(t, func() bool { _, err := os.Stat(firstStarted); return err == nil })
	go r.execute(Instance{ID: "light_b", EventID: "light_b", Start: "09:00:00", Deadline: "11:00:00"}, date)
	waitFor(t, func() bool { _, err := os.Stat(secondStarted); return err == nil })
	if _, err := os.Stat(firstEnded); err == nil {
		t.Fatal("second light handler started only after first completed")
	}
	waitFor(t, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return isTerminal(r.states[date+"/light_a"].Status) && isTerminal(r.states[date+"/light_b"].Status)
	})
	waitFor(t, func() bool { r.mu.Lock(); defer r.mu.Unlock(); return !r.running["light_a"] && !r.running["light_b"] })
	if len(r.classSlots) != 0 {
		t.Fatalf("light work used ordinary class slots: %v", r.classSlots)
	}
}

func TestStartupEventsFollowAfterOrderWithFakeClock(t *testing.T) {
	root := t.TempDir()
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now := time.Date(2026, 10, 20, 9, 20, 0, 0, loc)
	fc := newFakeClock(now)
	log := filepath.Join(root, "order.log")
	calendar := filepath.Join(root, "calendar.json")
	if err := os.WriteFile(calendar, []byte(`["2026-10-20"]`), 0600); err != nil {
		t.Fatal(err)
	}
	a := filepath.Join(root, "a.sh")
	b := filepath.Join(root, "b.sh")
	for path, name := range map[string]string{a: "a", b: "b"} {
		body := "#!/bin/sh\necho " + name + " >> " + log + "\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"
		if err := os.WriteFile(path, []byte(body), 0700); err != nil {
			t.Fatal(err)
		}
	}
	c := config.Config{ConfigVersion: 1, Timezone: "Asia/Shanghai", Calendars: map[string]struct {
		File string `json:"file"`
	}{"trading": {File: calendar}}, Handlers: map[string]config.Handler{"ha": {Argv: []string{a}}, "hb": {Argv: []string{b}}}, Events: []config.Event{{ID: "boot_a", Enabled: true, Trigger: config.Trigger{Type: "startup", Order: 20}, Handler: "ha"}, {ID: "boot_b", Enabled: true, Trigger: config.Trigger{Type: "startup", Order: 10, After: []string{"boot_a"}}, Handler: "hb"}}}
	l, err := openLedger(root, now.Format("2006-01-02"))
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, boot: "test", clock: fc, fake: fc, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}, ledger: l}
	if err = r.runBootEvents(now); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "a\nb\n" {
		t.Fatalf("startup order=%q", got)
	}
}

func TestLedgerTruncatesCorruptTailAndResumesSequence(t *testing.T) {
	root := t.TempDir()
	fc := newFakeClock(time.Date(2026, 10, 20, 10, 0, 0, 0, time.UTC))
	l, err := openLedger(root, "2026-10-20")
	if err != nil {
		t.Fatal(err)
	}
	l.now = fc.Now
	if err = l.append("INSTANCE_STATE", map[string]any{"event": "done"}); err != nil {
		t.Fatal(err)
	}
	_ = l.close()
	p := filepath.Join(root, "ledger", "2026-10-20.jsonl")
	f, err := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = f.WriteString("{\"seq\":2,\"truncated\":")
	_ = f.Close()
	original, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	l, err = openLedger(root, "2026-10-20")
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	if err = l.append("CHECK", nil); err != nil {
		t.Fatal(err)
	}
	f, err = os.Open(p)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	var records []ledgerRecord
	for s.Scan() {
		var r ledgerRecord
		if err := json.Unmarshal(s.Bytes(), &r); err != nil {
			t.Fatalf("invalid record: %v", err)
		}
		records = append(records, r)
	}
	if len(records) != 3 || records[1].Seq != 2 || records[1].Type != "TAIL_TRUNCATED" || records[2].Seq != 3 || records[2].Type != "CHECK" {
		t.Fatalf("tail not recovered: %#v", records)
	}
	data, _ := json.Marshal(records[1].Data)
	var tail struct {
		BackupPath string `json:"backup_path"`
	}
	_ = json.Unmarshal(data, &tail)
	backup, err := os.ReadFile(tail.BackupPath)
	if err != nil || string(backup) != string(original) || !strings.Contains(tail.BackupPath, ".corrupt-") {
		t.Fatalf("corrupt ledger backup=%q err=%v", tail.BackupPath, err)
	}
}

func TestLateWindowRunsAndMarksLedgerWithFakeClock(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	start, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:02:00", loc)
	fc := newFakeClock(start)
	script := filepath.Join(root, "ok.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	e := config.Event{ID: "late", Handler: "h", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:01:00"}, LateUntil: "09:03:00"}
	c := config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, start: start, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	r.schedule(start)
	getState := func() instanceState { r.mu.Lock(); defer r.mu.Unlock(); return r.states[date+"/late"] }
	waitFor(t, func() bool { return isTerminal(getState().Status) })
	if st := getState(); st.Status != "SUCCEEDED" || !st.Late {
		t.Fatalf("late result=%+v", st)
	}
	missed, active := startupClassify(c, start)
	if len(missed) != 0 || len(active) != 1 {
		t.Fatalf("late recovery classify missed=%d active=%d", len(missed), len(active))
	}
	fc.Advance(time.Minute)
	missed, active = startupClassify(c, fc.Now())
	if len(missed) != 0 || len(active) != 1 {
		t.Fatalf("late_until boundary missed=%d active=%d", len(missed), len(active))
	}
	fc.Advance(time.Second)
	missed, active = startupClassify(c, fc.Now())
	if len(missed) != 1 || len(active) != 0 {
		t.Fatalf("expired late recovery classify missed=%d active=%d", len(missed), len(active))
	}
	lateLedger := string(mustRead(t, filepath.Join(root, "ledger", date+".jsonl")))
	if !strings.Contains(lateLedger, `"status":"LATE_RUN"`) || !strings.Contains(lateLedger, `"late":true`) {
		t.Fatal("ledger omitted LATE_RUN marker")
	}
}

func TestNotApplicableIsSkippedWithoutAlertUsingFakeClock(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:00", loc)
	fc := newFakeClock(now)
	script := filepath.Join(root, "na.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"not_applicable\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	e := config.Event{ID: "na", Handler: "h"}
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}, root: root, loc: loc, clock: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	r.execute(Instance{ID: "na", EventID: "na", Start: "09:00:00", Deadline: "09:01:00"}, date)
	if got := runtimeTestState(r, date+"/na").Status; got != "SKIPPED_NA" {
		t.Fatalf("status=%s", got)
	}
	if ledgerTypeCounts(t, root, date)["ALERT_OUT"] != 0 {
		t.Fatal("not_applicable emitted an alert")
	}
}

func TestSuccessfulHandlerResetsGateBlockAlertWithFakeClock(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	script := filepath.Join(root, "ok.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "after_block", Handler: "h"}
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}, root: root, loc: loc, clock: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{date + "/after_block/GATE_BLOCKED_FIRST": true}, gateAlerted: map[string]bool{"h": true}}
	r.execute(Instance{ID: "after_block", EventID: "after_block", Start: "10:00:00", Deadline: "10:01:00"}, date)
	if r.gateAlerted["h"] || r.alertSeen[date+"/after_block/GATE_BLOCKED_FIRST"] {
		t.Fatal("success did not reset gate alert suppression")
	}
}

func mustRead(t *testing.T, p string) []byte {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestFakeStartup40MissedUsesOneAlertRequest(t *testing.T) {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", "2026-10-20 10:34:00", loc)
	c := config.Config{Timezone: "Asia/Shanghai", Handlers: map[string]config.Handler{"alert": {Argv: []string{"/bin/true"}}}}
	for i := 0; i < 45; i++ {
		c.Events = append(c.Events, config.Event{ID: fmt.Sprintf("event_%02d", i), Enabled: true, Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:01:00"}})
	}
	missed, active := startupClassify(c, now)
	if len(missed) != 45 || len(active) != 0 {
		t.Fatalf("startup missed=%d active=%d", len(missed), len(active))
	}
	root := t.TempDir()
	l, err := openLedger(root, "2026-10-20")
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	r := &liveRunner{c: c, root: root, start: now, loc: loc, clock: newFakeClock(now), states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, ledger: l, missedStartup: nil, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	if err = r.applyStartup(now, missed, active); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(root, "ledger", "2026-10-20.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	alerts := 0
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		var rec ledgerRecord
		_ = json.Unmarshal([]byte(line), &rec)
		if rec.Type == "ALERT_OUT" {
			alerts++
		}
	}
	if alerts != 1 {
		t.Fatalf("alert requests=%d want 1", alerts)
	}
	for _, x := range missed {
		if runtimeTestState(r, "2026-10-20/"+x.ID).Status != "MISSED" {
			t.Fatalf("%s not MISSED", x.ID)
		}
	}
}

func TestAlertArgvReceivesOneJSONRequestAndLimitsMinute(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	fc := newFakeClock(now)
	output := filepath.Join(root, "alerts.jsonl")
	script := filepath.Join(root, "alert.sh")
	body := "#!/bin/sh\ncat >> \"$ALERT_OUTPUT\"\n"
	if err := os.WriteFile(script, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: config.Config{Handlers: map[string]config.Handler{"alert": {Argv: []string{script}, EnvAllow: []string{"ALERT_OUTPUT"}}}}, root: root, loc: loc, clock: fc, ledger: l, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	t.Setenv("ALERT_OUTPUT", output)
	for i := 0; i < 5; i++ {
		r.alert("FAILED", map[string]any{"event": fmt.Sprintf("e%d", i), "date": date})
	}
	deadline := time.Now().Add(time.Second)
	var lines []string
	for time.Now().Before(deadline) {
		b, _ := os.ReadFile(output)
		lines = strings.Split(strings.TrimSpace(string(b)), "\n")
		if len(lines) >= 3 {
			break
		}
		runtime.Gosched()
	}
	if len(lines) != 3 {
		t.Fatalf("alert argv calls=%d, want minute cap 3", len(lines))
	}
	for _, line := range lines {
		var request map[string]any
		if err := json.Unmarshal([]byte(line), &request); err != nil || request["contract"] != float64(1) || request["reason"] != "FAILED" {
			t.Fatalf("invalid alert stdin JSON %q: %v", line, err)
		}
	}
	r.alert("FAILED", map[string]any{"event": "e0", "date": date}) // same-key dedupe
	if r.alertMinutes[now.Unix()/60] != 3 {
		t.Fatalf("minute dispatches=%d", r.alertMinutes[now.Unix()/60])
	}
}

func TestFakeClockAdvanceSocketOperation(t *testing.T) {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	start, _ := time.ParseInLocation(time.RFC3339, "2026-10-20T10:00:00+08:00", loc)
	fake := newFakeClock(start)
	r := &liveRunner{c: config.Config{ConfigVersion: 1}, mode: "fakeclock", loc: loc, clock: fake, fake: fake, states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, idempotency: map[string]any{}}
	server, client := net.Pipe()
	defer client.Close()
	go r.serve(server)
	_, err := fmt.Fprintln(client, `{"t":"req","id":"1","op":"clock.advance","args":{"by":"2h"}}`)
	if err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(client).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	var response map[string]any
	if err = json.Unmarshal([]byte(line), &response); err != nil {
		t.Fatal(err)
	}
	if response["ok"] != true || !fake.Now().Equal(start.Add(2*time.Hour)) {
		t.Fatalf("response=%v now=%s", response, fake.Now())
	}
}

func TestRecoveryRestoresCompletedStateWithoutRerun(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:34:00", loc)
	r := &liveRunner{c: config.Config{}, root: root, start: now, loc: loc, clock: newFakeClock(now), states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, ledger: l}
	x := Instance{ID: "already_done", EventID: "already_done", Start: "10:30:00", Deadline: "10:40:00"}
	r.setState(x, date, instanceState{Status: "SUCCEEDED", Attempts: 1, StartedAt: "2026-10-20T10:30:00+08:00", CompletedAt: "2026-10-20T10:31:00+08:00"})
	_ = l.close()
	l, err = openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	restarted := &liveRunner{c: config.Config{}, root: root, start: now, loc: loc, clock: newFakeClock(now), states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, ledger: l}
	if err = restarted.recover(); err != nil {
		t.Fatal(err)
	}
	if st := runtimeTestState(restarted, date+"/already_done"); st.Status != "SUCCEEDED" || st.Attempts != 1 {
		t.Fatalf("recovered state=%+v", st)
	}
	if err = restarted.applyStartup(now, nil, []Instance{x}); err != nil {
		t.Fatal(err)
	}
	if runtimeTestRunning(restarted, x.ID) {
		t.Fatal("completed instance was scheduled after restart")
	}
}

func TestStateExportIncludesRunningCompatibilityFields(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	r := &liveRunner{c: config.Config{ConfigVersion: 7}, root: root, loc: loc, clock: newFakeClock(now), states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}}
	x := Instance{ID: "daily", EventID: "daily"}
	r.setState(x, date, instanceState{Status: "RUNNING", Attempts: 2, StartedAt: "2026-10-20T10:33:00+08:00"})
	b, err := os.ReadFile(filepath.Join(root, date, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Events map[string]instanceState `json:"events"`
	}
	if err = json.Unmarshal(b, &doc); err != nil {
		t.Fatal(err)
	}
	state, ok := doc.Events[x.ID]
	if !ok || state.Status != "RUNNING" || state.Attempts != 2 || state.StartedAt == "" || state.CompletedAt != "" || state.LastError != "" {
		t.Fatalf("compatibility event state=%+v present=%v", state, ok)
	}
}

func TestTimeoutKillsHandlerProcessGroup(t *testing.T) {
	root := t.TempDir()
	script := filepath.Join(root, "handler.sh")
	pidFile := filepath.Join(root, "child.pid")
	body := "#!/bin/sh\n(trap '' TERM; while :; do :; done) &\necho $! > \"$CHILD_PID_FILE\"\nwait\n"
	if err := os.WriteFile(script, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	loc, _ := time.LoadLocation("Asia/Shanghai")
	_ = loc
	ledger, err := openLedger(root, "2026-10-20")
	if err != nil {
		t.Fatal(err)
	}
	defer ledger.close()
	e := config.Event{ID: "slow", Handler: "slow", Timeout: "100ms"}
	x := Instance{ID: "slow", EventID: "slow", Start: "10:00:00", Deadline: "10:01:00"}
	r := &liveRunner{c: config.Config{Handlers: map[string]config.Handler{"slow": {Argv: []string{script}, EnvAllow: []string{"CHILD_PID_FILE"}}}}, root: root, clock: realClock{}, loc: loc, ledger: ledger, boot: "test"}
	os.Setenv("CHILD_PID_FILE", pidFile)
	defer os.Unsetenv("CHILD_PID_FILE")
	_, err = r.invoke([]string{script}, &e, x, "2026-10-20", 1, false)
	if err == nil || !strings.Contains(err.Error(), "HANDLER_TIMEOUT") {
		t.Fatalf("invoke error=%v", err)
	}
	b, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatal(err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil {
		t.Fatal(err)
	}
	if err = syscall.Kill(pid, 0); err == nil {
		if stat, e := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid)); e == nil {
			fields := strings.Fields(string(stat))
			if len(fields) > 2 && fields[2] != "Z" {
				t.Fatalf("grandchild %d still running", pid)
			}
		}
	}
}

func TestThirtyFakeDaysRollStateAndKeepResourcesBounded(t *testing.T) {
	root := t.TempDir()
	loc, _ := time.LoadLocation("Asia/Shanghai")
	start, _ := time.ParseInLocation(time.RFC3339, "2026-10-01T23:59:00+08:00", loc)
	fc := newFakeClock(start)
	c := config.Config{Timezone: "Asia/Shanghai", Events: []config.Event{{ID: "daily", Enabled: true, Calendar: json.RawMessage(`"every_day"`), Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:01:00"}}}}
	l, err := openLedger(root, start.Format("2006-01-02"))
	if err != nil {
		t.Fatal(err)
	}
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, start: start, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	fdBefore := testFDCount(t)
	goBefore := runtime.NumGoroutine()
	var before runtime.MemStats
	runtime.ReadMemStats(&before)
	for day := 0; day < 30; day++ {
		r.schedule(fc.Now())
		if runtimeTestStateCount(r) != 1 {
			t.Fatalf("day %d retained %d in-memory dates", day, runtimeTestStateCount(r))
		}
		if day < 29 {
			fc.Advance(24 * time.Hour)
			if err = r.rollDate(fc.Now()); err != nil {
				t.Fatal(err)
			}
		}
	}
	fdAfter := testFDCount(t)
	goAfter := runtime.NumGoroutine()
	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	if fdAfter > fdBefore+1 || goAfter > goBefore+1 || after.HeapAlloc > before.HeapAlloc+8<<20 {
		t.Fatalf("resources grew: fd %d->%d goroutine %d->%d heap %d->%d", fdBefore, fdAfter, goBefore, goAfter, before.HeapAlloc, after.HeapAlloc)
	}
}
func testFDCount(t *testing.T) int {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Skip("/proc/self/fd unavailable")
	}
	return len(entries)
}

func TestStartupWindowRemainsRunnable(t *testing.T) {
	c := config.Config{Events: []config.Event{{ID: "a", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "10:30:00", Deadline: "10:40:00"}}}}
	date, _ := time.Parse("2006-01-02 15:04:05", "2026-10-20 10:34:00")
	missed, active := startupClassify(c, date)
	if len(missed) != 0 || len(active) != 1 {
		t.Fatalf("startup classification missed=%d active=%d", len(missed), len(active))
	}
}

func TestGateReadyAcceptsBridgeStatusReady(t *testing.T) {
	// guardian_handler.py gate prints {"status":"ready",...} / {"status":"not_ready",...}
	if !gateReady([]byte("{\"status\":\"ready\",\"reason\":\"gate_satisfied\"}\n")) {
		t.Fatal("status ready must open the gate")
	}
	if gateReady([]byte("{\"status\":\"not_ready\",\"reason\":\"gate_condition_false\"}\n")) {
		t.Fatal("not_ready must keep the gate closed")
	}
}

func runtimeTestState(r *liveRunner, key string) instanceState {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.states[key]
}

func runtimeTestStateCount(r *liveRunner) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.states)
}

func runtimeTestRunning(r *liveRunner, id string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.running[id]
}

func runtimeTestHeavyRunning(r *liveRunner) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.heavyRunning
}

func waitFor(t *testing.T, ready func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if ready() {
			return
		}
		runtime.Gosched()
	}
	t.Fatal("condition did not become true")
}

func ledgerTypeCounts(t *testing.T, root, date string) map[string]int {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(root, "ledger", date+".jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	counts := map[string]int{}
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if line == "" {
			continue
		}
		var rec ledgerRecord
		if err = json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatal(err)
		}
		counts[rec.Type]++
	}
	return counts
}

func socketServeRequest(t *testing.T, r *liveRunner, request string) map[string]any {
	t.Helper()
	server, client := net.Pipe()
	done := make(chan struct{})
	go func() { r.serve(server); close(done) }()
	if _, err := fmt.Fprintln(client, request); err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(client).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	_ = client.Close()
	<-done
	var response map[string]any
	if err = json.Unmarshal([]byte(line), &response); err != nil {
		t.Fatalf("invalid socket response %q: %v", line, err)
	}
	return response
}

func socketReady(path string) bool {
	c, err := net.DialTimeout("unix", path, 10*time.Millisecond)
	if err != nil {
		return false
	}
	_ = c.Close()
	return true
}

func unixRequest(t *testing.T, path, request string) map[string]any {
	t.Helper()
	c, err := net.DialTimeout("unix", path, time.Second)
	if err != nil {
		t.Fatalf("connect guardian socket: %v", err)
	}
	defer c.Close()
	if _, err = fmt.Fprintln(c, request); err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(c).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	var response map[string]any
	if err = json.Unmarshal([]byte(line), &response); err != nil {
		t.Fatalf("invalid socket response %q: %v", line, err)
	}
	return response
}

func errBody(resp map[string]any) map[string]any {
	value, _ := resp["error"].(map[string]any)
	return value
}

func TestLateInstanceStateStaysInItsTradeDateLedger(t *testing.T) {
	root := t.TempDir()
	loc, _ := time.LoadLocation("Asia/Shanghai")
	start, _ := time.ParseInLocation(time.RFC3339, "2026-10-20T23:59:00+08:00", loc)
	fc := newFakeClock(start)
	started, release := filepath.Join(root, "started"), filepath.Join(root, "release")
	script := filepath.Join(root, "handler.sh")
	body := "#!/bin/sh\ntouch \"$STARTED\"\nwhile [ ! -f \"$RELEASE\" ]; do :; done\nprintf '{\"contract\":1,\"status\":\"ok\"}\\n'\n"
	if err := os.WriteFile(script, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("STARTED", started)
	t.Setenv("RELEASE", release)
	l, err := openLedger(root, "2026-10-20")
	if err != nil {
		t.Fatal(err)
	}
	l.now = fc.Now
	e := config.Event{ID: "cross_midnight", Handler: "h", Timeout: "5m", Class: "default"}
	c := config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}, EnvAllow: []string{"STARTED", "RELEASE"}}}}
	r := &liveRunner{c: c, root: root, loc: loc, start: start, clock: fc, fake: fc, ledger: l, boot: "test", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}}
	x := Instance{ID: e.ID, EventID: e.ID, Start: "23:59:00", Deadline: "23:59:59"}
	done := make(chan struct{})
	go func() { r.execute(x, "2026-10-20"); close(done) }()
	waitFor(t, func() bool { _, e := os.Stat(started); return e == nil })
	fc.Advance(2 * time.Minute)
	if err = r.rollDate(fc.Now()); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(release, nil, 0600); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("cross-midnight handler did not finish")
	}
	_ = r.ledger.close()

	old, err := os.ReadFile(filepath.Join(root, "ledger", "2026-10-20.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	newDay, err := os.ReadFile(filepath.Join(root, "ledger", "2026-10-21.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(old), `"instance":"cross_midnight"`) {
		t.Fatal("late completion missing from original trade-date ledger")
	}
	if strings.Contains(string(newDay), `"trade_date":"2026-10-20"`) {
		t.Fatal("late completion leaked into next trade-date ledger")
	}
}

func TestRecoveredRunningInstanceConsumesNextAttempt(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	fc := newFakeClock(now)
	script := filepath.Join(root, "handler.sh")
	capture := filepath.Join(root, "requests.jsonl")
	body := "#!/bin/sh\ncat >> \"$REQUESTS\"\nprintf '{\"contract\":1,\"status\":\"ok\"}\\n'\n"
	if err := os.WriteFile(script, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("REQUESTS", capture)
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "resume", Handler: "h", Class: "default", Retry: config.Retry{MaxAttempts: 2}}
	x := Instance{ID: e.ID, EventID: e.ID, Start: "10:30:00", Deadline: "10:40:00"}
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}, EnvAllow: []string{"REQUESTS"}}}}, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, boot: "test", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	_ = l.append("INSTANCE_STATE", runState{EventID: e.ID, Instance: x.ID, TradeDate: date, State: instanceState{Status: "RUNNING", Attempts: 1, StartedAt: now.Format(time.RFC3339)}})
	if err = r.recover(); err != nil {
		t.Fatal(err)
	}
	if got := runtimeTestState(r, date+"/"+x.ID); got.Status != "RUNNING" || got.Attempts != 1 {
		t.Fatalf("recovered state=%+v", got)
	}
	r.execute(x, date)
	b, err := os.ReadFile(capture)
	if err != nil {
		t.Fatal(err)
	}
	var request struct {
		Attempt int `json:"attempt"`
	}
	if err = json.Unmarshal([]byte(strings.TrimSpace(string(b))), &request); err != nil {
		t.Fatalf("invalid captured request %q: %v", b, err)
	}
	if request.Attempt != 2 {
		t.Fatalf("recovered attempt=%d, want 2", request.Attempt)
	}
}

func TestAlertDailyLimitWritesSuppressionSummary(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	r := &liveRunner{c: config.Config{Handlers: map[string]config.Handler{"alert": {Argv: []string{"/bin/true"}}}}, root: root, loc: loc, clock: newFakeClock(now), ledger: l, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	for n := 0; n < 31; n++ {
		if n > 0 {
			r.clock.(*fakeClock).Advance(time.Minute)
		}
		if err = r.alert("FAILED", map[string]any{"event": fmt.Sprintf("event_%02d", n)}); err != nil {
			t.Fatal(err)
		}
	}
	counts := ledgerTypeCounts(t, root, date)
	if counts["ALERT_OUT"] != 30 || counts["ALERT_SUPPRESSED"] != 1 {
		t.Fatalf("alert ledger counts=%v, want 30 sent and 1 suppressed", counts)
	}
}

func TestGateReadyLedgerIncludesMeasuredVisibilityLag(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T09:33:05+08:00", loc)
	script := filepath.Join(root, "ready.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '{\"status\":\"ready\",\"reason\":\"checkpoint_threshold_met\",\"row_count\":4000,\"elapsed_ms\":12,\"query_window\":[\"2026-10-20 09:33:00\",\"2026-10-20 09:33:03\"],\"checkpoint_ts\":\"2026-10-20 09:33:02\"}\\n'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "ready", Handler: "h", Timeout: "10s", Trigger: config.Trigger{Deadline: "09:34:00"}, Gate: config.Gate{Ref: "checkpoint_ready", Poll: "1s", Timeout: "1s"}}
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {GateArgv: []string{script}, Argv: []string{"/bin/sh", "-c", "printf '{\"contract\":1,\"status\":\"ok\",\"retryable\":false}\\n'"}}}}, root: root, loc: loc, start: now, clock: newFakeClock(now), ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	r.execute(Instance{ID: e.ID, EventID: e.ID, Start: "09:33:00", Deadline: "09:34:00"}, date)
	b := mustRead(t, filepath.Join(root, "ledger", date+".jsonl"))
	var poll, ready map[string]any
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		var rec ledgerRecord
		if json.Unmarshal([]byte(line), &rec) != nil {
			continue
		}
		data, _ := json.Marshal(rec.Data)
		if rec.Type == "GATE_POLL" {
			_ = json.Unmarshal(data, &poll)
		}
		if rec.Type == "GATE_READY" {
			_ = json.Unmarshal(data, &ready)
		}
	}
	if poll["reason"] != "checkpoint_threshold_met" || poll["row_count"] != float64(4000) {
		t.Fatalf("GATE_POLL data=%v", poll)
	}
	if ready["window_end"] != "2026-10-20 09:33:02" || ready["visible_lag_seconds"] != float64(3) {
		t.Fatalf("GATE_READY data=%v", ready)
	}
}

func TestAlertFailedDeliveryCanRetryAndDistinctSummariesAreNotDeduped(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	countPath := filepath.Join(root, "count")
	script := filepath.Join(root, "alert.sh")
	body := "#!/bin/sh\nn=0; [ -f '" + countPath + "' ] && n=$(cat '" + countPath + "'); n=$((n+1)); echo $n > '" + countPath + "'; [ $n -gt 1 ]\n"
	if err := os.WriteFile(script, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = func() time.Time { return now }
	r := &liveRunner{c: config.Config{Handlers: map[string]config.Handler{"alert": {Argv: []string{script}}}}, root: root, loc: loc, clock: newFakeClock(now), ledger: l, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	if err := r.alert("FAILED", map[string]any{"event": "e"}); err == nil {
		t.Fatal("first failed delivery reported success")
	}
	if r.alertSeen[date+"/e/FAILED"] {
		t.Fatal("failed delivery marked alert as sent")
	}
	if err := r.alert("FAILED", map[string]any{"event": "e"}); err != nil {
		t.Fatalf("retry failed: %v", err)
	}
	r.clock.(*fakeClock).Advance(time.Minute)
	r.alert("GATE_BLOCKED", map[string]any{"events": []string{"a"}, "blocks": []map[string]string{{"event": "a", "reason": "stale", "at": "2026-10-20T10:35:00+08:00"}}, "count": 1, "summary": true})
	r.alert("GATE_BLOCKED", map[string]any{"events": []string{"a"}, "blocks": []map[string]string{{"event": "a", "reason": "timeout", "at": "2026-10-20T10:36:00+08:00"}}, "count": 1, "summary": true})
	r.alert("GATE_BLOCKED", map[string]any{"events": []string{"a"}, "blocks": []map[string]string{{"event": "a", "reason": "stale", "at": "2026-10-20T10:37:00+08:00"}}, "count": 1, "summary": true})
	counts := ledgerTypeCounts(t, root, date)
	if counts["ALERT_FAILED"] != 1 || counts["ALERT_OUT"] != 3 {
		t.Fatalf("alert outcomes=%v, want failed=1 sent=3", counts)
	}
}

func TestBootApplyPlanPreservesRecoveredPendingFetchState(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	boot := filepath.Join(root, "boot.sh")
	if err := os.WriteFile(boot, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\",\"detail\":{\"deferred\":[{\"id\":\"daily_kline\",\"partition\":\"2026-10-20\",\"run_in_phases\":[\"MIDDAY_BREAK\"]}]}}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	l.now = fc.Now
	x := Instance{ID: "fetch_item:daily_kline", EventID: "fetch_item", TriggerType: "startup"}
	if err = l.append("INSTANCE_STATE", runState{EventID: "fetch_item", Instance: x.ID, TradeDate: date, State: instanceState{Status: "RETRY_WAIT", Attempts: 1, NextAttemptAt: now.Add(2 * time.Minute).Format(time.RFC3339), DataDate: "2026-10-20"}}); err != nil {
		t.Fatal(err)
	}
	_ = l.close()
	l, err = openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	r := &liveRunner{c: config.Config{Events: []config.Event{{ID: "boot_apply_plan", Handler: "boot", Trigger: config.Trigger{Type: "startup"}}}, Handlers: map[string]config.Handler{"boot": {Argv: []string{boot}}}}, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	if err := r.recover(); err != nil {
		t.Fatal(err)
	}
	r.execute(Instance{ID: "boot_apply_plan", EventID: "boot_apply_plan", Start: "00:00:00", Deadline: "23:59:59", TriggerType: "startup"}, date)
	got := runtimeTestState(r, date+"/"+x.ID)
	if got.Status != "RETRY_WAIT" || got.Attempts != 1 || got.NextAttemptAt != now.Add(2*time.Minute).Format(time.RFC3339) || got.DataDate != "2026-10-20" {
		t.Fatalf("boot plan overwrote recovered state: %+v", got)
	}
}

func TestSlowHandlerDoesNotBlockOtherClassAndClassLimitIsEnforced(t *testing.T) {
	root := t.TempDir()
	release := filepath.Join(root, "release")
	started := filepath.Join(root, "started")
	overlap := filepath.Join(root, "overlap")
	script := filepath.Join(root, "handler.sh")
	body := "#!/bin/sh\nif [ \"$NAME\" = fast ]; then touch \"$STARTED_DIR/$NAME\"; printf '{\"contract\":1,\"status\":\"ok\"}\\n'; exit 0; fi\nif ! mkdir \"$LOCK_DIR\" 2>/dev/null; then touch \"$OVERLAP\"; fi\ntouch \"$STARTED_DIR/$NAME\"\nwhile [ ! -f \"$RELEASE_FILE\" ]; do :; done\nrmdir \"$LOCK_DIR\" 2>/dev/null || true\nprintf '{\"contract\":1,\"status\":\"ok\"}\\n'\n"
	if err := os.WriteFile(script, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	limitedLock := filepath.Join(root, "limited-lock")
	fastLock := filepath.Join(root, "fast-lock")
	t.Setenv("LOCK_DIR", limitedLock)
	t.Setenv("OVERLAP", overlap)
	t.Setenv("STARTED_DIR", started)
	t.Setenv("RELEASE_FILE", release)
	if err := os.Mkdir(started, 0700); err != nil {
		t.Fatal(err)
	}
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now := time.Date(2026, 10, 20, 10, 34, 0, 0, loc)
	fc := newFakeClock(now)
	date := now.Format("2006-01-02")
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	events := []config.Event{{ID: "slow_a", Handler: "slow", Class: "limited"}, {ID: "slow_b", Handler: "slow", Class: "limited"}, {ID: "fast", Handler: "fast", Class: "other"}}
	c := config.Config{Events: events, Handlers: map[string]config.Handler{"slow": {Argv: []string{script}, EnvAllow: []string{"NAME", "LOCK_DIR", "OVERLAP", "STARTED_DIR", "RELEASE_FILE"}}, "fast": {Argv: []string{script}, EnvAllow: []string{"NAME", "LOCK_DIR", "OVERLAP", "STARTED_DIR", "RELEASE_FILE"}}}, Concurrency: map[string]json.RawMessage{"limited": json.RawMessage(`{"max":1}`)}}
	r := &liveRunner{c: c, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, boot: "test", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	setName := func(name string) { t.Setenv("NAME", name) }
	setName("slow_a")
	firstDone := make(chan struct{})
	go func() { r.execute(Instance{ID: "slow_a", EventID: "slow_a"}, date); close(firstDone) }()
	waitFor(t, func() bool { _, err := os.Stat(filepath.Join(started, "slow_a")); return err == nil })
	setName("fast")
	t.Setenv("LOCK_DIR", fastLock)
	r.execute(Instance{ID: "fast", EventID: "fast"}, date)
	if got := runtimeTestState(r, date+"/fast").Status; got != "SUCCEEDED" {
		t.Fatalf("fast event status=%s while slow handler is running", got)
	}
	setName("slow_b")
	t.Setenv("LOCK_DIR", limitedLock)
	secondDone := make(chan struct{})
	go func() { r.execute(Instance{ID: "slow_b", EventID: "slow_b"}, date); close(secondDone) }()
	if _, err = os.Stat(filepath.Join(started, "slow_b")); err == nil {
		t.Fatal("second event in limited class started before first released its slot")
	}
	if err = os.WriteFile(release, nil, 0600); err != nil {
		t.Fatal(err)
	}
	select {
	case <-firstDone:
	case <-time.After(3 * time.Second):
		t.Fatal("first handler did not finish")
	}
	select {
	case <-secondDone:
	case <-time.After(3 * time.Second):
		t.Fatal("second handler did not finish")
	}
	if _, err = os.Stat(overlap); err == nil {
		t.Fatal("same-class handlers overlapped despite max=1")
	}
}

func TestRetryCountAndNoRetryOnTimeout(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	calls := filepath.Join(root, "calls")
	retryScript := filepath.Join(root, "retry.sh")
	if err := os.WriteFile(retryScript, []byte("#!/bin/sh\nn=0; [ -f \"$CALLS\" ] && n=$(cat \"$CALLS\"); n=$((n+1)); echo $n > \"$CALLS\"\nif [ $n -lt 3 ]; then printf '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"TEMP\",\"retryable\":true}\\n'; else printf '{\"contract\":1,\"status\":\"ok\"}\\n'; fi\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CALLS", calls)
	timeoutScript := filepath.Join(root, "timeout.sh")
	if err := os.WriteFile(timeoutScript, []byte("#!/bin/sh\ntrap '' TERM\nwhile :; do :; done\n"), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	events := []config.Event{{ID: "retry", Handler: "retry", Retry: config.Retry{MaxAttempts: 3, Base: "0s"}}, {ID: "timeout", Handler: "timeout", Timeout: "20ms", Retry: config.Retry{MaxAttempts: 4, NoRetryOnTimeout: true}}}
	r := &liveRunner{c: config.Config{Events: events, Handlers: map[string]config.Handler{"retry": {Argv: []string{retryScript}, EnvAllow: []string{"CALLS"}}, "timeout": {Argv: []string{timeoutScript}}}}, root: root, loc: loc, start: now, clock: newFakeClock(now), ledger: l, boot: "test", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	r.execute(Instance{ID: "retry", EventID: "retry"}, date)
	if got := runtimeTestState(r, date+"/retry"); got.Status != "SUCCEEDED" || got.Attempts != 3 {
		t.Fatalf("retry result=%+v, want success on 3 attempts", got)
	}
	r.execute(Instance{ID: "timeout", EventID: "timeout"}, date)
	if got := runtimeTestState(r, date+"/timeout"); got.Attempts != 1 || got.LastError != "HANDLER_TIMEOUT" {
		t.Fatalf("no_retry_on_timeout result=%+v, want one timed out attempt", got)
	}
}

func TestRetryBackoffIsExponentialPerAttemptAndCapped(t *testing.T) {
	if got := retryBackoff("60s", 3, 3); got != 4*time.Minute {
		t.Fatalf("attempt 3 delay=%s, want 4m", got)
	}
	if got := retryBackoff("60s", 2, 3); got != 2*time.Minute {
		t.Fatalf("attempt 2 delay=%s, want 2m", got)
	}
	if got := retryBackoff("10m", 100, 1); got != 10*time.Minute {
		t.Fatalf("capped delay=%s, want 10m", got)
	}
}

func TestUntilLateTotalAttemptsAreCappedAcrossRestarts(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	script := filepath.Join(root, "always-fail.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"TEMP\",\"retryable\":true}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "capped", Handler: "h", Trigger: config.Trigger{Type: "window", Start: "10:30:00", Deadline: "10:34:30"}, LateUntil: "11:00:00", Retry: config.Retry{MaxAttempts: 3, UntilLate: true, Backoff: "60s"}}
	x := Instance{ID: e.ID, EventID: e.ID, Start: "10:30:00", Deadline: "10:34:30", LateUntil: e.LateUntil}
	fc := newFakeClock(now)
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, boot: "test", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	r.execute(x, date)
	waiting := runtimeTestState(r, date+"/"+e.ID)
	if waiting.Status != "RETRY_WAIT" || waiting.Attempts != 1 || waiting.NextAttemptAt != now.Add(time.Minute).Format(time.RFC3339) {
		t.Fatalf("first retry state=%+v", waiting)
	}
	// Rebuild state from the ledger to prove the cumulative attempt count survives restart.
	r.states = map[string]instanceState{}
	if err := r.recover(); err != nil {
		t.Fatal(err)
	}
	if recovered := runtimeTestState(r, date+"/"+e.ID); recovered.Attempts != 1 || recovered.NextAttemptAt != waiting.NextAttemptAt {
		t.Fatalf("recovered retry state=%+v", recovered)
	}
	fc.Advance(time.Minute)
	r.execute(x, date)
	second := runtimeTestState(r, date+"/"+e.ID)
	if second.Status != "RETRY_WAIT" || second.Attempts != 2 || second.NextAttemptAt != now.Add(3*time.Minute).Format(time.RFC3339) {
		t.Fatalf("second retry state=%+v, want due at +3m", second)
	}
	fc.Advance(2 * time.Minute)
	r.execute(x, date)
	final := runtimeTestState(r, date+"/"+e.ID)
	if final.Status != "FAILED_FINAL" || final.Attempts != 3 || final.LastError != "RETRIES_EXHAUSTED" || r.alertDaily != 1 {
		t.Fatalf("exhausted state=%+v alerts=%d", final, r.alertDaily)
	}
}

func TestRetryBackoffIsExponentialPerAttempt(t *testing.T) {
	if got := retryBackoff("60s", 1, 3); got != time.Minute {
		t.Fatalf("attempt 1 delay=%s", got)
	}
	if got := retryBackoff("60s", 2, 3); got != 2*time.Minute {
		t.Fatalf("attempt 2 delay=%s, want 2m", got)
	}
}

func TestUntilLateSuccessOnSecondAttemptAndSilentExhaustion(t *testing.T) {
	t.Run("success on second attempt", func(t *testing.T) {
		root, date := t.TempDir(), "2026-10-20"
		loc, _ := time.LoadLocation("Asia/Shanghai")
		now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
		calls := filepath.Join(root, "calls")
		script := filepath.Join(root, "fail-once.sh")
		body := `#!/bin/sh
n=0; [ -f "$CALLS" ] && n=$(cat "$CALLS"); n=$((n+1)); echo $n > "$CALLS"
if [ $n -eq 1 ]; then printf '%s\n' '{"contract":1,"status":"failed","error_code":"TEMP","retryable":true}'; else printf '%s\n' '{"contract":1,"status":"ok"}'; fi
`
		if err := os.WriteFile(script, []byte(body), 0700); err != nil {
			t.Fatal(err)
		}
		t.Setenv("CALLS", calls)
		l, err := openLedger(root, date)
		if err != nil {
			t.Fatal(err)
		}
		defer l.close()
		e := config.Event{ID: "success", Handler: "h", LateUntil: "11:00:00", Retry: config.Retry{MaxAttempts: 3, UntilLate: true, Backoff: "60s"}}
		fc := newFakeClock(now)
		r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}, EnvAllow: []string{"CALLS"}}}}, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
		x := Instance{ID: e.ID, EventID: e.ID, Start: "10:30:00", Deadline: "10:34:30", LateUntil: e.LateUntil}
		r.execute(x, date)
		fc.Advance(time.Minute)
		r.execute(x, date)
		if got := runtimeTestState(r, date+"/"+e.ID); got.Status != "SUCCEEDED" || got.Attempts != 2 {
			t.Fatalf("second attempt result=%+v", got)
		}
	})

	t.Run("silent checkpoint exhausts without alert", func(t *testing.T) {
		root, date := t.TempDir(), "2026-10-20"
		loc, _ := time.LoadLocation("Asia/Shanghai")
		now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
		script := filepath.Join(root, "always-fail.sh")
		if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"TEMP\",\"retryable\":true}'\n"), 0700); err != nil {
			t.Fatal(err)
		}
		l, err := openLedger(root, date)
		if err != nil {
			t.Fatal(err)
		}
		defer l.close()
		e := config.Event{ID: "silent", Handler: "h", LateUntil: "11:00:00", Retry: config.Retry{MaxAttempts: 3, UntilLate: true, Backoff: "1s", SilentUntilLast: true}}
		fc := newFakeClock(now)
		r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
		x := Instance{ID: e.ID, EventID: e.ID, Start: "10:30:00", Deadline: "10:34:30", LateUntil: e.LateUntil}
		for i, advance := range []time.Duration{0, time.Minute, 2 * time.Minute} {
			if advance > 0 {
				fc.Advance(advance)
			}
			r.execute(x, date)
			if i < 2 && runtimeTestState(r, date+"/"+e.ID).Status != "RETRY_WAIT" {
				t.Fatalf("checkpoint %d state=%+v", i+1, runtimeTestState(r, date+"/"+e.ID))
			}
		}
		if got := runtimeTestState(r, date+"/"+e.ID); got.Status != "FAILED_FINAL" || got.Attempts != 3 || got.LastError != "RETRIES_EXHAUSTED" || r.alertDaily != 0 {
			t.Fatalf("silent exhausted state=%+v alerts=%d", got, r.alertDaily)
		}
	})
}

func TestEventRetryRealClockUsesTwoAndFourSecondBackoff(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	script := filepath.Join(root, "fail-and-record.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\ndate +%s%N >> \"$TIMES\"\nprintf '%s\\n' '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"TEMP\",\"retryable\":true}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TIMES", filepath.Join(root, "times"))
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	start := time.Now()
	e := config.Event{ID: "real_retry", Handler: "h", Retry: config.Retry{MaxAttempts: 3, Base: "2s"}}
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}, EnvAllow: []string{"TIMES"}}}}, root: root, loc: time.Local, start: start, clock: realClock{}, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	r.execute(Instance{ID: e.ID, EventID: e.ID, TriggerType: "startup"}, date)
	if got := runtimeTestState(r, date+"/"+e.ID); got.Status != "FAILED_FINAL" || got.Attempts != 3 {
		t.Fatalf("real retry result=%+v", got)
	}
	b, err := os.ReadFile(filepath.Join(root, "times"))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Fields(string(b))
	if len(lines) != 3 {
		t.Fatalf("recorded attempts=%d, want 3", len(lines))
	}
	var at [3]int64
	for i, line := range lines {
		at[i], err = strconv.ParseInt(line, 10, 64)
		if err != nil {
			t.Fatal(err)
		}
	}
	for i, bounds := range [][2]int64{{1_800_000_000, 3_500_000_000}, {3_800_000_000, 5_500_000_000}} {
		if delta := at[i+1] - at[i]; delta < bounds[0] || delta > bounds[1] {
			t.Fatalf("attempt interval %d=%dns, expected approximately %d..%dns", i+1, delta, bounds[0], bounds[1])
		}
	}
	t.Logf("real handler retries: attempts=3 intervals=%s,%s", time.Duration(at[1]-at[0]), time.Duration(at[2]-at[1]))
}

func TestRetryUntilLatePersistsWaitAndSucceedsWithoutAlert(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	calls := filepath.Join(root, "until-late-calls")
	script := filepath.Join(root, "until-late.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nn=0; [ -f \"$CALLS\" ] && n=$(cat \"$CALLS\"); n=$((n+1)); echo $n > \"$CALLS\"\nif [ $n -lt 3 ]; then printf '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"TEMP\",\"retryable\":true}\\n'; else printf '{\"contract\":1,\"status\":\"ok\"}\\n'; fi\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CALLS", calls)
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "until_late", Handler: "h", LateUntil: "10:35:00", Retry: config.Retry{MaxAttempts: 3, UntilLate: true, Backoff: "1s"}}
	x := Instance{ID: e.ID, EventID: e.ID, Start: "10:30:00", Deadline: "10:34:30", LateUntil: e.LateUntil, MaxAttempts: 3}
	fc := newFakeClock(now)
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}, EnvAllow: []string{"CALLS"}}}}, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, boot: "test", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	r.execute(x, date)
	waiting := runtimeTestState(r, date+"/"+e.ID)
	if waiting.Status != "RETRY_WAIT" || waiting.Attempts != 1 || waiting.LastError != "TEMP" {
		t.Fatalf("first attempt state=%+v, want persisted retry wait", waiting)
	}
	if waiting.NextAttemptAt == "" {
		t.Fatal("retry wait omitted next attempt time")
	}
	if r.alertDaily != 0 {
		t.Fatalf("retry wait emitted %d alerts", r.alertDaily)
	}
	contents, err := os.ReadFile(filepath.Join(root, "ledger", date+".jsonl"))
	if err != nil || !strings.Contains(string(contents), `"type":"FAILURE_OBSERVED"`) || !strings.Contains(string(contents), `"error_code":"TEMP"`) {
		t.Fatalf("failure observation missing from ledger: err=%v contents=%s", err, contents)
	}
	r.mu.Lock()
	delete(r.states, date+"/"+e.ID)
	r.mu.Unlock()
	if err := r.recover(); err != nil {
		t.Fatal(err)
	}
	if recovered := runtimeTestState(r, date+"/"+e.ID); recovered.Status != "RETRY_WAIT" || recovered.NextAttemptAt != waiting.NextAttemptAt {
		t.Fatalf("recovered retry state=%+v, want persisted wait at %s", recovered, waiting.NextAttemptAt)
	}
	fc.Advance(time.Second)
	r.execute(x, date)
	if got := runtimeTestState(r, date+"/"+e.ID); got.Status != "RETRY_WAIT" || got.Attempts != 2 {
		t.Fatalf("second attempt state=%+v, want another retry wait", got)
	}
	if got := runtimeTestState(r, date+"/"+e.ID).NextAttemptAt; got != now.Add(3*time.Second).Format(time.RFC3339) {
		t.Fatalf("second retry due=%s, want exponential due=%s", got, now.Add(3*time.Second).Format(time.RFC3339))
	}
	fc.Advance(2 * time.Second)
	r.execute(x, date)
	if got := runtimeTestState(r, date+"/"+e.ID); got.Status != "SUCCEEDED" || got.Attempts != 3 {
		t.Fatalf("third attempt state=%+v, want success", got)
	}
	if r.alertDaily != 0 {
		t.Fatalf("successful retry emitted %d alerts", r.alertDaily)
	}
}

func TestRetryUntilLateExpiryFinalizesAndAlerts(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:59+08:00", loc)
	script := filepath.Join(root, "always-fail.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"TEMP\",\"retryable\":true}\\n'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "expiry", Handler: "h", Trigger: config.Trigger{Type: "window", Start: "10:30:00", Deadline: "10:34:00"}, LateUntil: "10:35:00", Retry: config.Retry{MaxAttempts: 3, UntilLate: true, Backoff: "60s"}}
	fc := newFakeClock(now)
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	xs, _, err := instances(r.c, date)
	if err != nil || len(xs) != 1 {
		t.Fatalf("instances=%v err=%v", xs, err)
	}
	r.execute(xs[0], date)
	if got := runtimeTestState(r, date+"/expiry").Status; got != "RETRY_WAIT" {
		t.Fatalf("before late_until status=%s, want RETRY_WAIT", got)
	}
	fc.Advance(time.Second)
	r.schedule(fc.Now())
	got := runtimeTestState(r, date+"/expiry")
	if got.Status != "FAILED_FINAL" || got.LastError != "RETRIES_EXHAUSTED_BY_LATE_UNTIL" || r.alertDaily != 1 {
		t.Fatalf("expiry state=%+v alerts=%d, want final reason and one alert", got, r.alertDaily)
	}
}

func TestNonRetryableFailureIsImmediatelyFinal(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	script := filepath.Join(root, "permanent.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nprintf '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"BAD_INPUT\",\"retryable\":false}\\n'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "permanent", Handler: "h", LateUntil: "10:35:00", Retry: config.Retry{MaxAttempts: 1, UntilLate: true, Backoff: "1s"}}
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}, root: root, loc: loc, start: now, clock: newFakeClock(now), ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	r.execute(Instance{ID: e.ID, EventID: e.ID, Start: "10:30:00", Deadline: "10:34:30", LateUntil: e.LateUntil}, date)
	got := runtimeTestState(r, date+"/"+e.ID)
	if got.Status != "FAILED_FINAL" || got.Attempts != 1 || got.LastError != "BAD_INPUT" || r.alertDaily != 1 {
		t.Fatalf("non-retryable state=%+v alerts=%d, want immediate final and one alert", got, r.alertDaily)
	}
}

func TestGateDeadlineAndDailySummary(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	gateScript := filepath.Join(root, "gate.sh")
	calls := filepath.Join(root, "gate-calls")
	if err := os.WriteFile(gateScript, []byte("#!/bin/sh\necho x >> \"$GATE_CALLS\"\nprintf '{\"status\":\"not_ready\",\"reason\":\"source_stale\",\"row_count\":17,\"elapsed_ms\":8,\"query_window\":[\"09:33:00\",\"09:33:03\"]}\\n'\nexit 75\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GATE_CALLS", calls)
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "gated", Handler: "h", Timeout: "60s", Gate: config.Gate{Ref: "ready", Poll: "10s", Timeout: "100ms"}}
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {GateArgv: []string{gateScript}, EnvAllow: []string{"GATE_CALLS"}}, "alert": {Argv: []string{"/bin/true"}}}}, root: root, loc: loc, start: now, clock: newFakeClock(now), fake: newFakeClock(now), ledger: l, boot: "test", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	// Keep the runner clock and its wait clock on the same fake source.
	r.clock = r.fake
	done := make(chan struct{})
	go func() {
		r.execute(Instance{ID: e.ID, EventID: e.ID, Start: "10:30:00", Deadline: "10:40:00"}, date)
		close(done)
	}()
	waitFor(t, func() bool { _, err := os.Stat(calls); return err == nil })
	waitFor(t, func() bool { return r.fake.waitCount() > 0 })
	r.fake.Advance(10 * time.Second)
	waitFor(t, func() bool { return lineCount(t, calls) >= 2 })
	waitFor(t, func() bool { return r.fake.waitCount() >= 2 })
	if count := lineCount(t, calls); count < 2 {
		t.Fatalf("gate did not poll again inside its window: calls=%d", count)
	}
	if count := r.fake.waitCount(); count < 2 {
		t.Fatalf("gate did not wait for its next in-window poll: waits=%d", count)
	}
	r.fake.Advance(10 * time.Minute)
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("gate handler did not finish after deadline advance")
	}
	if got := runtimeTestState(r, date+"/gated").Status; got != "GATE_BLOCKED" {
		t.Fatalf("gate status=%s, want GATE_BLOCKED", got)
	}
	if got := runtimeTestState(r, date+"/gated").LastError; got != "source_stale" {
		t.Fatalf("gate reason=%q, want source_stale", got)
	}
	_ = r.alert("GATE_BLOCKED", map[string]any{"events": []string{"gated"}, "summary": true})
	_ = r.alert("GATE_BLOCKED", map[string]any{"events": []string{"gated"}, "summary": true})
	counts := ledgerTypeCounts(t, root, date)
	if counts["GATE_POLL"] != 1 || counts["GATE_POLL_SUMMARY"] != 1 {
		t.Fatalf("gate poll ledger counts=%v, want one deduplicated poll and summary", counts)
	}
	ledgerBytes := mustRead(t, filepath.Join(root, "ledger", date+".jsonl"))
	foundPoll := false
	foundSummary := false
	for _, line := range strings.Split(strings.TrimSpace(string(ledgerBytes)), "\n") {
		var rec ledgerRecord
		_ = json.Unmarshal([]byte(line), &rec)
		b, _ := json.Marshal(rec.Data)
		var data map[string]any
		_ = json.Unmarshal(b, &data)
		if rec.Type == "GATE_POLL" {
			foundPoll = data["reason"] == "source_stale" && data["row_count"] == float64(17) && data["exit_code"] == float64(75)
		}
		if rec.Type == "GATE_POLL_SUMMARY" {
			foundSummary = data["poll_count"] == float64(2) && data["outcome"] == "blocked"
		}
	}
	if !foundPoll {
		t.Fatal("deduplicated GATE_POLL omitted child reason, row_count, or failed exit")
	}
	if !foundSummary {
		t.Fatal("gate poll summary omitted repeated poll count or blocked outcome")
	}
	if counts["ALERT_OUT"] != 2 {
		t.Fatalf("gate first and daily summary alerts=%d, want two", counts["ALERT_OUT"])
	}
	ledgerBytes = mustRead(t, filepath.Join(root, "ledger", date+".jsonl"))
	foundFirst := false
	for _, line := range strings.Split(strings.TrimSpace(string(ledgerBytes)), "\n") {
		var rec ledgerRecord
		_ = json.Unmarshal([]byte(line), &rec)
		if rec.Type != "ALERT_OUT" {
			continue
		}
		encoded, _ := json.Marshal(rec.Data)
		var alert struct {
			Reason  string `json:"reason"`
			Request struct {
				Event      string `json:"event"`
				Handler    string `json:"handler"`
				GateReason string `json:"gate_reason"`
			} `json:"request"`
		}
		_ = json.Unmarshal(encoded, &alert)
		if alert.Reason == "GATE_BLOCKED_FIRST" {
			foundFirst = alert.Request.Event == "gated" && alert.Request.Handler == "h" && alert.Request.GateReason == "source_stale"
		}
	}
	if !foundFirst {
		t.Fatal("first gate alert omitted event, handler, or gate reason")
	}
	_ = l.close()
	l2, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l2.close()
	restarted := &liveRunner{c: r.c, root: root, loc: loc, start: now, clock: r.clock, ledger: l2, states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	if err = restarted.recover(); err != nil {
		t.Fatal(err)
	}
	_ = restarted.alert("GATE_BLOCKED", map[string]any{"events": []string{"gated"}, "summary": true})
	if got := ledgerTypeCounts(t, root, date)["ALERT_OUT"]; got != 2 {
		t.Fatalf("gate summary repeated after restart: alerts=%d", got)
	}
}

func TestRealDataGatePollingAgainstCurrentConfig(t *testing.T) {
	configPath := os.Getenv("GUARDIAN_REAL_CONFIG")
	if configPath == "" {
		t.Skip("GUARDIAN_REAL_CONFIG not set; run explicitly against the read-only production config")
	}
	c, _, _, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	eventID := os.Getenv("GUARDIAN_REAL_EVENT")
	if eventID == "" {
		eventID = "intraday_33"
	}
	var event *config.Event
	for i := range c.Events {
		if c.Events[i].ID == eventID {
			event = &c.Events[i]
			break
		}
	}
	if event == nil || event.Gate.Ref == "" {
		t.Fatalf("real config has no gated %s event", eventID)
	}
	var postmarket *config.Event
	for i := range c.Events {
		if c.Events[i].ID == "postmarket_publish" {
			postmarket = &c.Events[i]
			break
		}
	}
	if postmarket == nil || postmarket.Retry.MaxAttempts != 3 || !postmarket.Retry.UntilLate {
		t.Fatalf("real postmarket retry policy=%+v, want max_attempts=3 until_late=true", postmarket)
	}
	root := t.TempDir()
	noop := filepath.Join(root, "isolated-handler.sh")
	if err := os.WriteFile(noop, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	// Keep the configured real gate command and its read-only inputs. Prevent
	// the downstream fetch and alert handlers from producing external effects.
	h := c.Handlers[event.Handler]
	h.Argv = []string{noop}
	c.Handlers[event.Handler] = h
	alert := c.Handlers["alert"]
	alert.Argv = nil
	c.Handlers["alert"] = alert
	loc, err := time.LoadLocation(c.Timezone)
	if err != nil {
		t.Fatal(err)
	}
	date := os.Getenv("GUARDIAN_REAL_DATE")
	if date == "" {
		date = "2026-10-21"
	}
	checkpoint := event.Trigger.Start
	if checkpoint == "" {
		checkpoint = "09:33:00"
	}
	now, err := time.ParseInLocation(time.RFC3339, date+"T"+checkpoint+"+08:00", loc)
	if err != nil {
		t.Fatal(err)
	}
	now = now.Add(5 * time.Second)
	fc := newFakeClock(now)
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, boot: "real-data-gate", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	x := Instance{ID: event.ID, EventID: event.ID, Start: event.Trigger.Start, Deadline: event.Trigger.Deadline, LateUntil: event.LateUntil, TriggerType: event.Trigger.Type}
	done := make(chan struct{})
	go func() { r.execute(x, date); close(done) }()
	deadline := time.Now().Add(20 * time.Second)
	advanced := false
	for {
		select {
		case <-done:
			goto finished
		default:
		}
		if !advanced && fc.waitCount() > 0 {
			fc.Advance(60 * time.Second)
			advanced = true
		}
		if time.Now().After(deadline) {
			t.Fatal("real-data gate command did not return within its configured timeout")
		}
		time.Sleep(time.Millisecond)
	}

finished:
	state := runtimeTestState(r, date+"/"+event.ID)
	if state.Status != "GATE_BLOCKED" && state.Status != "SUCCEEDED" {
		t.Fatalf("real-data gate returned unexpected state: %+v", state)
	}
	if state.Status == "GATE_BLOCKED" && state.LastError == "" {
		t.Fatal("real-data gate block omitted its final reason")
	}
	ledgerOutput := mustRead(t, filepath.Join(root, "ledger", date+".jsonl"))
	for _, line := range strings.Split(strings.TrimSpace(string(ledgerOutput)), "\n") {
		if strings.Contains(line, `"type":"GATE_POLL"`) || strings.Contains(line, `"type":"GATE_POLL_SUMMARY"`) || strings.Contains(line, `"type":"GATE_READY"`) {
			t.Logf("ledger_record=%s", line)
		}
	}
	t.Logf("real_config=%s event=%s gate=%s status=%s reason=%q postmarket_max_attempts=%d until_late=%t temp_state_root=%s", configPath, event.ID, event.Gate.Ref, state.Status, state.LastError, postmarket.Retry.MaxAttempts, postmarket.Retry.UntilLate, root)
}

func lineCount(t *testing.T, path string) int {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	return len(strings.Fields(string(b)))
}

func TestSocketErrorsIdempotencyAndInvalidReloadPreservesRunner(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	cfgPath := filepath.Join(root, "config.json")
	c := config.Config{SchemaVersion: 1, ConfigVersion: 1, Name: "initial", Timezone: "Asia/Shanghai", Events: []config.Event{{ID: "evt", Trigger: config.Trigger{Type: "window", Start: "10:30:00", Deadline: "10:40:00"}}}}
	writeConfig := func(value any) {
		b, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(cfgPath, b, 0600); err != nil {
			t.Fatal(err)
		}
	}
	writeConfig(c)
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	r := &liveRunner{c: c, root: root, cfgPath: cfgPath, loc: loc, start: now, clock: newFakeClock(now), ledger: l, states: map[string]instanceState{}, running: map[string]bool{"evt": true}, paused: map[string]bool{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}, idempotency: map[string]any{}}
	resp := socketServeRequest(t, r, `{"t":"req","id":"1","op":"pause","args":{"event":"evt"},"idem":"pause-once"}`)
	if resp["ok"] != true {
		t.Fatalf("pause response=%v", resp)
	}
	resp = socketServeRequest(t, r, `{"t":"req","id":"2","op":"pause","args":{"event":"missing"}}`)
	if errBody(resp)["code"] != "UNKNOWN_EVENT" {
		t.Fatalf("unknown event error=%v", resp)
	}
	resp = socketServeRequest(t, r, `{"t":"req","id":"3","op":"no_such_op"}`)
	if errBody(resp)["code"] != "BAD_REQUEST" {
		t.Fatalf("unknown operation error=%v", resp)
	}
	if err = os.WriteFile(cfgPath, []byte(`{"schema_version":1,"config_version":2,"name":"broken","timezone":"Asia/Shanghai","events":[],"unknown":true}`), 0600); err != nil {
		t.Fatal(err)
	}
	resp = socketServeRequest(t, r, `{"t":"req","id":"4","op":"reload"}`)
	running := runtimeTestRunning(r, "evt")
	if errBody(resp)["code"] != "CONFIG_INVALID" || r.c.ConfigVersion != 1 || !running {
		t.Fatalf("invalid reload changed live state: response=%v version=%d running=%v", resp, r.c.ConfigVersion, running)
	}
	_ = l.close()
	l2, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l2.close()
	restarted := &liveRunner{c: c, root: root, cfgPath: cfgPath, loc: loc, start: now, clock: newFakeClock(now), ledger: l2, states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}, idempotency: map[string]any{}}
	if err = restarted.recover(); err != nil {
		t.Fatal(err)
	}
	resp = socketServeRequest(t, restarted, `{"t":"req","id":"5","op":"pause","args":{"event":"evt"},"idem":"pause-once"}`)
	if resp["ok"] != true || !restarted.paused["evt"] {
		t.Fatalf("persisted idempotency/pause missing after restart: response=%v paused=%v", resp, restarted.paused)
	}
}

func TestKill9RestartRestoresAttemptsMissedSummaryAndSocketIdem(t *testing.T) {
	root := t.TempDir()
	bin := filepath.Join(root, "guardiand")
	build := exec.Command("go", "build", "-o", bin, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build guardian subprocess: %v: %s", err, out)
	}
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	stateRoot := filepath.Join(root, "state")
	countFile := filepath.Join(root, "handler-count")
	firstPID := filepath.Join(root, "handler-pid")
	marker := filepath.Join(root, "handler-started")
	release := filepath.Join(root, "handler-release")
	requests := filepath.Join(root, "handler-requests.jsonl")
	handler := filepath.Join(root, "handler.sh")
	body := "#!/bin/sh\ncat >> \"$REQUESTS\"\nn=0; [ -f \"$COUNT_FILE\" ] && n=$(cat \"$COUNT_FILE\"); n=$((n+1)); echo $n > \"$COUNT_FILE\"\nif [ $n -eq 1 ]; then echo $$ > \"$PID_FILE\"; touch \"$MARKER\"; while [ ! -f \"$RELEASE_FILE\" ]; do :; done; fi\nprintf '{\"contract\":1,\"status\":\"ok\"}\\n'\n"
	for name, value := range map[string]string{"COUNT_FILE": countFile, "PID_FILE": firstPID, "MARKER": marker, "RELEASE_FILE": release, "REQUESTS": requests} {
		t.Setenv(name, value)
	}
	if err := os.WriteFile(handler, []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	cfgPath := filepath.Join(root, "config.json")
	c := config.Config{SchemaVersion: 1, ConfigVersion: 1, Name: "kill9", Timezone: "Asia/Shanghai", Handlers: map[string]config.Handler{"h": {Argv: []string{handler}, EnvAllow: []string{"COUNT_FILE", "PID_FILE", "MARKER", "RELEASE_FILE", "REQUESTS"}}, "alert": {Argv: []string{"/bin/true"}}}, Events: []config.Event{
		{ID: "missed", Enabled: true, Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:01:00"}},
		{ID: "active", Enabled: true, Handler: "h", Timeout: "5m", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "10:30:00", Deadline: "10:40:00"}, Retry: config.Retry{MaxAttempts: 3}},
	}}
	configBytes, err := json.Marshal(c)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(cfgPath, configBytes, 0600); err != nil {
		t.Fatal(err)
	}
	start := func() *exec.Cmd {
		cmd := exec.Command(bin, "run", "--config", cfgPath, "--state-root", stateRoot, "--mode=fakeclock", "--clock-start", now.Format(time.RFC3339))
		cmd.Stdout, cmd.Stderr = io.Discard, io.Discard
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if cmd.ProcessState == nil && cmd.Process != nil {
				_ = cmd.Process.Kill()
				_ = cmd.Wait()
			}
		})
		return cmd
	}
	stop := func(cmd *exec.Cmd) {
		if err := cmd.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
			t.Fatal(err)
		}
		_ = cmd.Wait()
	}
	first := start()
	waitFor(t, func() bool { _, e := os.Stat(marker); return e == nil })
	pidBytes, err := os.ReadFile(firstPID)
	if err != nil {
		t.Fatal(err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(pidBytes)))
	if err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(stateRoot, "guardiand.sock")
	waitFor(t, func() bool { return socketReady(socket) })
	info, err := os.Stat(socket)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatalf("socket mode=%o, want 600", info.Mode().Perm())
	}
	if response := unixRequest(t, socket, `{"t":"req","id":"bad","op":"unknown"}`); errBody(response)["code"] != "BAD_REQUEST" {
		t.Fatalf("socket error response=%v", response)
	}
	idemRequest := `{"t":"req","id":"pause","op":"pause","args":{"event":"active"},"idem":"persisted-pause"}`
	if response := unixRequest(t, socket, idemRequest); response["ok"] != true {
		t.Fatalf("initial idem request=%v", response)
	}
	triggerRequest := fmt.Sprintf(`{"t":"req","id":"trigger","op":"trigger","args":{"event":"active","trade_date":%q},"idem":"persisted-trigger"}`, date)
	if response := unixRequest(t, socket, triggerRequest); response["ok"] != true {
		t.Fatalf("initial trigger idempotency request=%v", response)
	}
	stop(first)
	_ = syscall.Kill(-pid, syscall.SIGKILL)

	second := start()
	waitFor(t, func() bool {
		b, e := os.ReadFile(countFile)
		return e == nil && strings.TrimSpace(string(b)) == "2"
	})
	waitFor(t, func() bool {
		b, e := os.ReadFile(filepath.Join(stateRoot, date, "state.json"))
		if e != nil {
			return false
		}
		var doc struct {
			Events map[string]instanceState `json:"events"`
		}
		return json.Unmarshal(b, &doc) == nil && doc.Events["active"].Status == "SUCCEEDED"
	})
	if counts := ledgerTypeCounts(t, stateRoot, date); counts["ALERT_OUT"] != 1 {
		t.Fatalf("startup MISSED summary repeated after restart: ledger counts=%v", counts)
	}
	stop(second)

	third := start()
	waitFor(t, func() bool { return socketReady(socket) })
	if response := unixRequest(t, socket, idemRequest); response["ok"] != true {
		t.Fatalf("idempotent replay after restart=%v", response)
	}
	if response := unixRequest(t, socket, triggerRequest); response["ok"] != true {
		t.Fatalf("trigger idempotency replay after restart=%v", response)
	}
	stop(third)
	if b, err := os.ReadFile(countFile); err != nil || strings.TrimSpace(string(b)) != "2" {
		t.Fatalf("completed instance reran after restart: count=%q err=%v", b, err)
	}
	requestLines, err := os.ReadFile(requests)
	if err != nil {
		t.Fatal(err)
	}
	attempts := []int{}
	for _, line := range strings.Split(strings.TrimSpace(string(requestLines)), "\n") {
		var request struct {
			Attempt int `json:"attempt"`
		}
		if err = json.Unmarshal([]byte(line), &request); err != nil {
			t.Fatalf("invalid captured handler request %q: %v", line, err)
		}
		attempts = append(attempts, request.Attempt)
	}
	if len(attempts) != 2 || attempts[0] != 1 || attempts[1] != 2 {
		t.Fatalf("handler attempts after kill/restart=%v, want [1 2]", attempts)
	}
}

func TestDecodeResultKeepsHandlerDetailOnNonZeroExit(t *testing.T) {
	out := []byte(`{"contract":1,"status":"failed","error_code":"TYPEERROR","retryable":true,"detail":"ladder boom"}` + "\n")
	_, code, retry, detail := decodeResult(out, exec.Command("sh", "-c", "exit 75").Run())
	if code != "INTERNAL_ERROR" || !retry || detail != "TYPEERROR: ladder boom" {
		t.Fatalf("got %s %v %q", code, retry, detail)
	}
	status, _, _, objDetail := decodeResult([]byte(`{"contract":1,"status":"ok","error_code":"","retryable":false,"detail":{"plan":[]}}`+"\n"), nil)
	if status != "ok" || objDetail == "" {
		t.Fatalf("object detail must not break contract parsing: %s %q", status, objDetail)
	}
	if _, code, _, _ := decodeResult([]byte("garbage\n"), nil); code != "CONTRACT_INVALID" {
		t.Fatal(code)
	}
}

func TestDecodeResultAcceptsDegradedExitCode10(t *testing.T) {
	out := []byte("{\"contract\":1,\"status\":\"degraded\",\"error_code\":\"\",\"retryable\":false,\"detail\":\"fallback result\"}\n")
	status, code, retryable, detail := decodeResult(out, localExitError{code: 10, text: "exit status 10"})
	if status != "degraded" || code != "" || retryable || detail != "fallback result" {
		t.Fatalf("decodeResult=%q,%q,%t,%q", status, code, retryable, detail)
	}
}

func TestRecoveredRunningFetchItemIsRearmedWithoutCountingTheInterruptedAttempt(t *testing.T) {
	r := &liveRunner{states: map[string]instanceState{
		"2026-10-21/fetch_item:daily_kline": {Status: "RUNNING", Attempts: 3, DataDate: "2026-10-21", NextPhase: "POST_MARKET"},
		"2026-10-21/baostock_probe_1730":    {Status: "RUNNING", Attempts: 1},
	}}
	r.rearmInterruptedFetchItems()
	got := runtimeTestState(r, "2026-10-21/fetch_item:daily_kline")
	if got.Status != "DEFERRED" || got.Attempts != 2 || got.DataDate != "2026-10-21" || got.NextPhase != "" {
		t.Fatalf("fetch_item not rearmed: %+v", got)
	}
	if runtimeTestState(r, "2026-10-21/baostock_probe_1730").Status != "RUNNING" {
		t.Fatal("non fetch_item state must be left to the normal startup recovery")
	}
}

func TestFetchItemRetriesBackOffFromTheConfiguredBaseAndAlertsOnceWhenSpent(t *testing.T) {
	root, date := t.TempDir(), "2026-10-21"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T22:30:00+08:00", loc)
	fail := filepath.Join(root, "fail.sh")
	if err := os.WriteFile(fail, []byte("#!/bin/sh\nprintf '%s\\n' '{\"contract\":1,\"status\":\"failed\",\"error_code\":\"FETCH_JOB_FAILED\",\"retryable\":true}'\nexit 75\n"), 0700); err != nil {
		t.Fatal(err)
	}
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "fetch_item", Handler: "h", Timeout: "60s", Trigger: config.Trigger{Type: "startup"},
		Retry: config.Retry{MaxAttempts: 3, Backoff: "60s"}}
	fc := newFakeClock(now)
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{fail}}, "alert": {Argv: []string{"/bin/true"}}}}, root: root, loc: loc, start: now, clock: fc, fake: fc, ledger: l, boot: "test", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	done := make(chan struct{})
	go func() {
		r.execute(Instance{ID: "fetch_item:daily_kline", EventID: "fetch_item", Start: "00:00:00", Deadline: "23:59:59", TriggerType: "startup", DataDate: date}, date)
		close(done)
	}()
	for finished := false; !finished; {
		select {
		case <-done:
			finished = true
		case <-time.After(20 * time.Millisecond):
			fc.Advance(30 * time.Second)
		}
	}
	got := runtimeTestState(r, date+"/fetch_item:daily_kline")
	if got.Attempts != 3 || (got.Status != "CIRCUIT_OPEN" && got.Status != "FAILED_FINAL") {
		t.Fatalf("state=%+v, want three attempts then a terminal state", got)
	}
	if elapsed := fc.Now().Sub(now); elapsed < 3*time.Minute {
		t.Fatalf("retries took %v of fake time, want >= 60s+120s exponential backoff", elapsed)
	}
	if counts := ledgerTypeCounts(t, root, date); counts["ALERT_OUT"] != 1 {
		t.Fatalf("spent fetch_item alerts=%d, want exactly one", counts["ALERT_OUT"])
	}
}

func TestC545CatchupModeAndDeadlineBoundary(t *testing.T) {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	date := "2026-10-20"
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:05:00", loc)
	c := config.Config{Events: []config.Event{
		{ID: "run", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:10:00"}},
		{ID: "skip", Catchup: "skip", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:10:00"}},
	}}
	missed, active := startupClassify(c, now)
	if len(missed) != 1 || missed[0].ID != "skip" || len(active) != 1 || active[0].ID != "run" {
		t.Fatalf("catchup classification missed=%v active=%v", instanceIDs(missed), instanceIDs(active))
	}
	deadline, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:10:00", loc)
	missed, active = startupClassify(c, deadline)
	if len(missed) != 1 || missed[0].ID != "skip" || len(active) != 1 || active[0].ID != "run" {
		t.Fatalf("deadline boundary missed=%v active=%v", instanceIDs(missed), instanceIDs(active))
	}
	missed, active = startupClassify(c, deadline.Add(time.Second))
	if len(missed) != 2 || len(active) != 0 {
		t.Fatalf("past-deadline missed=%v active=%v", instanceIDs(missed), instanceIDs(active))
	}
}

func TestC545TradingCalendarExcludesOnlyCoveredDates(t *testing.T) {
	raw := json.RawMessage(`"trading_day"`)
	cal := map[string]bool{"2026-10-20": true, "2026-10-22": true}
	trading := calendarStatus(raw, "2026-10-20", cal, "test calendar (2026-10-20..2026-10-22)")
	covered := calendarStatus(raw, "2026-10-21", cal, "test calendar (2026-10-20..2026-10-22)")
	uncovered := calendarStatus(raw, "2026-10-19", cal, "test calendar (2026-10-20..2026-10-22)")
	unavailable := calendarStatus(raw, "2026-10-21", nil, "unavailable")
	if !strings.HasPrefix(trading, "INCLUDED") {
		t.Fatalf("covered trading date status=%q", trading)
	}
	if !strings.HasPrefix(covered, "EXCLUDED") {
		t.Fatalf("covered non-trading date status=%q", covered)
	}
	if !strings.HasPrefix(uncovered, "UNKNOWN") || !strings.HasPrefix(unavailable, "UNKNOWN") {
		t.Fatalf("uncovered/unavailable statuses=%q/%q", uncovered, unavailable)
	}
}

func TestC545DeadlineDegradeRequestAndMissedAction(t *testing.T) {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	date := "2026-10-20"
	start, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:00", loc)
	for _, tc := range []struct {
		name       string
		onDeadline string
		wantStatus string
		wantCall   bool
	}{
		{name: "degrade", onDeadline: "degrade", wantStatus: "SUCCEEDED_DEGRADED", wantCall: true},
		{name: "missed", onDeadline: "missed", wantStatus: "MISSED"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			fc := newFakeClock(start)
			input := filepath.Join(root, "handler-input.jsonl")
			handler := writeC545Script(t, filepath.Join(root, "handler.sh"), "cat >> '"+input+"'\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n")
			gate := writeC545Script(t, filepath.Join(root, "gate.sh"), "printf '%s\\n' '{\"status\":\"not_ready\",\"reason\":\"test_not_ready\"}'\n")
			event := config.Event{ID: "deadline", Handler: "h", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:00:01"}, Gate: config.Gate{Ref: "gate", Poll: "1s"}, OnDeadline: tc.onDeadline, Catchup: "run_in_window", Phases: []string{"any"}, Retry: config.Retry{MaxAttempts: 1}}
			c := config.Config{Events: []config.Event{event}, Handlers: map[string]config.Handler{"h": {Argv: []string{handler}, GateArgv: []string{gate}, SupportsDegrade: true}}}
			r, closeRunner := newC545Runner(t, root, date, fc, c)
			defer closeRunner()
			x := Instance{ID: event.ID, EventID: event.ID, Start: event.Trigger.Start, Deadline: event.Trigger.Deadline, Timeout: "2s", MaxAttempts: 1, Catchup: event.Catchup, OnDeadline: event.OnDeadline}
			done := make(chan struct{})
			go func() { r.execute(x, date); close(done) }()
			waitFor(t, func() bool { return fc.waitCount() > 0 })
			fc.Advance(time.Second)
			select {
			case <-done:
			case <-time.After(5 * time.Second):
				t.Fatal("deadline execution did not finish")
			}
			got := runtimeTestState(r, date+"/"+event.ID)
			if got.Status != tc.wantStatus {
				t.Fatalf("status=%s want=%s state=%+v", got.Status, tc.wantStatus, got)
			}
			b, err := os.ReadFile(input)
			called := err == nil && len(bytes.TrimSpace(b)) > 0
			if called != tc.wantCall {
				t.Fatalf("handler called=%v want=%v err=%v", called, tc.wantCall, err)
			}
			if tc.wantCall {
				var request map[string]any
				if err := json.Unmarshal(bytes.TrimSpace(b), &request); err != nil {
					t.Fatal(err)
				}
				if request["degrade_requested"] != true || got.Attempts != 1 {
					t.Fatalf("degrade request/attempts=%v/%d", request["degrade_requested"], got.Attempts)
				}
			}
		})
	}
}

func TestC545DeadlineDegradeHonorsLateRetryBackoff(t *testing.T) {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	date := "2026-10-20"
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:02", loc)
	root := t.TempDir()
	input := filepath.Join(root, "handler-input.jsonl")
	handler := writeC545Script(t, filepath.Join(root, "handler.sh"), "cat >> '"+input+"'\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n")
	event := config.Event{
		ID: "deadline_retry", Handler: "h", OnDeadline: "degrade", Catchup: "run_in_window",
		Trigger:   config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:00:01"},
		LateUntil: "09:01:00", Calendar: json.RawMessage(`"every_day"`), Phases: []string{"any"},
		Retry: config.Retry{MaxAttempts: 3, UntilLate: true, Backoff: "10s"},
	}
	fc := newFakeClock(now)
	r, closeRunner := newC545Runner(t, root, date, fc, config.Config{Events: []config.Event{event}, Handlers: map[string]config.Handler{"h": {Argv: []string{handler}, SupportsDegrade: true}}})
	defer closeRunner()
	x := Instance{ID: event.ID, EventID: event.ID, Start: event.Trigger.Start, Deadline: event.Trigger.Deadline, LateUntil: event.LateUntil, TriggerType: "window", Catchup: event.Catchup, OnDeadline: event.OnDeadline}
	next := now.Add(5 * time.Second)
	r.setState(x, date, instanceState{Status: "RETRY_WAIT", Attempts: 1, NextAttemptAt: next.Format(time.RFC3339), Late: true})
	r.schedule(now)
	time.Sleep(50 * time.Millisecond)
	if _, err := os.Stat(input); !os.IsNotExist(err) {
		t.Fatalf("late retry ran before NextAttemptAt, stat err=%v", err)
	}
	fc.Advance(5 * time.Second)
	r.schedule(fc.Now())
	waitFor(t, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.states[date+"/"+event.ID].Status == "SUCCEEDED_DEGRADED"
	})
	b, err := os.ReadFile(input)
	if err != nil {
		t.Fatal(err)
	}
	var request map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(b), &request); err != nil {
		t.Fatal(err)
	}
	if request["degrade_requested"] != true || request["attempt"] != float64(2) {
		t.Fatalf("late retry request=%v, want attempt 2 with degrade requested", request)
	}
}

func TestC545WindowDependenciesDeferAndPropagateFailure(t *testing.T) {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	date := "2026-10-20"
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:05:00", loc)
	root := t.TempDir()
	input := filepath.Join(root, "handler-input.jsonl")
	handler := writeC545Script(t, filepath.Join(root, "handler.sh"), "cat >> '"+input+"'\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n")
	c := config.Config{Events: []config.Event{
		{ID: "a", Handler: "h", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:10:00"}, Phases: []string{"any"}},
		{ID: "b", Handler: "h", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:10:00"}, DependsOn: []config.Dependency{{ID: "a"}}, Phases: []string{"any"}},
		{ID: "c", Handler: "h", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:10:00"}, DependsOn: []config.Dependency{{ID: "b"}}, Phases: []string{"any"}},
	}, Handlers: map[string]config.Handler{"h": {Argv: []string{handler}}}}
	fc := newFakeClock(now)
	r, closeRunner := newC545Runner(t, root, date, fc, c)
	defer closeRunner()
	r.setState(Instance{ID: "a", EventID: "a"}, date, instanceState{Status: "RUNNING", StartedAt: now.Format(time.RFC3339)})
	r.mu.Lock()
	r.running["a"] = true
	r.mu.Unlock()
	r.schedule(now)
	waitFor(t, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.states[date+"/b"].Status == "DEFERRED" && r.states[date+"/c"].Status == "DEFERRED"
	})
	r.setState(Instance{ID: "a", EventID: "a"}, date, instanceState{Status: "FAILED_FINAL", CompletedAt: now.Format(time.RFC3339)})
	r.mu.Lock()
	delete(r.running, "a")
	r.mu.Unlock()
	r.schedule(now)
	waitFor(t, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.states[date+"/b"].Status == "SKIPPED_DEP" && r.states[date+"/c"].Status == "SKIPPED_DEP"
	})
	if _, err := os.Stat(input); !os.IsNotExist(err) {
		t.Fatalf("dependent handler was called, stat err=%v", err)
	}
	if !isTerminal("SKIPPED_DEP") {
		t.Fatal("SKIPPED_DEP must be terminal for transitive dependencies")
	}
	ledger := mustRead(t, filepath.Join(root, "ledger", date+".jsonl"))
	if !strings.Contains(string(ledger), `"status":"SKIPPED_DEP"`) {
		t.Fatal("SKIPPED_DEP states were not written to the ledger")
	}

	t.Run("successful chain releases dependents", func(t *testing.T) {
		successRoot := t.TempDir()
		successInput := filepath.Join(successRoot, "handler-input.jsonl")
		successHandler := writeC545Script(t, filepath.Join(successRoot, "handler.sh"), "cat >> '"+successInput+"'\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n")
		successEvents := []config.Event{
			{ID: "a", Handler: "h", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:10:00"}, Phases: []string{"any"}},
			{ID: "b", Handler: "h", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:10:00"}, DependsOn: []config.Dependency{{ID: "a"}}, Phases: []string{"any"}},
			{ID: "c", Handler: "h", Catchup: "run_in_window", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:10:00"}, DependsOn: []config.Dependency{{ID: "b"}}, Phases: []string{"any"}},
		}
		successRunner, closeSuccessRunner := newC545Runner(t, successRoot, date, fc, config.Config{Events: successEvents, Handlers: map[string]config.Handler{"h": {Argv: []string{successHandler}}}})
		defer closeSuccessRunner()
		successRunner.setState(Instance{ID: "a", EventID: "a"}, date, instanceState{Status: "SUCCEEDED", CompletedAt: now.Format(time.RFC3339)})
		successRunner.schedule(now)
		waitFor(t, func() bool {
			successRunner.mu.Lock()
			defer successRunner.mu.Unlock()
			return successRunner.states[date+"/b"].Status == "SUCCEEDED"
		})
		successRunner.schedule(now)
		waitFor(t, func() bool {
			successRunner.mu.Lock()
			defer successRunner.mu.Unlock()
			return successRunner.states[date+"/c"].Status == "SUCCEEDED"
		})
		calls := mustRead(t, successInput)
		if strings.Count(string(calls), `"contract":1`) != 2 {
			t.Fatalf("successful dependency chain handler calls=%q", calls)
		}
	})
}

func instanceIDs(xs []Instance) []string {
	ids := make([]string, len(xs))
	for i, x := range xs {
		ids[i] = x.ID
	}
	return ids
}

func newC545Runner(t *testing.T, root, date string, fc *fakeClock, c config.Config) (*liveRunner, func()) {
	t.Helper()
	loc, _ := time.LoadLocation("Asia/Shanghai")
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
	return r, func() { _ = l.close() }
}

func writeC545Script(t *testing.T, path, body string) string {
	t.Helper()
	if err := os.WriteFile(path, []byte("#!/bin/sh\nset -eu\n"+body), 0700); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestC547LadderRetryExhaustionAdvancesAndUntilLateIsTierLocal(t *testing.T) {
	t.Run("retry exhaustion advances", func(t *testing.T) {
		root, date := t.TempDir(), "2026-10-20"
		loc, _ := time.LoadLocation("Asia/Shanghai")
		now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:00", loc)
		calls := filepath.Join(root, "calls.jsonl")
		script := writeC545Script(t, filepath.Join(root, "handler.sh"), `cat >> "$CALL_LOG"
n=$(wc -l < "$CALL_LOG")
if [ "$n" -le 2 ]; then printf '%s\n' '{"contract":1,"status":"failed","error_code":"TEMP","retryable":true}'; exit 75; fi
printf '%s\n' '{"contract":1,"status":"ok"}'
`)
		t.Setenv("CALL_LOG", calls)
		event := config.Event{ID: "ladder_retry", Handler: "h", Ladder: []config.LadderTier{
			{Name: "primary", Attempts: 2, Backoff: "1s*1", Timeout: "2s"},
			{Name: "fallback", Attempts: 1, Timeout: "2s"},
		}}
		fc := newFakeClock(now)
		r, closeRunner := newC545Runner(t, root, date, fc, config.Config{Events: []config.Event{event}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}, EnvAllow: []string{"CALL_LOG"}}}})
		defer closeRunner()
		advanced := make(chan struct{})
		go func() {
			for fc.waitCount() == 0 {
				runtime.Gosched()
			}
			fc.Advance(2 * time.Second)
			close(advanced)
		}()
		r.execute(Instance{ID: event.ID, EventID: event.ID, TriggerType: "startup"}, date)
		<-advanced
		state := runtimeTestState(r, date+"/"+event.ID)
		if state.Status != "SUCCEEDED_DEGRADED" || state.Attempts != 3 || state.Rung != 2 || state.Tier != "fallback" {
			t.Fatalf("ladder result=%+v, want fallback success after two primary failures", state)
		}
		if counts := ledgerTypeCounts(t, root, date); counts["TIER_EXHAUSTED"] != 1 {
			t.Fatalf("tier exhaustion ledger counts=%v", counts)
		}
		b, err := os.ReadFile(calls)
		if err != nil {
			t.Fatal(err)
		}
		var tiers []string
		for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
			var request struct {
				Tier     string `json:"tier"`
				Degraded bool   `json:"degraded"`
			}
			if err := json.Unmarshal([]byte(line), &request); err != nil {
				t.Fatal(err)
			}
			tiers = append(tiers, request.Tier)
			if request.Tier == "fallback" && !request.Degraded {
				t.Fatal("fallback request did not set degraded=true")
			}
		}
		if strings.Join(tiers, ",") != "primary,primary,fallback" {
			t.Fatalf("tier call order=%v", tiers)
		}
	})

	t.Run("late_until only stops this tier retries", func(t *testing.T) {
		root, date := t.TempDir(), "2026-10-20"
		loc, _ := time.LoadLocation("Asia/Shanghai")
		now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:59:58", loc)
		calls := filepath.Join(root, "calls.jsonl")
		script := writeC545Script(t, filepath.Join(root, "handler.sh"), `cat >> "$CALL_LOG"
n=$(wc -l < "$CALL_LOG")
if [ "$n" -eq 1 ]; then printf '%s\n' '{"contract":1,"status":"failed","error_code":"TEMP","retryable":true}'; exit 75; fi
printf '%s\n' '{"contract":1,"status":"ok"}'
`)
		t.Setenv("CALL_LOG", calls)
		event := config.Event{ID: "tier_until", Handler: "h", LateUntil: "10:00:00", Catchup: "run_in_window",
			Trigger: config.Trigger{Type: "window", Start: "09:59:00", Deadline: "10:00:30"}, Ladder: []config.LadderTier{
				{Name: "primary", Attempts: 3, Backoff: "3s*1", Timeout: "2s", Until: "late_until"},
				{Name: "fallback", Attempts: 1, Timeout: "2s"},
			}}
		fc := newFakeClock(now)
		r, closeRunner := newC545Runner(t, root, date, fc, config.Config{Events: []config.Event{event}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}, EnvAllow: []string{"CALL_LOG"}}}})
		defer closeRunner()
		x := Instance{ID: event.ID, EventID: event.ID, TriggerType: "window", Start: event.Trigger.Start,
			Deadline: event.Trigger.Deadline, LateUntil: event.LateUntil}
		r.execute(x, date)
		waiting := runtimeTestState(r, date+"/"+event.ID)
		if waiting.Status != "RETRY_WAIT" || waiting.RungAttempts != 1 {
			t.Fatalf("first late_until result=%+v, want one scheduled primary retry", waiting)
		}
		fc.Advance(4 * time.Second)
		r.execute(x, date)
		state := runtimeTestState(r, date+"/"+event.ID)
		if state.Status != "SUCCEEDED_DEGRADED" || state.Tier != "fallback" || state.Attempts != 2 {
			t.Fatalf("late_until ladder result=%+v, want fallback success after one primary attempt", state)
		}
		b, err := os.ReadFile(calls)
		if err != nil {
			t.Fatal(err)
		}
		var tiers []string
		for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
			var request struct {
				Tier string `json:"tier"`
			}
			if err := json.Unmarshal([]byte(line), &request); err != nil {
				t.Fatal(err)
			}
			tiers = append(tiers, request.Tier)
		}
		if strings.Join(tiers, ",") != "primary,fallback" {
			t.Fatalf("late_until tier calls=%v", tiers)
		}
	})
}

func TestC559Exit40IsPermanentWhile30And75Retry(t *testing.T) {
	for _, tc := range []struct {
		name      string
		exitCode  string
		wantCalls int
		wantGaps  []time.Duration
	}{
		{name: "exit 40 is permanent", exitCode: "40", wantCalls: 1},
		{name: "exit 30 retries", exitCode: "30", wantCalls: 3, wantGaps: []time.Duration{5 * time.Minute, 10 * time.Minute}},
		{name: "exit 75 retries", exitCode: "75", wantCalls: 3, wantGaps: []time.Duration{5 * time.Minute, 10 * time.Minute}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root, date := t.TempDir(), "2026-10-20"
			loc, _ := time.LoadLocation("Asia/Shanghai")
			now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:00", loc)
			calls := filepath.Join(root, "calls.txt")
			script := writeC545Script(t, filepath.Join(root, "handler.sh"), `printf '%s\n' "$EXIT_CODE" >> "$CALL_LOG"
if [ "$EXIT_CODE" = 40 ]; then
  printf '%s\n' '{"contract":1,"status":"failed","error_code":"DETERMINISTIC_FAILURE","retryable":false}'
else
  printf '%s\n' '{"contract":1,"status":"failed","error_code":"TEMP","retryable":true}'
fi
exit "$EXIT_CODE"
`)
			t.Setenv("CALL_LOG", calls)
			t.Setenv("EXIT_CODE", tc.exitCode)
			budgetEnabled := false
			event := config.Event{ID: "exit_code", Handler: "h", Ladder: []config.LadderTier{
				{Name: "primary", Attempts: 3, Backoff: "5m*2", Timeout: "20m", BudgetEnabled: &budgetEnabled},
			}}
			fc := newFakeClock(now)
			r, closeRunner := newC545Runner(t, root, date, fc, config.Config{
				Events:   []config.Event{event},
				Handlers: map[string]config.Handler{"h": {Argv: []string{script}, EnvAllow: []string{"CALL_LOG", "EXIT_CODE"}}},
			})
			defer closeRunner()

			advanceStop := make(chan struct{})
			advancesDone := make(chan struct{})
			go func() {
				defer close(advancesDone)
				for i, d := range []time.Duration{5 * time.Minute, 10 * time.Minute} {
					for fc.waitCount() <= i {
						select {
						case <-advanceStop:
							return
						default:
						}
						runtime.Gosched()
					}
					select {
					case <-advanceStop:
						return
					default:
					}
					fc.Advance(d)
				}
			}()
			r.execute(Instance{ID: event.ID, EventID: event.ID, TriggerType: "startup"}, date)
			close(advanceStop)
			<-advancesDone

			got := runtimeTestState(r, date+"/"+event.ID)
			if got.Status != "FAILED_FINAL" || got.Attempts != tc.wantCalls || r.alertDaily != 1 {
				t.Fatalf("state=%+v alerts=%d, want FAILED_FINAL after %d attempt(s) and one alert", got, r.alertDaily, tc.wantCalls)
			}
			b, err := os.ReadFile(calls)
			if err != nil {
				t.Fatal(err)
			}
			lines := strings.Split(strings.TrimSpace(string(b)), "\n")
			if len(lines) != tc.wantCalls {
				t.Fatalf("handler calls=%d, want %d", len(lines), tc.wantCalls)
			}
			var wantAdvance time.Duration
			for _, gap := range tc.wantGaps {
				wantAdvance += gap
			}
			if got := fc.Now().Sub(now); got != wantAdvance || fc.waitCount() != len(tc.wantGaps) {
				t.Fatalf("fake clock advanced %s with %d waits, want %s with %d waits", got, fc.waitCount(), wantAdvance, len(tc.wantGaps))
			}
		})
	}
}

func TestC547StartupLadderAppliesDeadlineTimeBudget(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:00", loc)
	calls := filepath.Join(root, "calls.jsonl")
	script := writeC545Script(t, filepath.Join(root, "handler.sh"), `cat >> "$CALL_LOG"
printf '%s\n' '{"contract":1,"status":"ok"}'
`)
	t.Setenv("CALL_LOG", calls)
	event := config.Event{ID: "startup_budget", Handler: "h", Ladder: []config.LadderTier{
		{Name: "primary", Attempts: 1, Timeout: "6s"},
		{Name: "fallback", Attempts: 1, Timeout: "2s"},
	}}
	fc := newFakeClock(now)
	r, closeRunner := newC545Runner(t, root, date, fc, config.Config{Events: []config.Event{event}, Handlers: map[string]config.Handler{
		"h": {Argv: []string{script}, EnvAllow: []string{"CALL_LOG"}, SupportsDegrade: true},
	}})
	defer closeRunner()
	r.execute(Instance{ID: event.ID, EventID: event.ID, TriggerType: "startup", Deadline: "09:00:05"}, date)
	state := runtimeTestState(r, date+"/"+event.ID)
	if state.Status != "SUCCEEDED_DEGRADED" || state.Rung != 2 || len(state.SkippedTiers) != 1 || state.SkippedTiers[0] != "primary" {
		t.Fatalf("startup time-budget result=%+v, want primary skipped and fallback success", state)
	}
	b, err := os.ReadFile(calls)
	if err != nil {
		t.Fatal(err)
	}
	var request struct {
		Tier string `json:"tier"`
	}
	if err := json.Unmarshal(bytes.TrimSpace(b), &request); err != nil {
		t.Fatal(err)
	}
	if request.Tier != "fallback" {
		t.Fatalf("startup handler ran tier %q, want fallback only", request.Tier)
	}
}

func TestC547LateUntilRetryYieldsBetweenAttempts(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:59:58", loc)
	calls := filepath.Join(root, "calls.jsonl")
	script := writeC545Script(t, filepath.Join(root, "handler.sh"), `cat >> "$CALL_LOG"
printf '%s\n' '{"contract":1,"status":"failed","error_code":"TEMP","retryable":true}'
exit 75
`)
	t.Setenv("CALL_LOG", calls)
	event := config.Event{ID: "late_retry", Handler: "h", LateUntil: "10:00:00", Ladder: []config.LadderTier{{
		Name: "primary", Attempts: 3, Backoff: "1s*2", Timeout: "2s", Until: "late_until",
	}}}
	fc := newFakeClock(now)
	r, closeRunner := newC545Runner(t, root, date, fc, config.Config{Events: []config.Event{event}, Handlers: map[string]config.Handler{
		"h": {Argv: []string{script}, EnvAllow: []string{"CALL_LOG"}},
	}})
	defer closeRunner()
	x := Instance{ID: event.ID, EventID: event.ID, TriggerType: "window", Start: "09:59:00", Deadline: "10:00:30", LateUntil: event.LateUntil}
	done := make(chan struct{})
	go func() {
		for {
			select {
			case <-done:
				return
			default:
			}
			if fc.waitCount() > 0 {
				fc.Advance(10 * time.Second)
				return
			}
			runtime.Gosched()
		}
	}()
	r.execute(x, date)
	close(done)
	state := runtimeTestState(r, date+"/"+event.ID)
	if state.Status != "RETRY_WAIT" || state.Attempts != 1 || state.Rung != 1 || state.RungAttempts != 1 || state.NextAttemptAt == "" {
		t.Fatalf("first late_until attempt state=%+v, want one attempt scheduled for retry", state)
	}
	if fc.waitCount() != 0 {
		t.Fatalf("late_until retry blocked the worker on fake clock %d times", fc.waitCount())
	}
}

func TestC557DeadlineRetryUsesTierUntil(t *testing.T) {
	type sourceEvent struct {
		ID              string          `json:"event_id"`
		At              json.RawMessage `json:"at"`
		Deadline        string          `json:"deadline"`
		LateUntil       string          `json:"late_until"`
		OnDeadline      string          `json:"on_deadline"`
		Profile         string          `json:"profile"`
		Cost            string          `json:"cost"`
		RetryUntilLate  bool            `json:"retry_until_late"`
		SilentUntilLast bool            `json:"silent_until_last"`
	}
	var source struct {
		Events   []sourceEvent `json:"events"`
		Profiles map[string]struct {
			Ladder []config.LadderTier `json:"ladder"`
		} `json:"profiles"`
	}
	b, err := os.ReadFile(filepath.Join("..", "..", "src", "engine_core", "guardian", "schedule.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, &source); err != nil {
		t.Fatal(err)
	}

	scaleDuration := func(d time.Duration) time.Duration {
		seconds := d / time.Minute
		if d%time.Minute != 0 {
			seconds++
		}
		if seconds < 1 {
			seconds = 1
		}
		return seconds * time.Second
	}
	scaleTimeout := func(d time.Duration) time.Duration {
		units := d / time.Minute
		if d%time.Minute != 0 {
			units++
		}
		if units < 1 {
			units = 1
		}
		return units * 10 * time.Millisecond
	}
	scaledClock := func(from, to string) string {
		parse := func(value string) time.Time {
			parsed, err := time.Parse("15:04:05", value)
			if err != nil {
				t.Fatalf("invalid production schedule clock %q: %v", value, err)
			}
			return parsed
		}
		seconds := int(scaleDuration(parse(to).Sub(parse(from))) / time.Second)
		return time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC).Add(time.Duration(seconds) * time.Second).Format("15:04:05")
	}
	production := map[string]config.Event{}
	for _, id := range []string{"startup_0830", "baostock_probe_1730", "baostock_retry_2000"} {
		var item *sourceEvent
		for i := range source.Events {
			if source.Events[i].ID == id {
				item = &source.Events[i]
				break
			}
		}
		if item == nil {
			t.Fatalf("production schedule is missing %s", id)
		}
		var at string
		if err := json.Unmarshal(item.At, &at); err != nil {
			t.Fatalf("production schedule event %s has invalid at value %s: %v", id, item.At, err)
		}
		profile, ok := source.Profiles[item.Profile]
		if !ok || len(profile.Ladder) == 0 {
			t.Fatalf("production schedule profile %s has no ladder", item.Profile)
		}
		ladder := append([]config.LadderTier(nil), profile.Ladder...)
		for i := range ladder {
			if d, err := time.ParseDuration(ladder[i].Timeout); err == nil {
				ladder[i].Timeout = scaleTimeout(d).String()
			}
			if ladder[i].Backoff != "" {
				parts := strings.SplitN(ladder[i].Backoff, "*", 2)
				if d, err := time.ParseDuration(parts[0]); err == nil {
					parts[0] = scaleDuration(d).String()
				}
				ladder[i].Backoff = strings.Join(parts, "*")
			}
			if d, err := time.ParseDuration(ladder[i].BackoffCap); err == nil {
				ladder[i].BackoffCap = scaleDuration(d).String()
			}
		}
		production[id] = config.Event{
			ID: id, Enabled: true, Handler: "handler", Catchup: "run_in_window", Cost: item.Cost,
			Trigger:   config.Trigger{Type: "window", Start: "00:00:00", Deadline: scaledClock(at, item.Deadline)},
			LateUntil: scaledClock(at, item.LateUntil), OnDeadline: item.OnDeadline,
			Retry: config.Retry{UntilLate: item.RetryUntilLate, SilentUntilLast: item.SilentUntilLast},
			Gate:  config.Gate{Ref: "calendar_ready", Poll: "1s", Timeout: "1s"}, Ladder: ladder,
		}
	}

	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	base, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 00:00:00", loc)
	writeRunner := func(t *testing.T, event config.Event) (*liveRunner, *fakeClock, string, string, func()) {
		t.Helper()
		root := t.TempDir()
		calls := filepath.Join(root, "calls.txt")
		release := filepath.Join(root, "release")
		handler := writeC545Script(t, filepath.Join(root, "handler.sh"), `printf '%s\n' call >> "$CALL_LOG"
n=$(wc -l < "$CALL_LOG")
if [ "$n" -eq 1 ]; then
  while [ ! -e "$RELEASE" ]; do sleep 0.01; done
  sleep 5
fi
printf '%s\n' '{"contract":1,"status":"ok"}'
`)
		gate := writeC545Script(t, filepath.Join(root, "gate.sh"), `printf '%s\n' '{"contract":1,"status":"ready","reason":"calendar_ready"}'
`)
		t.Setenv("CALL_LOG", calls)
		t.Setenv("RELEASE", release)
		fc := newFakeClock(base)
		r, closeRunner := newC545Runner(t, root, date, fc, config.Config{Events: []config.Event{event}, Handlers: map[string]config.Handler{
			"handler": {Argv: []string{handler}, GateArgv: []string{gate}, EnvAllow: []string{"CALL_LOG", "RELEASE"}},
		}})
		return r, fc, calls, release, closeRunner
	}
	runPastDeadlineFailure := func(t *testing.T, event config.Event) (*liveRunner, *fakeClock, string, func()) {
		t.Helper()
		r, fc, calls, release, closeRunner := writeRunner(t, event)
		advanced := make(chan bool, 1)
		go func() {
			end := time.Now().Add(5 * time.Second)
			for time.Now().Before(end) {
				if _, err := os.Stat(calls); err == nil {
					deadline, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" "+event.Trigger.Deadline, loc)
					if !fc.Now().Before(deadline) {
						advanced <- false
						return
					}
					fc.Advance(deadline.Sub(base) + time.Second)
					_ = os.WriteFile(release, []byte("go"), 0600)
					advanced <- true
					return
				}
				runtime.Gosched()
			}
			advanced <- false
		}()
		r.schedule(base)
		if !<-advanced {
			closeRunner()
			t.Fatal("first handler attempt did not start")
		}
		waitFor(t, func() bool {
			st := runtimeTestState(r, date+"/"+event.ID)
			r.mu.Lock()
			running := r.running[event.ID]
			r.mu.Unlock()
			return st.Status == "RETRY_WAIT" && !running
		})
		return r, fc, calls, closeRunner
	}

	for _, id := range []string{"startup_0830", "baostock_probe_1730", "baostock_retry_2000"} {
		t.Run("retry succeeds after deadline/"+id, func(t *testing.T) {
			event := production[id]
			r, fc, _, closeRunner := runPastDeadlineFailure(t, event)
			defer closeRunner()
			first := runtimeTestState(r, date+"/"+id)
			if first.Attempts != 1 || first.Status != "RETRY_WAIT" || first.LastError != "HANDLER_TIMEOUT" {
				t.Fatalf("first attempt result=%+v, want HANDLER_TIMEOUT and retry wait after one pre-deadline attempt", first)
			}
			next, err := time.Parse(time.RFC3339, first.NextAttemptAt)
			until, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" "+event.LateUntil, loc)
			if err != nil || !next.Before(until) {
				t.Fatalf("next attempt %q must be due before tier until %s", first.NextAttemptAt, until.Format(time.RFC3339))
			}
			fc.Advance(next.Sub(fc.Now()))
			r.schedule(fc.Now())
			waitFor(t, func() bool { return isTerminal(runtimeTestState(r, date+"/"+id).Status) })
			state := runtimeTestState(r, date+"/"+id)
			if state.Status != "SUCCEEDED" || state.Attempts != 2 || r.alertDaily != 0 {
				t.Fatalf("post-deadline retry result=%+v alerts=%d, want SUCCEEDED after two attempts and no MISSED alert", state, r.alertDaily)
			}
		})
	}

	t.Run("until expiry is terminal and alerts once", func(t *testing.T) {
		event := production["baostock_probe_1730"]
		r, fc, _, closeRunner := runPastDeadlineFailure(t, event)
		defer closeRunner()
		until, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" "+event.LateUntil, loc)
		fc.Advance(until.Sub(fc.Now()) + time.Second)
		r.schedule(fc.Now())
		state := runtimeTestState(r, date+"/"+event.ID)
		if state.Status != "FAILED_FINAL" || state.LastError != "TIER_UNTIL_EXPIRED" || r.alertDaily != 1 {
			t.Fatalf("expired retry result=%+v alerts=%d, want FAILED_FINAL/TIER_UNTIL_EXPIRED and one alert", state, r.alertDaily)
		}
		r.schedule(fc.Now())
		if got := runtimeTestState(r, date+"/"+event.ID); got.Status != "FAILED_FINAL" || r.alertDaily != 1 {
			t.Fatalf("repeated scheduler pass result=%+v alerts=%d, want one alert total", got, r.alertDaily)
		}
	})

	t.Run("never started remains missed", func(t *testing.T) {
		event := config.Event{ID: "never_started", Enabled: true, Handler: "handler", Catchup: "run_in_window", OnDeadline: "missed",
			Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:00:05"}}
		late, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:06", loc)
		fc := newFakeClock(late)
		r, closeRunner := newC545Runner(t, t.TempDir(), date, fc, config.Config{Events: []config.Event{event}})
		defer closeRunner()
		r.schedule(late)
		state := runtimeTestState(r, date+"/"+event.ID)
		if state.Status != "MISSED" || state.Attempts != 0 || r.alertDaily != 1 {
			t.Fatalf("never-started result=%+v alerts=%d, want MISSED with one alert", state, r.alertDaily)
		}
	})

	t.Run("ordinary retry remains bounded by event deadline", func(t *testing.T) {
		event := config.Event{ID: "ordinary_retry", Enabled: true, Handler: "handler", Catchup: "run_in_window", OnDeadline: "missed",
			Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:00:05"}}
		late, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:06", loc)
		fc := newFakeClock(late)
		r, closeRunner := newC545Runner(t, t.TempDir(), date, fc, config.Config{Events: []config.Event{event}})
		defer closeRunner()
		x := Instance{ID: event.ID, EventID: event.ID, TriggerType: "window", Start: event.Trigger.Start, Deadline: event.Trigger.Deadline}
		r.setState(x, date, instanceState{Status: "RETRY_WAIT", Attempts: 1, StartedAt: "2026-10-20T09:00:04+08:00", NextAttemptAt: late.Format(time.RFC3339), LastError: "HANDLER_TIMEOUT"})
		r.schedule(late)
		state := runtimeTestState(r, date+"/"+event.ID)
		if state.Status != "MISSED" || state.Attempts != 1 || r.alertDaily != 1 {
			t.Fatalf("ordinary retry result=%+v alerts=%d, want event-deadline MISSED with one alert", state, r.alertDaily)
		}
	})
}

func TestC548ScenarioMatrix(t *testing.T) {
	date := "2026-10-20"
	loc, err := time.LoadLocation("Asia/Shanghai")
	if err != nil {
		t.Fatal(err)
	}
	start, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:00:00", loc)
	triggers := []string{"punctual", "startup_late", "window_expired"}
	outcomes := []string{"success", "retry30_then_success", "retry30_exhausted", "tier_no_data40", "timeout_killed", "degraded_success10", "already_satisfied20"}
	margins := []string{"enough", "only_final_tier", "insufficient_for_first"}
	restarts := []string{"none", "within_tier", "between_tiers"}
	t.Log("C548_MATRIX|trigger|outcome|margin|restart|expected_state|expected_calls|expected_ledger")
	rowCount := 0
	for _, trigger := range triggers {
		for _, outcome := range outcomes {
			for _, margin := range margins {
				for _, restart := range restarts {
					cell := fmt.Sprintf("%s/%s/%s/%s", trigger, outcome, margin, restart)
					rowCount++
					t.Run(cell, func(t *testing.T) {
						root := t.TempDir()
						callsPath := filepath.Join(root, "calls.jsonl")
						datePath := filepath.Join(root, "trade-dates.json")
						if err := os.WriteFile(datePath, []byte("[\""+date+"\"]\n"), 0600); err != nil {
							t.Fatal(err)
						}
						handler := writeC545Script(t, filepath.Join(root, "handler.sh"), `request=$(cat)
printf '%s\n' "$request" >> "$CALL_LOG"
tier=$(printf '%s\n' "$request" | sed -n 's/.*"tier":"\([^"]*\)".*/\1/p')
attempt=$(printf '%s\n' "$request" | sed -n 's/.*"attempt":\([0-9][0-9]*\).*/\1/p')
case "$MATRIX_OUTCOME" in
  success) printf '%s\n' '{"contract":1,"status":"ok"}' ;;
  retry30_then_success)
    if [ "$attempt" -eq "$MATRIX_FAIL_AT" ]; then printf '%s\n' '{"contract":1,"status":"failed","error_code":"TEMP","retryable":true}'; exit 75; fi
    printf '%s\n' '{"contract":1,"status":"ok"}' ;;
  retry30_exhausted) printf '%s\n' '{"contract":1,"status":"failed","error_code":"TEMP","retryable":true}'; exit 75 ;;
  tier_no_data40) printf '%s\n' '{"contract":1,"status":"failed","error_code":"TIER_NO_DATA","retryable":false}'; exit 40 ;;
  timeout_killed) sleep 5 ;;
  degraded_success10)
    if [ "$tier" = "redis" ]; then printf '%s\n' '{"contract":1,"status":"failed","error_code":"TIER_NO_DATA","retryable":false}'; exit 40; fi
    printf '%s\n' '{"contract":1,"status":"degraded","error_code":"","retryable":false}'; exit 10 ;;
  already_satisfied20) printf '%s\n' '{"contract":1,"status":"already_satisfied","error_code":"ALREADY_SATISFIED"}' ;;
esac
`)
						alertHandler := writeC545Script(t, filepath.Join(root, "alert.sh"), `cat >/dev/null
printf '%s\n' '{"contract":1,"status":"ok"}'
`)
						t.Setenv("CALL_LOG", callsPath)
						t.Setenv("MATRIX_OUTCOME", outcome)
						priorAttempts := 0
						if restart != "none" {
							priorAttempts = 1
						}
						failAt := priorAttempts + 1
						t.Setenv("MATRIX_FAIL_AT", strconv.Itoa(failAt))

						triggerAt := start
						deadline := start.Add(time.Second)
						now := start
						switch trigger {
						case "punctual":
							switch margin {
							case "enough":
								deadline = start.Add(30 * time.Second)
							case "only_final_tier":
								deadline = start.Add(4 * time.Second)
							case "insufficient_for_first":
								deadline = start.Add(time.Second)
							}
						case "startup_late":
							now = start.Add(time.Second)
							switch margin {
							case "enough":
								deadline = start.Add(31 * time.Second)
							case "only_final_tier":
								deadline = start.Add(4 * time.Second)
							case "insufficient_for_first":
								deadline = start.Add(2 * time.Second)
							}
						case "window_expired":
							now = start.Add(6 * time.Second)
							deadline = start.Add(5 * time.Second)
						}
						timeouts := []string{"5s", "4s", "2s"}
						if outcome == "timeout_killed" {
							if margin == "enough" {
								timeouts = []string{"250ms", "250ms", "250ms"}
							} else if margin == "only_final_tier" {
								timeouts = []string{"5s", "4s", "250ms"}
							} else {
								timeouts = []string{"2s", "2s", "2s"}
							}
						}
						attempts := 2
						if outcome == "retry30_then_success" {
							attempts = 3
						}
						if outcome == "retry30_exhausted" || outcome == "timeout_killed" {
							attempts = 2
						}
						ladder := []config.LadderTier{
							{Name: "redis", Attempts: attempts, Timeout: timeouts[0]},
							{Name: "td", Attempts: attempts, Timeout: timeouts[1]},
							{Name: "wencai", Attempts: attempts, Timeout: timeouts[2]},
						}
						if restart == "between_tiers" {
							ladder[0].Attempts = 1
						}
						event := config.Event{ID: "matrix", Enabled: true, Handler: "h", Catchup: "run_in_window",
							Calendar: json.RawMessage(`"every_day"`), Phases: []string{"any"},
							Trigger:    config.Trigger{Type: "window", Start: triggerAt.Format("15:04:05"), Deadline: deadline.Format("15:04:05")},
							OnDeadline: "missed", Ladder: ladder}
						c := config.Config{Events: []config.Event{event}, Calendars: map[string]struct {
							File string `json:"file"`
						}{"trading": {File: datePath}}, Handlers: map[string]config.Handler{
							"h":     {Argv: []string{handler}, EnvAllow: []string{"CALL_LOG", "MATRIX_OUTCOME", "MATRIX_FAIL_AT"}},
							"alert": {Argv: []string{alertHandler}},
						}}
						fc := newFakeClock(now)
						r, closeRunner := newC545Runner(t, root, date, fc, c)
						if restart != "none" {
							priorError := "TEMP"
							if restart == "between_tiers" {
								priorError = "TIER_NO_DATA"
							}
							prior := instanceState{Status: "RUNNING", Attempts: 1, Rung: 1, Tier: "redis", RungAttempts: 1, StartedAt: now.Add(-time.Second).Format(time.RFC3339)}
							x := Instance{ID: "matrix", EventID: "matrix", TriggerType: "window", Start: event.Trigger.Start, Deadline: event.Trigger.Deadline}
							r.setState(x, date, prior)
							_, _ = r.appendLedger(date, "TIER_ATTEMPT", map[string]any{"instance": "matrix", "rung": 1, "tier": "redis", "rung_attempt": 1, "attempt": 1})
							_, _ = r.appendLedger(date, "TIER_FAILED", map[string]any{"instance": "matrix", "rung": 1, "tier": "redis", "rung_attempt": 1, "attempt": 1, "error_code": priorError})
							if err := os.WriteFile(callsPath, []byte(`{"tier":"redis","prior":"restart"}`+"\n"), 0600); err != nil {
								t.Fatal(err)
							}
							closeRunner()
							r, closeRunner = newC545Runner(t, root, date, fc, c)
							r.start = now
							if err := r.recover(); err != nil {
								t.Fatal(err)
							}
						}
						defer closeRunner()
						r.schedule(now)
						if err := waitC548MatrixDone(r, date, "matrix", 4*time.Second); err != nil {
							t.Fatal(err)
						}
						got := runtimeTestState(r, date+"/matrix")
						gotCalls := readC548MatrixCalls(t, callsPath)
						expectedCalls, expectedStatus := c548ExpectedMatrix(trigger, outcome, margin, restart)
						if got.Status != expectedStatus {
							t.Fatalf("state=%+v calls=%v expected_status=%s", got, gotCalls, expectedStatus)
						}
						if strings.Join(gotCalls, ",") != strings.Join(expectedCalls, ",") {
							t.Fatalf("calls=%v expected=%v", gotCalls, expectedCalls)
						}
						gotLedger := ledgerTypeCounts(t, root, date)
						expectedLedger := c548ExpectedMatrixLedger(trigger, outcome, margin, restart, expectedCalls)
						for typ, want := range expectedLedger {
							if gotLedger[typ] != want {
								t.Fatalf("ledger %s=%d expected=%d; all=%v", typ, gotLedger[typ], want, gotLedger)
							}
						}
						t.Logf("MATRIX|trigger=%s|outcome=%s|margin=%s|restart=%s|status=%s|calls=%s|ledger=%s", trigger, outcome, margin, restart, expectedStatus, strings.Join(expectedCalls, ">"), c548FormatMatrixLedger(expectedLedger))
					})
				}
			}
		}
	}
	if rowCount != 189 {
		t.Fatalf("matrix rows=%d, want 189", rowCount)
	}
}

func waitC548MatrixDone(r *liveRunner, date, id string, timeout time.Duration) error {
	until := time.Now().Add(timeout)
	for time.Now().Before(until) {
		r.mu.Lock()
		state, ok := r.states[date+"/"+id]
		running := r.running[id]
		r.mu.Unlock()
		if ok && isTerminal(state.Status) && !running {
			return nil
		}
		time.Sleep(time.Millisecond)
	}
	return fmt.Errorf("matrix instance %s did not finish", id)
}

func readC548MatrixCalls(t *testing.T, path string) []string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		t.Fatal(err)
	}
	var calls []string
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if line == "" {
			continue
		}
		var request struct {
			Tier string `json:"tier"`
		}
		if err := json.Unmarshal([]byte(line), &request); err != nil {
			t.Fatal(err)
		}
		calls = append(calls, request.Tier)
	}
	return calls
}

func c548ExpectedMatrix(trigger, outcome, margin, restart string) ([]string, string) {
	calls := []string{}
	if restart != "none" {
		calls = append(calls, "redis")
	}
	if trigger == "window_expired" {
		return calls, "MISSED"
	}
	if margin == "insufficient_for_first" {
		return calls, "FAILED_FINAL"
	}
	tiers := []string{"redis", "td", "wencai"}
	start := 0
	if restart == "between_tiers" {
		start = 1
	}
	if margin == "only_final_tier" {
		start = 2
	}
	if start > 2 {
		return calls, "FAILED_FINAL"
	}
	appendAttempts := func(index, count int) {
		remaining := count
		if restart == "within_tier" && index == 0 {
			remaining--
		}
		for i := 0; i < remaining; i++ {
			calls = append(calls, tiers[index])
		}
	}
	switch outcome {
	case "success":
		calls = append(calls, tiers[start])
		if start > 0 {
			return calls, "SUCCEEDED_DEGRADED"
		}
		return calls, "SUCCEEDED"
	case "retry30_then_success":
		calls = append(calls, tiers[start], tiers[start])
		if start > 0 {
			return calls, "SUCCEEDED_DEGRADED"
		}
		return calls, "SUCCEEDED"
	case "retry30_exhausted", "timeout_killed":
		attempts := 2
		if outcome == "retry30_then_success" {
			attempts = 3
		}
		for i := start; i < len(tiers); i++ {
			appendAttempts(i, attempts)
		}
		return calls, "FAILED_FINAL"
	case "tier_no_data40":
		for i := start; i < len(tiers); i++ {
			calls = append(calls, tiers[i])
		}
		return calls, "FAILED_FINAL"
	case "degraded_success10":
		if restart != "between_tiers" && start == 0 {
			calls = append(calls, "redis", "td")
		} else {
			calls = append(calls, tiers[start])
		}
		return calls, "SUCCEEDED_DEGRADED"
	case "already_satisfied20":
		calls = append(calls, tiers[start])
		return calls, "SKIPPED_SATISFIED"
	}
	return calls, "FAILED_FINAL"
}

func c548ExpectedMatrixLedger(trigger, outcome, margin, restart string, expectedCalls []string) map[string]int {
	seeded := 0
	if restart != "none" {
		seeded = 1
	}
	actual := len(expectedCalls) - seeded
	if trigger == "window_expired" {
		actual = 0
	}
	if margin == "insufficient_for_first" && trigger != "window_expired" {
		actual = 0
	}
	failed := seeded
	switch outcome {
	case "retry30_then_success":
		if actual > seeded {
			failed++
		}
	case "retry30_exhausted", "tier_no_data40", "timeout_killed":
		failed += actual
	case "degraded_success10":
		if restart != "between_tiers" && margin != "insufficient_for_first" && trigger != "window_expired" && margin != "only_final_tier" {
			failed++
		}
	}
	skipped := 0
	if trigger != "window_expired" {
		if margin == "only_final_tier" {
			skipped = 2
		} else if margin == "insufficient_for_first" {
			skipped = 3
		}
	}
	exhausted := 0
	eligibleTiers := 3
	if margin == "only_final_tier" {
		eligibleTiers = 1
	} else if margin == "insufficient_for_first" || trigger == "window_expired" {
		eligibleTiers = 0
	}
	if restart == "between_tiers" && eligibleTiers > 0 && margin != "only_final_tier" && trigger != "window_expired" {
		exhausted++
	}
	if (outcome == "retry30_exhausted" || outcome == "timeout_killed") && actual > 0 {
		currentTiers := eligibleTiers
		if restart == "between_tiers" && margin != "only_final_tier" && currentTiers > 0 {
			currentTiers--
		}
		exhausted += currentTiers
	}
	alerts := 0
	if trigger == "window_expired" || margin == "insufficient_for_first" || outcome == "retry30_exhausted" || outcome == "tier_no_data40" || outcome == "timeout_killed" {
		alerts = 1
	}
	satisfied := 0
	if outcome == "already_satisfied20" && actual > 0 {
		satisfied = 1
	}
	return map[string]int{
		"TIER_ATTEMPT":      len(expectedCalls),
		"TIER_FAILED":       failed,
		"TIER_EXHAUSTED":    exhausted,
		"TIER_SKIPPED":      skipped,
		"ALREADY_SATISFIED": satisfied,
		"ATTEMPT_START":     actual,
		"ATTEMPT_END":       actual,
		"ALERT_OUT":         alerts,
	}
}

func c548FormatMatrixLedger(counts map[string]int) string {
	order := []string{"TIER_ATTEMPT", "TIER_FAILED", "TIER_EXHAUSTED", "TIER_SKIPPED", "ALREADY_SATISFIED", "ATTEMPT_START", "ATTEMPT_END", "ALERT_OUT"}
	parts := make([]string, 0, len(order))
	for _, key := range order {
		parts = append(parts, fmt.Sprintf("%s:%d", key, counts[key]))
	}
	return strings.Join(parts, ",")
}
