package main

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"guardiand/config"
)

func TestResolvePreviousTradeDayUsesCapturedTradingCalendar(t *testing.T) {
	fixture := filepath.Join("..", "..", "tests", "fixtures", "trade_calendar", "baostock-2026-01-01_2026-12-31.json")
	fixtureBytes, err := os.ReadFile(fixture)
	if err != nil {
		t.Fatal(err)
	}
	var captured struct {
		Rows []struct {
			Date        string `json:"calendar_date"`
			TradingFlag string `json:"is_trading_day"`
		} `json:"rows"`
	}
	if err := json.Unmarshal(fixtureBytes, &captured); err != nil {
		t.Fatal(err)
	}
	var tradingDays []string
	for _, row := range captured.Rows {
		if row.TradingFlag == "1" {
			tradingDays = append(tradingDays, row.Date)
		}
	}
	calendar := filepath.Join(t.TempDir(), "trade_dates.json")
	calendarBytes, err := json.Marshal(tradingDays)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(calendar, calendarBytes, 0600); err != nil {
		t.Fatal(err)
	}
	c := config.Config{Calendars: map[string]struct {
		File string `json:"file"`
	}{"trading": {File: calendar}}}
	e := &config.Event{Args: map[string]any{"target": "prev_trade_day"}}
	cases := []struct {
		name, eventDate, want string
		wantError             bool
	}{
		{name: "monday", eventDate: "2026-10-26", want: "2026-10-23"},
		{name: "saturday", eventDate: "2026-10-24", want: "2026-10-23"},
		{name: "sunday", eventDate: "2026-10-25", want: "2026-10-23"},
		{name: "first day after national holiday", eventDate: "2026-10-08", want: "2026-09-30"},
		{name: "first day after mid autumn holiday", eventDate: "2026-09-28", want: "2026-09-24"},
		{name: "outside calendar coverage", eventDate: "2027-01-04", wantError: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := resolveDataDate(c, e, Instance{}, tc.eventDate)
			if tc.wantError {
				if err == nil || !strings.Contains(err.Error(), "TARGET_DATE_UNRESOLVED") {
					t.Fatalf("resolveDataDate error=%v, want TARGET_DATE_UNRESOLVED", err)
				}
				return
			}
			if err != nil || got != tc.want {
				t.Fatalf("resolveDataDate=%q, %v; want %q", got, err, tc.want)
			}
		})
	}
	legacyEvent := &config.Event{DataDateOffset: -1}
	legacyDate, err := resolveDataDate(c, legacyEvent, Instance{DataDateOffset: -1}, "2026-10-26")
	if err != nil || legacyDate != "2026-10-23" {
		t.Fatalf("legacy data_date_offset=-1 resolved %q, %v; want 2026-10-23", legacyDate, err)
	}
}

func TestCompileScheduleV3ProfilesAtListAndLegacyFields(t *testing.T) {
	source := []byte(`{"schema_version":3,"timezone":"Asia/Shanghai","profiles":{"auction":{"catchup":"run_in_window","window":{"deadline":"09:30"},"ladder":[{"name":"redis","args":{"source":"redis"},"attempts":2,"backoff":"2s*2","timeout":"3s"},{"name":"td","args":{"source":"td"},"attempts":1,"timeout":"5s"}]}},"events":[{"id":"auction_report","at":["09:25","09:26:30"],"deadline":"09:29","handler":"auction_report","profile":"auction"},{"id":"legacy_report","at":"09:27","deadline":"09:29","handler":"auction_report","timeout_seconds":8,"max_attempts":3,"retry_backoff":"2s","retry_until_late":true,"late_until":"10:00","catchup_mode":"run_in_window"}]}`)
	c, err := compileScheduleV3(source)
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Events) != 2 || c.Events[0].Trigger.AtTimes[1] != "09:26:30" || len(c.Events[0].Ladder) != 2 {
		t.Fatalf("profile/list not compiled: %+v", c.Events)
	}
	if c.Events[0].WindowSource != "event" || c.Events[0].LadderSource != "profile:auction" || c.Events[0].CatchupSource != "profile:auction" {
		t.Fatalf("effective sources not recorded: %+v", c.Events[0])
	}
	legacy := c.Events[1]
	if len(legacy.Ladder) != 1 || legacy.Ladder[0].Attempts != 3 || legacy.Ladder[0].Until != "late_until" || legacy.Ladder[0].BudgetEnabled == nil || *legacy.Ladder[0].BudgetEnabled {
		t.Fatalf("legacy fields not downgraded: %+v", legacy.Ladder)
	}
}

