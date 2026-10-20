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
