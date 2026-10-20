package main

import "testing"

func TestClampTimeoutToWindow(t *testing.T) {
	got, changed := clampTimeout("09:00:00", "09:02:00", 300)
	if !changed || got != 119 {
		t.Fatalf("clampTimeout() = %d, %v; want 119, true", got, changed)
	}
	got, changed = clampTimeout("09:00:00", "09:02:00", 60)
	if changed || got != 60 {
		t.Fatalf("non-clamped timeout = %d, %v; want 60, false", got, changed)
	}
}