func TestScheduleV3ProfileRetainsLegacyRetryAndLadderFields(t *testing.T) {
	source := []byte(`{"schema_version":3,"timezone":"Asia/Shanghai","profiles":{"morning":{"catchup":"run_in_window","retry_backoff":"60s","retry_until_late":true,"silent_until_last":false,"window":{"late_until":"14:00"},"ladder":[{"name":"legacy","attempts":3,"backoff":"60s*2","backoff_cap":"10m","timeout":"300s","until":"late_until","budget_enabled":false}]}},"events":[{"id":"profiled","at":"09:00","deadline":"09:05","handler":"auction_report","profile":"morning","args":{"source":"td"}}]}`)
	c, err := compileScheduleV3(source)
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Events) != 1 {
		t.Fatalf("compiled events=%d", len(c.Events))
	}
	e := c.Events[0]
	if e.Retry.MaxAttempts != 3 || e.Retry.Backoff != "60s" || !e.Retry.UntilLate || e.Retry.SilentUntilLast {
		t.Fatalf("profile retry metadata=%+v", e.Retry)
	}
	if e.LateUntil != "14:00:00" || e.Catchup != "run_in_window" {
		t.Fatalf("profile event policy late_until=%q catchup=%q", e.LateUntil, e.Catchup)
	}
	if len(e.Ladder) != 1 {
		t.Fatalf("profile ladder length=%d", len(e.Ladder))
	}
	rung := e.Ladder[0]
	if rung.Args["source"] != "td" || rung.Backoff != "60s*2" || rung.BackoffCap != "10m" || rung.Timeout != "300s" || rung.Until != "late_until" || rung.BudgetEnabled == nil || *rung.BudgetEnabled {
		t.Fatalf("compiled profile rung=%+v", rung)
	}
}

func TestDisabledScheduleEventDoesNotCreateInstances(t *testing.T) {
	source := []byte(`{"schema_version":3,"timezone":"Asia/Shanghai","events":[{"id":"daily_kline_backfill","enabled":false,"calendar":"every_day","at":["01:00","08:00"],"deadline":"08:15","handler":"selftest","args":{"target":"prev_trade_day"},"profile":"kline"}],"profiles":{"kline":{"ladder":[{"name":"primary","attempts":3,"backoff":"1m*2","timeout":"5m"}]}}}`)
	c, err := compileScheduleV3(source)
	if err != nil {
		t.Fatal(err)
	}
	xs, _, err := instances(c, "2026-10-26")
	if err != nil {
		t.Fatal(err)
	}
	if len(xs) != 0 {
		t.Fatalf("disabled event created instances: %+v", xs)
	}
}

