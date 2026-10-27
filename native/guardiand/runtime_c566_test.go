package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"guardiand/config"
)

// C566: the 15:05 daily-kline close event comes from the checked-in schedule; every scenario below runs
// that compiled event (rung order, attempts, backoff and on_timeout/alert_on_fallback all from schedule.json).

const c566Date = "2026-10-27"

func c566Event(t *testing.T) (config.Config, config.Event) {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", "schedule.json"))
	if err != nil {
		t.Fatal(err)
	}
	c, err := compileScheduleV3(b)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range c.Events {
		if e.ID == "daily_kline_close_1505" {
			return c, e
		}
	}
	t.Fatal("schedule is missing daily_kline_close_1505")
	return c, config.Event{}
}

type c566Run struct {
	r       *liveRunner
	root    string
	calls   string
	alerts  string
	fc      *fakeClock
	closeFn func()
	event   config.Event
}

func c566Setup(t *testing.T, mutate func(*config.Event), behaviors map[string]string, startClock string) *c566Run {
	t.Helper()
	_, event := c566Event(t)
	for i := range event.Ladder { // real timeouts are minutes; the shell handler sleeps 5s to model a hang
		event.Ladder[i].Timeout = "300ms"
		event.Ladder[i].Backoff = "10s*1" // fake-clock backoff
	}
	event.Timeout = "300ms"
	if mutate != nil {
		mutate(&event)
	}
	root := t.TempDir()
	calls := filepath.Join(root, "calls.jsonl")
	alerts := filepath.Join(root, "alerts.jsonl")
	datePath := filepath.Join(root, "trade-dates.json")
	if err := os.WriteFile(datePath, []byte("[\""+c566Date+"\"]\n"), 0600); err != nil {
		t.Fatal(err)
	}
	handler := writeC545Script(t, filepath.Join(root, "handler.sh"), `request=$(cat)
printf '%s\n' "$request" >> "$CALL_LOG"
tier=$(printf '%s\n' "$request" | sed -n 's/.*"tier":"\([^"]*\)".*/\1/p')
case "$tier" in
  wencai) b="$B_WENCAI" ;;
  q2_final) b="$B_Q2" ;;
  *) b=none ;;
esac
case "$b" in
  ok10) printf '%s\n' '{"contract":1,"status":"degraded","error_code":"","retryable":false}'; exit 10 ;;
  nodata40) printf '%s\n' '{"contract":1,"status":"failed","error_code":"TIER_NO_DATA","retryable":false}'; exit 40 ;;
  mismatch40) printf '%s\n' '{"contract":1,"status":"failed","error_code":"TIER_DATE_MISMATCH","retryable":false}'; exit 40 ;;
  det40) printf '%s\n' '{"contract":1,"status":"failed","error_code":"DETERMINISTIC_FAILURE","retryable":false}'; exit 40 ;;
  retry30) printf '%s\n' '{"contract":1,"status":"failed","error_code":"TIER_PARTIAL","retryable":true}'; exit 30 ;;
  satisfied20) printf '%s\n' '{"contract":1,"status":"not_applicable","error_code":"ALREADY_SATISFIED","retryable":false}'; exit 20 ;;
  hang) sleep 5 ;;
esac
`)
	alertHandler := writeC545Script(t, filepath.Join(root, "alert.sh"), `cat >> "$ALERT_LOG"
printf '%s\n' '{"contract":1,"status":"ok"}'
`)
	gate := writeC545Script(t, filepath.Join(root, "gate.sh"), `cat >/dev/null
printf '%s\n' '{"status":"ready","reason":"calendar_ready"}'
`)
	t.Setenv("CALL_LOG", calls)
	t.Setenv("ALERT_LOG", alerts)
	t.Setenv("B_WENCAI", behaviors["wencai"])
	t.Setenv("B_Q2", behaviors["q2_final"])
	c := config.Config{Events: []config.Event{event}, Calendars: map[string]struct {
		File string `json:"file"`
	}{"trading": {File: datePath}}, Handlers: map[string]config.Handler{
		event.Handler: {Argv: []string{handler}, EnvAllow: []string{"CALL_LOG", "B_WENCAI", "B_Q2"}, SupportsDegrade: true, GateArgv: []string{gate}},
		"alert":       {Argv: []string{alertHandler}, EnvAllow: []string{"ALERT_LOG"}},
	}}
	loc, _ := time.LoadLocation("Asia/Shanghai")
	now, err := time.ParseInLocation("2006-01-02 15:04:05", c566Date+" "+startClock, loc)
	if err != nil {
		t.Fatal(err)
	}
	fc := newFakeClock(now)
	r, closeFn := newC545Runner(t, root, c566Date, fc, c)
	return &c566Run{r: r, root: root, calls: calls, alerts: alerts, fc: fc, closeFn: closeFn, event: event}
}

