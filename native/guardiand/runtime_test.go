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

func TestLedgerTruncatesCorruptTailAndResumesSequence(t *testing.T) {
	root := t.TempDir()
	l, err := openLedger(root, "2026-10-20")
	if err != nil {
		t.Fatal(err)
	}
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

func TestGateDeadlineAndDailySummary(t *testing.T) {
	root := t.TempDir()
	date := "2026-10-20"
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, _ := time.ParseInLocation(time.RFC3339, date+"T10:34:00+08:00", loc)
	gateScript := filepath.Join(root, "gate.sh")
	calls := filepath.Join(root, "gate-calls")
	if err := os.WriteFile(gateScript, []byte("#!/bin/sh\necho x >> \"$GATE_CALLS\"\nprintf '{\"status\":\"not_ready\"}\\n'\n"), 0700); err != nil {
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
	if counts["ALERT_OUT"] != 1 {
		t.Fatalf("gate deadline summary alerts=%d, want one", counts["ALERT_OUT"])
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
	if got := ledgerTypeCounts(t, root, date)["ALERT_OUT"]; got != 1 {
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