func TestCompileScheduleV3RejectsInvalidProfilesAndTiers(t *testing.T) {
	base := `{"schema_version":3,"timezone":"Asia/Shanghai","events":[{"id":"e","at":"09:00","deadline":"09:05","handler":"auction_report","profile":"p"}],"profiles":{"p":{"ladder":[{"name":"redis","attempts":1,"timeout":"2s"}]}}}`
	cases := []struct{ name, input, want string }{
		{"unknown_profile", strings.Replace(base, `"profile":"p"`, `"profile":"missing"`, 1), "unknown profile"},
		{"unknown_field", strings.Replace(base, `"attempts":1`, `"attempts":1,"extra":true`, 1), "unknown field"},
		{"empty_ladder", strings.Replace(base, `[{"name":"redis","attempts":1,"timeout":"2s"}]`, `[]`, 1), "empty ladder"},
		{"invalid_backoff", strings.Replace(base, `"attempts":1`, `"attempts":2,"backoff":"nonsense"`, 1), "backoff"},
		{"missing_event_tier_timeout", `{"schema_version":3,"timezone":"Asia/Shanghai","events":[{"id":"e","at":"09:00","deadline":"09:05","handler":"auction_report","timeout_seconds":2,"ladder":[{"name":"redis","attempts":1}]}]}`, "timeout"},
		{"duplicate_rung", strings.Replace(base, `[{"name":"redis","attempts":1,"timeout":"2s"}]`, `[{"name":"redis","attempts":1,"timeout":"2s"},{"name":"redis","attempts":1,"timeout":"2s"}]`, 1), "unique"},
		{"invalid_at", strings.Replace(base, `"at":"09:00"`, `"at":"25:00"`, 1), "invalid time"},
		{"duplicate_at", strings.Replace(base, `"at":"09:00"`, `"at":["09:00","09:00:00"]`, 1), "duplicate at time"},
		{"unused_empty_profile", strings.Replace(base, `"p":{"ladder":[{"name":"redis","attempts":1,"timeout":"2s"}]}`, `"p":{"ladder":[{"name":"redis","attempts":1,"timeout":"2s"}]},"unused":{"ladder":[]}`, 1), "empty ladder"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := compileScheduleV3([]byte(tc.input))
			if err == nil || !strings.Contains(strings.ToLower(err.Error()), strings.ToLower(tc.want)) {
				t.Fatalf("error=%v, want substring %q", err, tc.want)
			}
		})
	}
}

func TestExplainShowsEffectiveHandlerAndEventArgs(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "config.json")
	source := `{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","handlers":{"auction_report":{"argv":["/bin/true"]}},"events":[{"id":"report","handler":"auction_report","args":{"source":"event"},"timeout":"2s","on_deadline":"missed","trigger":{"type":"window","start":"09:00:00","deadline":"09:05:00","at_times":["09:00:00"]},"ladder":[{"name":"redis","attempts":1,"timeout":"2s","args":{"source":"redis"}}]}]}`
	if err := os.WriteFile(path, []byte(source), 0600); err != nil {
		t.Fatal(err)
	}
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	defer func() { os.Stdout = old }()
	if err := explainCmd([]string{path, "--date", "2026-10-20", "--json"}); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	os.Stdout = old
	b, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	var rows []map[string]any
	if err := json.Unmarshal(b, &rows); err != nil {
		t.Fatalf("explain JSON=%s: %v", b, err)
	}
	if len(rows) != 1 || rows[0]["handler"] != "auction_report" || rows[0]["on_deadline"] != "missed" {
		t.Fatalf("effective handler/deadline missing from explain: %s", b)
	}
	args, ok := rows[0]["event_args"].(map[string]any)
	if !ok || args["source"] != "event" {
		t.Fatalf("effective event args missing from explain: %s", b)
	}
}

func TestMigrationPreservesTimeoutBeyondWindow(t *testing.T) {
	dir := t.TempDir()
	src, dst := filepath.Join(dir, "schedule.json"), filepath.Join(dir, "config.json")
	legacy := `{"events":[{"event_id":"long_budget","start":"09:00:00","deadline":"09:01:00","timeout_seconds":300,"max_attempts":1}]}`
	if err := os.WriteFile(src, []byte(legacy), 0600); err != nil {
		t.Fatal(err)
	}
	if err := migrateCmd([]string{"--from", src, "--to", dst, "--expand"}); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	var got struct {
		Events []struct {
			Timeout string `json:"timeout"`
		} `json:"events"`
	}
	if err = json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	if len(got.Events) != 1 || got.Events[0].Timeout != "300s" {
		t.Fatalf("migrated timeout=%+v, want 300s independent of 60s window", got.Events)
	}
}