func (s *c566Run) run(t *testing.T) instanceState {
	t.Helper()
	s.r.schedule(s.fc.Now())
	// fake-clock backoff waits need the clock pushed forward while the ladder sleeps
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		s.r.mu.Lock()
		state, ok := s.r.states[c566Date+"/"+s.event.ID]
		running := s.r.running[s.event.ID]
		s.r.mu.Unlock()
		if ok && isTerminal(state.Status) && !running {
			return state
		}
		s.fc.Advance(5 * time.Second)
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("event did not finish: %+v", runtimeTestState(s.r, c566Date+"/"+s.event.ID))
	return instanceState{}
}

func (s *c566Run) tiers(t *testing.T) []string {
	t.Helper()
	return readC548MatrixCalls(t, s.calls)
}

func (s *c566Run) alertReasons(t *testing.T) []string {
	t.Helper()
	b, err := os.ReadFile(s.alerts)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		t.Fatal(err)
	}
	var reasons []string
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if line == "" {
			continue
		}
		var v struct {
			Reason  string         `json:"reason"`
			Request map[string]any `json:"request"`
		}
		if err := json.Unmarshal([]byte(line), &v); err != nil {
			t.Fatal(err)
		}
		text := v.Reason
		if v.Reason == "TIER_FALLBACK" {
			raw, _ := json.Marshal(v.Request)
			text += "|" + string(raw)
		}
		reasons = append(reasons, text)
	}
	return reasons
}

func TestC566ProductionEventShape(t *testing.T) {
	c, event := c566Event(t)
	if event.Trigger.Start != "15:05:00" || event.Cost != "heavy" || event.Catchup != "run_in_window" || event.OnDeadline != "missed" {
		t.Fatalf("event shape %+v", event)
	}
	var baostock config.Event
	for _, e := range c.Events {
		if e.ID == "baostock_probe_1730" {
			baostock = e
		}
	}
	if baostock.Cost != "heavy" || baostock.AvailableAfter == nil {
		t.Fatalf("baostock probe must be heavy with available_after, got %+v", baostock)
	}
	late, err1 := time.Parse("15:04:05", event.LateUntil)
	avail, err2 := time.Parse("15:04:05", *baostock.AvailableAfter)
	if err1 != nil || err2 != nil || !late.Before(avail) {
		t.Fatalf("late_until %q must precede baostock available_after %v", event.LateUntil, baostock.AvailableAfter)
	}
	if event.Trigger.Deadline != event.LateUntil {
		t.Fatalf("deadline %s and late_until %s must agree", event.Trigger.Deadline, event.LateUntil)
	}
	names := []string{}
	for _, tier := range event.Ladder {
		names = append(names, tier.Name)
		if tier.OnTimeout != "advance" || !tier.AlertOnFallback || tier.Args["providers"] != tier.Name {
			t.Fatalf("tier %+v must advance on timeout, alert on fallback and read only its own provider", tier)
		}
	}
	if strings.Join(names, ",") != "wencai,q2_final" {
		t.Fatalf("rung order %v", names)
	}
	// no other production event gained the new semantics
	for _, e := range c.Events {
		if e.ID == event.ID {
			continue
		}
		for _, tier := range e.Ladder {
			if tier.OnTimeout != "" || tier.AlertOnFallback {
				t.Fatalf("event %s tier %s must not use the C566 semantics", e.ID, tier.Name)
			}
		}
	}
}

