package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

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
	source := filepath.Join("../../../engine_core/src/engine_core/guardian/schedule.json")
	dst := filepath.Join(t.TempDir(), "config.json")
	if err := migrateCmd([]string{"--from", source, "--to", dst, "--expand"}); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	var got struct {
		HeavyMaxLoad1 float64           `json:"heavy_max_load1"`
		HeavyCutoffs  map[string]string `json:"heavy_cutoffs"`
		Events        []struct {
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
	if got.HeavyMaxLoad1 != 3 || got.HeavyCutoffs["PRE_MARKET"] != "09:10:00" || len(got.Events) != 55 {
		t.Fatalf("global config/events lost: maxload=%v cutoffs=%v count=%d", got.HeavyMaxLoad1, got.HeavyCutoffs, len(got.Events))
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
	if event := byID["boot_trade_day"]; event.Trigger.Type != "startup" || event.Trigger.Order != 10 || event.Handler != "legacy_boot_trade_day" {
		t.Fatalf("startup event changed: %+v", event)
	}
	if event := byID["startup_0830"]; event.Trigger.Start != "08:30:00" || event.LateUntil != "09:10:00" || event.Cost != "medium" || !event.Retry.UntilLate {
		t.Fatalf("window strategy changed: %+v", event)
	}
}
