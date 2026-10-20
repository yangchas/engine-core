package main

import (
	"bufio"
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
	decoded, issues := config.Decode([]byte(`{"schema_version":1,"config_version":1,"name":"test","timezone":"Asia/Shanghai","events":[{"id":"heavy","trigger":{"type":"window","start":"09:00:00","deadline":"09:10:00"},"late_until":"13:00:00","phases":["MIDDAY_BREAK"],"on_phase_mismatch":"defer","on_start":"reconcile","handler":"h"}],"handlers":{"h":{"argv":["` + script + `"]}}}`))
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

func TestHeavyInstancesQueueOutsideClassSlots(t *testing.T) {
	root, date := t.TempDir(), "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 10:00:00", loc)
	fc := newFakeClock(now)
	script := filepath.Join(root, "slow.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nsleep 0.3\nprintf '%s\\n' '{\"contract\":1,\"status\":\"ok\"}'\n"), 0700); err != nil {
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
	l.now = fc.Now
	r := &liveRunner{c: c, root: root, loc: loc, clock: fc, fake: fc, ledger: l, states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}}
	getState := func(id string) instanceState { r.mu.Lock(); defer r.mu.Unlock(); return r.states[date+"/"+id] }
	x1 := Instance{ID: "heavy_a", EventID: "heavy_a", Start: "09:00:00", Deadline: "11:00:00"}
	x2 := Instance{ID: "heavy_b", EventID: "heavy_b", Start: "09:00:00", Deadline: "11:00:00"}
	go r.execute(x1, date)
	waitFor(t, func() bool { return getState("heavy_a").Status == "RUNNING" })
	r.execute(x2, date)
	if got := getState("heavy_b").Status; got != "QUEUED_HEAVY" {
		t.Fatalf("second heavy status=%s", got)
	}
	waitFor(t, func() bool { return isTerminal(getState("heavy_a").Status) })
	waitFor(t, func() bool { r.mu.Lock(); defer r.mu.Unlock(); return !r.heavyRunning })
	r.execute(x2, date)
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
	}{"trading": {File: calendar}}, Handlers: map[string]config.Handler{"ha": {Argv: []string{a}}, "hb": {Argv: []string{b}}}, Events: []config.Event{{ID: "boot_a", Trigger: config.Trigger{Type: "startup", Order: 20}, Handler: "ha"}, {ID: "boot_b", Trigger: config.Trigger{Type: "startup", Order: 10, After: []string{"boot_a"}}, Handler: "hb"}}}
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
	e := config.Event{ID: "late", Handler: "h", Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:01:00"}, LateUntil: "09:03:00"}
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
	if got := r.states[date+"/na"].Status; got != "SKIPPED_NA" {
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
	c := config.Config{Timezone: "Asia/Shanghai"}
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
		if r.states["2026-10-20/"+x.ID].Status != "MISSED" {
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
	if st := restarted.states[date+"/already_done"]; st.Status != "SUCCEEDED" || st.Attempts != 1 {
		t.Fatalf("recovered state=%+v", st)
	}
	if err = restarted.applyStartup(now, nil, []Instance{x}); err != nil {
		t.Fatal(err)
	}
	if restarted.running[x.ID] {
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
		if len(r.states) != 1 {
			t.Fatalf("day %d retained %d in-memory dates", day, len(r.states))
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
	c := config.Config{Events: []config.Event{{ID: "a", Trigger: config.Trigger{Type: "window", Start: "10:30:00", Deadline: "10:40:00"}}}}
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
	if got := r.states[date+"/"+x.ID]; got.Status != "RUNNING" || got.Attempts != 1 {
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
	r := &liveRunner{c: config.Config{}, root: root, loc: loc, clock: newFakeClock(now), ledger: l, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
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
	if got := r.states[date+"/fast"].Status; got != "SUCCEEDED" {
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
	if got := r.states[date+"/retry"]; got.Status != "SUCCEEDED" || got.Attempts != 3 {
		t.Fatalf("retry result=%+v, want success on 3 attempts", got)
	}
	r.execute(Instance{ID: "timeout", EventID: "timeout"}, date)
	if got := r.states[date+"/timeout"]; got.Attempts != 1 || got.LastError != "HANDLER_TIMEOUT" {
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
	waiting := r.states[date+"/"+e.ID]
	if waiting.Status != "RETRY_WAIT" || waiting.Attempts != 1 || waiting.NextAttemptAt != now.Add(time.Minute).Format(time.RFC3339) {
		t.Fatalf("first retry state=%+v", waiting)
	}
	// Rebuild state from the ledger to prove the cumulative attempt count survives restart.
	r.states = map[string]instanceState{}
	if err := r.recover(); err != nil {
		t.Fatal(err)
	}
	if recovered := r.states[date+"/"+e.ID]; recovered.Attempts != 1 || recovered.NextAttemptAt != waiting.NextAttemptAt {
		t.Fatalf("recovered retry state=%+v", recovered)
	}
	fc.Advance(time.Minute)
	r.execute(x, date)
	second := r.states[date+"/"+e.ID]
	if second.Status != "RETRY_WAIT" || second.Attempts != 2 || second.NextAttemptAt != now.Add(3*time.Minute).Format(time.RFC3339) {
		t.Fatalf("second retry state=%+v, want due at +3m", second)
	}
	fc.Advance(2 * time.Minute)
	r.execute(x, date)
	final := r.states[date+"/"+e.ID]
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
		if got := r.states[date+"/"+e.ID]; got.Status != "SUCCEEDED" || got.Attempts != 2 {
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
			if i < 2 && r.states[date+"/"+e.ID].Status != "RETRY_WAIT" {
				t.Fatalf("checkpoint %d state=%+v", i+1, r.states[date+"/"+e.ID])
			}
		}
		if got := r.states[date+"/"+e.ID]; got.Status != "FAILED_FINAL" || got.Attempts != 3 || got.LastError != "RETRIES_EXHAUSTED" || r.alertDaily != 0 {
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
	if got := r.states[date+"/"+e.ID]; got.Status != "FAILED_FINAL" || got.Attempts != 3 {
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
	waiting := r.states[date+"/"+e.ID]
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
	if recovered := r.states[date+"/"+e.ID]; recovered.Status != "RETRY_WAIT" || recovered.NextAttemptAt != waiting.NextAttemptAt {
		t.Fatalf("recovered retry state=%+v, want persisted wait at %s", recovered, waiting.NextAttemptAt)
	}
	fc.Advance(time.Second)
	r.execute(x, date)
	if got := r.states[date+"/"+e.ID]; got.Status != "RETRY_WAIT" || got.Attempts != 2 {
		t.Fatalf("second attempt state=%+v, want another retry wait", got)
	}
	if got := r.states[date+"/"+e.ID].NextAttemptAt; got != now.Add(3*time.Second).Format(time.RFC3339) {
		t.Fatalf("second retry due=%s, want exponential due=%s", got, now.Add(3*time.Second).Format(time.RFC3339))
	}
	fc.Advance(2 * time.Second)
	r.execute(x, date)
	if got := r.states[date+"/"+e.ID]; got.Status != "SUCCEEDED" || got.Attempts != 3 {
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
	if got := r.states[date+"/expiry"].Status; got != "RETRY_WAIT" {
		t.Fatalf("before late_until status=%s, want RETRY_WAIT", got)
	}
	fc.Advance(time.Second)
	r.schedule(fc.Now())
	got := r.states[date+"/expiry"]
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
	got := r.states[date+"/"+e.ID]
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
	if err := os.WriteFile(gateScript, []byte("#!/bin/sh\necho x >> \"$GATE_CALLS\"\nprintf '{\"status\":\"not_ready\",\"reason\":\"source_stale\"}\\n'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GATE_CALLS", calls)
	l, err := openLedger(root, date)
	if err != nil {
		t.Fatal(err)
	}
	defer l.close()
	e := config.Event{ID: "gated", Handler: "h", Gate: config.Gate{Ref: "ready", Poll: "10s"}}
	r := &liveRunner{c: config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {GateArgv: []string{gateScript}, EnvAllow: []string{"GATE_CALLS"}}}}, root: root, loc: loc, start: now, clock: newFakeClock(now), fake: newFakeClock(now), ledger: l, boot: "test", states: map[string]instanceState{}, running: map[string]bool{}, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}}
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
	if got := r.states[date+"/gated"].Status; got != "GATE_BLOCKED" {
		t.Fatalf("gate status=%s, want GATE_BLOCKED", got)
	}
	_ = r.alert("GATE_BLOCKED", map[string]any{"events": []string{"gated"}, "summary": true})
	_ = r.alert("GATE_BLOCKED", map[string]any{"events": []string{"gated"}, "summary": true})
	counts := ledgerTypeCounts(t, root, date)
	if counts["ALERT_OUT"] != 2 {
		t.Fatalf("gate first and daily summary alerts=%d, want two", counts["ALERT_OUT"])
	}
	ledgerBytes := mustRead(t, filepath.Join(root, "ledger", date+".jsonl"))
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
	if errBody(resp)["code"] != "CONFIG_INVALID" || r.c.ConfigVersion != 1 || !r.running["evt"] {
		t.Fatalf("invalid reload changed live state: response=%v version=%d running=%v", resp, r.c.ConfigVersion, r.running)
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
	c := config.Config{SchemaVersion: 1, ConfigVersion: 1, Name: "kill9", Timezone: "Asia/Shanghai", Handlers: map[string]config.Handler{"h": {Argv: []string{handler}, EnvAllow: []string{"COUNT_FILE", "PID_FILE", "MARKER", "RELEASE_FILE", "REQUESTS"}}}, Events: []config.Event{
		{ID: "missed", Enabled: true, Trigger: config.Trigger{Type: "window", Start: "09:00:00", Deadline: "09:01:00"}},
		{ID: "active", Enabled: true, Handler: "h", Timeout: "5m", Trigger: config.Trigger{Type: "window", Start: "10:30:00", Deadline: "10:40:00"}, Retry: config.Retry{MaxAttempts: 3}},
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