func TestC566Scenarios(t *testing.T) {
	type want struct {
		status string
		tier   string
		calls  string
		alerts int // exact alert count
		fb     bool
	}
	cases := []struct {
		name     string
		start    string
		behavior map[string]string
		mutate   func(*config.Event)
		want     want
	}{
		{"normal 15:05 wencai ok", "15:05:00", map[string]string{"wencai": "ok10"}, nil, want{"SUCCEEDED_DEGRADED", "wencai", "wencai", 0, false}},
		{"late start 15:30 backfills", "15:30:00", map[string]string{"wencai": "ok10"}, nil, want{"SUCCEEDED_DEGRADED", "wencai", "wencai", 0, false}},
		{"late start 16:50 backfills", "16:50:00", map[string]string{"wencai": "ok10"}, nil, want{"SUCCEEDED_DEGRADED", "wencai", "wencai", 0, false}},
		{"late start 17:34:00 still backfills", "17:34:00", map[string]string{"wencai": "ok10"}, nil, want{"SUCCEEDED_DEGRADED", "wencai", "wencai", 0, false}},
		{"wencai no data -> q2 silently", "15:05:00", map[string]string{"wencai": "nodata40", "q2_final": "ok10"}, nil, want{"SUCCEEDED_DEGRADED", "q2_final", "wencai,q2_final", 0, false}},
		{"wencai date mismatch -> q2 silently", "15:05:00", map[string]string{"wencai": "mismatch40", "q2_final": "ok10"}, nil, want{"SUCCEEDED_DEGRADED", "q2_final", "wencai,q2_final", 0, false}},
		{"wencai timeout -> q2 without same-tier retry", "15:05:00", map[string]string{"wencai": "hang", "q2_final": "ok10"}, nil, want{"SUCCEEDED_DEGRADED", "q2_final", "wencai,q2_final", 0, false}},
		{"wencai deterministic -> q2 and one alert", "15:05:00", map[string]string{"wencai": "det40", "q2_final": "ok10"}, nil, want{"SUCCEEDED_DEGRADED", "q2_final", "wencai,q2_final", 1, true}},
		{"retryable partial retries in tier then q2", "15:05:00", map[string]string{"wencai": "retry30", "q2_final": "ok10"}, nil, want{"SUCCEEDED_DEGRADED", "q2_final", "wencai,wencai,q2_final", 0, false}},
		{"both tiers fail -> FAILED_FINAL one alert", "15:05:00", map[string]string{"wencai": "det40", "q2_final": "det40"}, nil, want{"FAILED_FINAL", "q2_final", "wencai,q2_final", 1, false}},
		{"both tiers no data -> FAILED_FINAL one alert", "15:05:00", map[string]string{"wencai": "nodata40", "q2_final": "nodata40"}, nil, want{"FAILED_FINAL", "q2_final", "wencai,q2_final", 1, false}},
		{"existing equal-or-better version skips", "15:05:00", map[string]string{"wencai": "satisfied20"}, nil, want{"SKIPPED_SATISFIED", "wencai", "wencai", 0, false}},
		{"reordering the ladder in config runs q2_final first", "15:05:00", map[string]string{"wencai": "ok10", "q2_final": "ok10"}, func(e *config.Event) {
			e.Ladder[0], e.Ladder[1] = e.Ladder[1], e.Ladder[0]
		}, want{"SUCCEEDED_DEGRADED", "q2_final", "q2_final", 0, false}},
		{"on_timeout retry restores same-tier retry", "15:05:00", map[string]string{"wencai": "hang", "q2_final": "ok10"}, func(e *config.Event) {
			e.Ladder[0].OnTimeout = "retry"
		}, want{"SUCCEEDED_DEGRADED", "q2_final", "wencai,wencai,q2_final", 0, false}},
		{"alert_on_fallback off is silent", "15:05:00", map[string]string{"wencai": "det40", "q2_final": "ok10"}, func(e *config.Event) {
			e.Ladder[0].AlertOnFallback = false
		}, want{"SUCCEEDED_DEGRADED", "q2_final", "wencai,q2_final", 0, false}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := c566Setup(t, tc.mutate, tc.behavior, tc.start)
			defer s.closeFn()
			state := s.run(t)
			calls := strings.Join(s.tiers(t), ",")
			alerts := s.alertReasons(t)
			if state.Status != tc.want.status || state.Tier != tc.want.tier || calls != tc.want.calls || len(alerts) != tc.want.alerts {
				t.Fatalf("state=%+v calls=%s alerts=%v want=%+v", state, calls, alerts, tc.want)
			}
			if tc.want.fb {
				if !strings.HasPrefix(alerts[0], "TIER_FALLBACK|") || !strings.Contains(alerts[0], "wencai:DETERMINISTIC_FAILURE") || !strings.Contains(alerts[0], `"succeeded_tier":"q2_final"`) {
					t.Fatalf("fallback alert must name the failed and the succeeded tier: %s", alerts[0])
				}
			}
			if tc.name == "wencai timeout -> q2 without same-tier retry" {
				if got := ledgerTypeCounts(t, s.root, c566Date)["TIER_FAILED"]; got != 1 {
					t.Fatalf("TIER_FAILED=%d want 1", got)
				}
			}
		})
	}
}

func TestC566StartAfterWindowIsMissedAndNeverFetches(t *testing.T) {
	s := c566Setup(t, nil, map[string]string{"wencai": "ok10"}, "17:35:30")
	defer s.closeFn()
	s.r.schedule(s.fc.Now())
	state := runtimeTestState(s.r, c566Date+"/"+s.event.ID)
	if state.Status != "MISSED" || len(s.tiers(t)) != 0 {
		t.Fatalf("state=%+v calls=%v, want MISSED without any fetch", state, s.tiers(t))
	}
}

func TestC566FallbackFailuresSurviveRestart(t *testing.T) {
	// the deterministic failure is recorded in the persisted state, so a daemon restart between rungs still alerts once
	s := c566Setup(t, nil, map[string]string{"wencai": "det40", "q2_final": "ok10"}, "15:05:00")
	defer s.closeFn()
	state := s.run(t)
	if len(state.FallbackFailures) != 1 || state.FallbackFailures[0] != "wencai:DETERMINISTIC_FAILURE" {
		t.Fatalf("fallback failures not persisted: %+v", state)
	}
}
