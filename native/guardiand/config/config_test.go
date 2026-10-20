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
