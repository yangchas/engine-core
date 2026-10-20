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
