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