func TestMigrationV2PreservesConfiguredStartupAndWindowPolicies(t *testing.T) {
	// schedule_v2.json is the pre-C550 source schedule at c9b69f8^:
	// src/engine_core/guardian/schedule.json (blob 8a2462b9fd406eefd089f329f8afce3d15d5b740).
	sources := []struct {
		name      string
		path      string
		wantCount int
		wantLoad1 bool
	}{
		{name: "v2 fixture", path: filepath.Join("testdata", "schedule_v2.json"), wantCount: 62, wantLoad1: true},
		{name: "current v3", path: filepath.Join("../../../engine_core/src/engine_core/guardian/schedule.json"), wantCount: 64},
	}
	for _, source := range sources {
		t.Run(source.name, func(t *testing.T) {
			dst := filepath.Join(t.TempDir(), "config.json")
			if err := migrateCmd([]string{"--from", source.path, "--to", dst, "--expand"}); err != nil {
				t.Fatal(err)
			}
			b, err := os.ReadFile(dst)
			if err != nil {
				t.Fatal(err)
			}
			var got struct {
				HeavyMaxLoad1          float64           `json:"heavy_max_load1"`
				HeavyMaxLoad5          float64           `json:"heavy_max_load5"`
				HeavyLoadDeferMaxPolls int               `json:"heavy_load_defer_max_polls"`
				HeavyCutoffs           map[string]string `json:"heavy_cutoffs"`
				Events                 []struct {
					ID      string `json:"id"`
					Handler string `json:"handler"`
					Trigger struct {
						Type  string `json:"type"`
						Order int    `json:"order"`
						Start string `json:"start"`
					} `json:"trigger"`
					LateUntil string `json:"late_until"`
					Cost      string `json:"cost"`
					Retry     struct {
						MaxAttempts int    `json:"max_attempts"`
						Backoff     string `json:"backoff"`
						UntilLate   bool   `json:"until_late"`
					} `json:"retry"`
				} `json:"events"`
			}
			if err = json.Unmarshal(b, &got); err != nil {
				t.Fatal(err)
			}
			loadThreshold := got.HeavyMaxLoad5
			if source.wantLoad1 {
				loadThreshold = got.HeavyMaxLoad1
			} else if got.HeavyLoadDeferMaxPolls != 6 {
				t.Fatalf("load deferral poll cap=%d, want 6", got.HeavyLoadDeferMaxPolls)
			}
			if loadThreshold != 3 || got.HeavyCutoffs["PRE_MARKET"] != "09:10:00" || len(got.Events) != source.wantCount {
				t.Fatalf("global config/events lost: maxload=%v cutoffs=%v count=%d want=%d", loadThreshold, got.HeavyCutoffs, len(got.Events), source.wantCount)
			}
			byID := map[string]struct {
				ID      string `json:"id"`
				Handler string `json:"handler"`
				Trigger struct {
					Type  string `json:"type"`
					Order int    `json:"order"`
					Start string `json:"start"`
				} `json:"trigger"`
				LateUntil string `json:"late_until"`
				Cost      string `json:"cost"`
				Retry     struct {
					MaxAttempts int    `json:"max_attempts"`
					Backoff     string `json:"backoff"`
					UntilLate   bool   `json:"until_late"`
				} `json:"retry"`
			}{}
			for _, event := range got.Events {
				byID[event.ID] = event
			}
			for id, start := range map[string]string{"open_report_0932": "09:32:10", "open_report_0935": "09:35:10",
				"open_report_0940": "09:40:10", "open_report_0945": "09:45:30"} {
				if event, ok := byID[id]; !ok || event.Trigger.Start != start {
					t.Fatalf("open report %s start=%q present=%v, want %q", id, event.Trigger.Start, ok, start)
				}
			}
			if _, ok := byID["open_report_0938"]; ok {
				t.Fatal("0938 must remain an internal data checkpoint, not a scheduled report")
			}
			if event := byID["boot_trade_day"]; event.Trigger.Type != "startup" || event.Trigger.Order != 10 || event.Handler != "legacy_boot_trade_day" {
				t.Fatalf("startup event changed: %+v", event)
			}
			if event := byID["startup_0830"]; event.Trigger.Start != "08:30:00" || event.LateUntil != "09:10:00" || event.Cost != "medium" || !event.Retry.UntilLate {
				t.Fatalf("window strategy changed: %+v", event)
			}
		})
	}
}
