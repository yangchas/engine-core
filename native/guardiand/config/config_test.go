package config

import "testing"

func TestStrictDecodeAndVersion(t *testing.T) {
	if _, issues := Decode([]byte(`{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","events":[],"surprise":true}`)); len(issues) == 0 || issues[0].Code != "DECODE" {
		t.Fatalf("unknown field accepted: %#v", issues)
	}
	if err := CheckAgainstApplied(2, "a", 2, "b"); err == nil {
		t.Fatal("same version with changed hash accepted")
	}
}

func TestLadderConfigSchemaAccepted(t *testing.T) {
	input := []byte(`{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","events":[{"id":"tiered","trigger":{"type":"window","start":"09:00:00","deadline":"09:10:00"},"ladder":[{"name":"redis","args":{"source":"redis"},"attempts":2,"backoff":"2s*2","timeout":"3s"},{"name":"td","args":{"source":"td"},"attempts":1,"timeout":"5s"}]}]}`)
	if _, issues := Decode(input); len(issues) != 0 {
		t.Fatalf("valid ladder config rejected: %+v", issues)
	}
}

func TestHeavyLoad5AndDeferralPollLimitValidate(t *testing.T) {
	valid := []byte(`{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","heavy_max_load5":3,"heavy_load_defer_max_polls":6,"events":[]}`)
	if _, issues := Decode(valid); len(issues) != 0 {
		t.Fatalf("valid load5 gate rejected: %+v", issues)
	}
	invalid := []byte(`{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","heavy_max_load5":3,"heavy_load_defer_max_polls":0,"events":[]}`)
	_, issues := Decode(invalid)
	for _, issue := range issues {
		if issue.Path == "heavy_load_defer_max_polls" && issue.Code == "LOAD_DEFER_POLLS" {
			return
		}
	}
	t.Fatalf("zero deferral poll limit was accepted: %+v", issues)
}

func TestAtTimesMustRemainDistinctAfterInstanceExpansion(t *testing.T) {
	c := Config{SchemaVersion: 1, ConfigVersion: 1, Name: "x", Timezone: "Asia/Shanghai", Events: []Event{{
		ID: "two_triggers", Trigger: Trigger{Type: "window", Start: "09:00:00", Deadline: "09:10:00", AtTimes: []string{"09:00:00", "09:00:01"}},
	}}}
	for _, issue := range Validate(c) {
		if issue.Path == "events[0].trigger.at_times" && issue.Code == "TRIGGER_DUPLICATE" {
			return
		}
	}
	t.Fatal("trigger points that expand to the same HH:MM instance were accepted")
}

func TestTimeoutIsIndependentOfWindow(t *testing.T) {
	c := Config{SchemaVersion: 1, ConfigVersion: 1, Name: "x", Events: []Event{{
		ID: "long", Trigger: Trigger{Type: "window", Start: "09:00:00", Deadline: "09:01:00"}, Timeout: "300s",
	}}}
	for _, issue := range Validate(c) {
		if issue.Code == "TIMEOUT_WINDOW" {
			t.Fatalf("runtime budget was constrained by start deadline: %+v", issue)
		}
	}
}

func TestRetryUntilLateFieldsDecodeAndBackoffLimit(t *testing.T) {
	valid := []byte(`{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","events":[{"id":"data","trigger":{"type":"window","start":"09:00:00","deadline":"09:01:00"},"late_until":"10:00:00","retry":{"until_late":true,"backoff":"10m","silent_until_last":true}}]}`)
	c, issues := Decode(valid)
	if len(issues) != 0 || !c.Events[0].Retry.UntilLate || c.Events[0].Retry.Backoff != "10m" || !c.Events[0].Retry.SilentUntilLast {
		t.Fatalf("retry settings not decoded: event=%+v issues=%+v", c.Events[0], issues)
	}
	invalid := []byte(`{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","events":[{"id":"data","trigger":{"type":"window","start":"09:00:00","deadline":"09:01:00"},"retry":{"backoff":"11m"}}]}`)
	_, issues = Decode(invalid)
	for _, issue := range issues {
		if issue.Path == "events[0].retry.backoff" && issue.Code == "DURATION" {
			return
		}
	}
	t.Fatalf("backoff above 10m was accepted: %+v", issues)
}

func TestRetryConfigurationRejectsInvalidAttemptAndBase(t *testing.T) {
	invalid := []byte(`{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","events":[{"id":"bad","trigger":{"type":"window","start":"09:00:00","deadline":"09:01:00"},"retry":{"max_attempts":-1,"base":"6m","until_late":true}}]}`)
	_, issues := Decode(invalid)
	found := map[string]bool{}
	for _, issue := range issues {
		found[issue.Code] = true
	}
	if !found["RETRY_ATTEMPTS"] || !found["DURATION"] || !found["LATE_UNTIL_REQUIRED"] {
		t.Fatalf("invalid retry settings were not all rejected: %+v", issues)
	}
}

func TestPhasesAndStartupTriggerConfig(t *testing.T) {
	c := Config{SchemaVersion: 1, ConfigVersion: 1, Name: "x", Timezone: "Asia/Shanghai", Events: []Event{{
		ID: "boot", Trigger: Trigger{Type: "startup", Order: 10},
	}}}
	if issues := Validate(c); len(issues) != 0 {
		t.Fatalf("defaults/startup rejected: %+v", issues)
	}
	c.Phases = []Phase{{Name: "A", Start: "00:00", End: "12:00"}, {Name: "B", Start: "12:00", End: "24:00"}}
	if issues := Validate(c); len(issues) != 0 {
		t.Fatalf("contiguous phases rejected: %+v", issues)
	}
	c.Phases[1].Start = "11:00"
	if issues := Validate(c); len(issues) == 0 {
		t.Fatal("overlapping phases accepted")
	}
}

func TestCostPhaseMismatchAndHeavyLoadConfig(t *testing.T) {
	valid := []byte(`{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","heavy_max_load1":"cpus","events":[{"id":"heavy","trigger":{"type":"window","start":"09:00:00","deadline":"09:01:00"},"cost":"heavy","on_phase_mismatch":"defer","phases":["NIGHT"]}]}`)
	if _, issues := Decode(valid); len(issues) != 0 {
		t.Fatalf("valid scheduling config rejected: %+v", issues)
	}
	invalid := []byte(`{"schema_version":1,"config_version":1,"name":"x","timezone":"Asia/Shanghai","heavy_max_load1":-1,"events":[{"id":"heavy","trigger":{"type":"window","start":"09:00:00","deadline":"09:01:00"},"cost":"huge","on_phase_mismatch":"later"}]}`)
	_, issues := Decode(invalid)
	if len(issues) < 3 {
		t.Fatalf("invalid scheduling config accepted or underreported: %+v", issues)
	}
}
