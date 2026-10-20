package config

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"time"
)

type Config struct {
	SchemaVersion int    `json:"schema_version"`
	ConfigVersion int    `json:"config_version"`
	Name          string `json:"name"`
	Timezone      string `json:"timezone"`
	Calendars     map[string]struct {
		File string `json:"file"`
	} `json:"calendars,omitempty"`
	Concurrency map[string]json.RawMessage `json:"concurrency,omitempty"`
	Handlers    map[string]Handler         `json:"handlers,omitempty"`
	Alerts      map[string]json.RawMessage `json:"alerts,omitempty"`
	Events      []Event                    `json:"events"`
	Datasets    json.RawMessage            `json:"datasets,omitempty"`
}
type Handler struct {
	Argv            []string `json:"argv,omitempty"`
	GateArgv        []string `json:"gate_argv,omitempty"`
	Cwd             string   `json:"cwd,omitempty"`
	EnvAllow        []string `json:"env_allow,omitempty"`
	SandboxSafe     bool     `json:"sandbox_safe,omitempty"`
	MaxOutputBytes  int      `json:"max_output_bytes,omitempty"`
	SupportsDegrade bool     `json:"supports_degrade,omitempty"`
}
type Trigger struct {
	Type     string `json:"type"`
	Start    string `json:"start,omitempty"`
	Deadline string `json:"deadline,omitempty"`
	Until    string `json:"until,omitempty"`
	Every    string `json:"every,omitempty"`
	Offset   string `json:"offset,omitempty"`
	Window   string `json:"window,omitempty"`
	At       string `json:"time,omitempty"`
}
type Gate struct {
	Ref        string         `json:"ref,omitempty"`
	Params     map[string]any `json:"params,omitempty"`
	Poll       string         `json:"poll,omitempty"`
	BackoffMax string         `json:"backoff_max,omitempty"`
}
type Retry struct {
	MaxAttempts      int      `json:"max_attempts,omitempty"`
	Base             string   `json:"base,omitempty"`
	Max              string   `json:"max,omitempty"`
	Jitter           float64  `json:"jitter,omitempty"`
	RetryOn          []string `json:"retry_on,omitempty"`
	NoRetryOnTimeout bool     `json:"no_retry_on_timeout,omitempty"`
}
type Dependency struct {
	ID             string `json:"id"`
	AcceptDegraded bool   `json:"accept_degraded,omitempty"`
}
type Event struct {
	ID             string          `json:"id"`
	Enabled        bool            `json:"enabled,omitempty"`
	Description    string          `json:"description,omitempty"`
	Calendar       json.RawMessage `json:"calendar,omitempty"`
	Trigger        Trigger         `json:"trigger"`
	DataDateOffset int             `json:"data_date_offset,omitempty"`
	AvailableAfter *string         `json:"available_after,omitempty"`
	Handler        string          `json:"handler,omitempty"`
	Args           map[string]any  `json:"args,omitempty"`
	Gate           Gate            `json:"gate,omitempty"`
	Timeout        string          `json:"timeout,omitempty"`
	Retry          Retry           `json:"retry,omitempty"`
	OnDeadline     string          `json:"on_deadline,omitempty"`
	Catchup        string          `json:"catchup,omitempty"`
	Class          string          `json:"class,omitempty"`
	Priority       int             `json:"priority,omitempty"`
	Alert          json.RawMessage `json:"alert,omitempty"`
	DependsOn      []Dependency    `json:"depends_on,omitempty"`
}
type Issue struct {
	Path    string `json:"path"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

var idRE = regexp.MustCompile(`^[a-zA-Z][a-zA-Z0-9_-]*$`)
var vars = map[string]bool{"trade_date": true, "data_date": true, "asof": true, "event_id": true, "instance": true, "attempt": true, "run_id": true, "sandbox_root": true, "python": true}
var templateRE = regexp.MustCompile(`\{([^{}]+)\}`)

func Decode(b []byte) (Config, []Issue) {
	var c Config
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if err := d.Decode(&c); err != nil {
		return c, []Issue{{"$", "DECODE", err.Error()}}
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return c, []Issue{{"$", "DECODE", "trailing JSON value"}}
	}
	return c, Validate(c)
}
func Load(path string) (Config, string, []Issue, error) {
	b, e := os.ReadFile(path)
	if e != nil {
		return Config{}, "", nil, e
	}
	c, issues := Decode(b)
	canonical, e := json.Marshal(c)
	if e != nil {
		return c, "", issues, e
	}
	h := sha256.Sum256(canonical)
	return c, hex.EncodeToString(h[:]), issues, nil
}
func Validate(c Config) []Issue {
	var out []Issue
	add := func(p, code, msg string) { out = append(out, Issue{p, code, msg}) }
	if c.SchemaVersion != 1 {
		add("schema_version", "SCHEMA_VERSION", "must be 1")
	}
	if c.ConfigVersion < 1 {
		add("config_version", "CONFIG_VERSION", "must be positive")
	}
	if c.Name == "" {
		add("name", "REQUIRED", "required")
	}
	if c.Timezone != "" {
		if _, e := time.LoadLocation(c.Timezone); e != nil {
			add("timezone", "TIMEZONE", e.Error())
		}
	}
	ids := map[string]int{}
	by := map[string]Event{}
	for i, e := range c.Events {
		p := fmt.Sprintf("events[%d]", i)
		if !idRE.MatchString(e.ID) {
			add(p+".id", "ID_FORMAT", "invalid event id")
		}
		if _, ok := ids[e.ID]; ok {
			add(p+".id", "DUPLICATE_ID", "duplicate event id")
		}
		ids[e.ID] = i
		by[e.ID] = e
		checkTime := func(path, s string) int {
			t, err := time.Parse("15:04:05", s)
			if err != nil {
				add(path, "TIME_FORMAT", "expected HH:MM:SS")
				return -1
			}
			return t.Hour()*3600 + t.Minute()*60 + t.Second()
		}
		start, end := -1, -1
		switch e.Trigger.Type {
		case "window":
			start = checkTime(p+".trigger.start", e.Trigger.Start)
			end = checkTime(p+".trigger.deadline", e.Trigger.Deadline)
		case "at":
			start = checkTime(p+".trigger.time", e.Trigger.At)
			if e.Trigger.Window == "" {
				add(p+".trigger.window", "REQUIRED", "required")
			}
		case "interval":
			start = checkTime(p+".trigger.start", e.Trigger.Start)
			end = checkTime(p+".trigger.until", e.Trigger.Until)
			if _, err := time.ParseDuration(e.Trigger.Every); err != nil || durationSeconds(e.Trigger.Every) <= 0 {
				add(p+".trigger.every", "DURATION", "invalid positive duration")
			}
			if e.Trigger.Window == "" {
				add(p+".trigger.window", "REQUIRED", "required")
			}
		default:
			add(p+".trigger.type", "TRIGGER_TYPE", "unsupported trigger type")
		}
		if start >= 0 && end >= 0 && start > end {
			add(p+".trigger.deadline", "WINDOW_ORDER", "start must not exceed deadline")
		}
		win := int64(end - start)
		if e.Trigger.Type == "at" {
			win = durationSeconds(e.Trigger.Window)
		}
		if e.Trigger.Type == "interval" {
			win = durationSeconds(e.Trigger.Window)
		}
		if e.Timeout != "" {
			to := durationSeconds(e.Timeout)
			if to <= 0 {
				add(p+".timeout", "DURATION", "invalid positive duration")
			} else if win >= 0 && to >= win {
				add(p+".timeout", "TIMEOUT_WINDOW", "timeout must be shorter than window")
			}
		}
		if e.Handler != "" {
			if _, ok := c.Handlers[e.Handler]; !ok {
				add(p+".handler", "UNKNOWN_HANDLER", "handler is not declared")
			}
		}
		for _, s := range append(append([]string{}, c.Handlers[e.Handler].Argv...), flatten(e.Args)...) {
			for _, m := range templateRE.FindAllStringSubmatch(s, -1) {
				if !vars[m[1]] {
					add(p+".args", "TEMPLATE_VARIABLE", "unknown template variable {"+m[1]+"}")
				}
			}
		}
		if e.OnDeadline == "degrade" && !c.Handlers[e.Handler].SupportsDegrade {
			add(p+".on_deadline", "DEGRADE_UNSUPPORTED", "handler must support degrade")
		}
		if e.OnDeadline != "" && e.OnDeadline != "degrade" && e.OnDeadline != "kill_missed" && e.OnDeadline != "missed" {
			add(p+".on_deadline", "ENUM", "invalid on_deadline")
		}
		if e.Catchup != "" && e.Catchup != "skip" && e.Catchup != "run_in_window" {
			add(p+".catchup", "ENUM", "invalid catchup")
		}
		if e.Gate.Ref != "" {
			if e.Gate.Poll != "" && durationSeconds(e.Gate.Poll) <= 0 {
				add(p+".gate.poll", "DURATION", "invalid duration")
			}
			if e.Gate.BackoffMax != "" && durationSeconds(e.Gate.BackoffMax) <= 0 {
				add(p+".gate.backoff_max", "DURATION", "invalid duration")
			}
		}
	}
	graph := map[string][]string{}
	for i, e := range c.Events {
		for _, d := range e.DependsOn {
			if _, ok := by[d.ID]; !ok {
				add(fmt.Sprintf("events[%d].depends_on", i), "DEPENDENCY_MISSING", "unknown dependency "+d.ID)
			} else {
				graph[e.ID] = append(graph[e.ID], d.ID)
			}
		}
	}
	state := map[string]int{}
	var visit func(string)
	visit = func(n string) {
		if state[n] == 1 {
			add("events", "DEPENDENCY_CYCLE", "dependency cycle involving "+n)
			return
		}
		if state[n] == 2 {
			return
		}
		state[n] = 1
		for _, v := range graph[n] {
			visit(v)
		}
		state[n] = 2
	}
	for n := range graph {
		visit(n)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Path == out[j].Path {
			return out[i].Code < out[j].Code
		}
		return out[i].Path < out[j].Path
	})
	return out
}
func durationSeconds(s string) int64 {
	d, e := time.ParseDuration(s)
	if e != nil {
		return -1
	}
	return int64(d / time.Second)
}
func flatten(v any) []string {
	var r []string
	switch x := v.(type) {
	case string:
		r = append(r, x)
	case []any:
		for _, a := range x {
			r = append(r, flatten(a)...)
		}
	case map[string]any:
		for _, a := range x {
			r = append(r, flatten(a)...)
		}
	}
	return r
}
func CheckAgainstApplied(prevVersion int, prevHash string, nextVersion int, nextHash string) error {
	if nextVersion < prevVersion {
		return fmt.Errorf("CONFIG_VERSION_ROLLBACK")
	}
	if nextVersion == prevVersion && nextHash != prevHash {
		return fmt.Errorf("CONFIG_VERSION_HASH_MISMATCH")
	}
	if nextVersion > prevVersion && nextHash == prevHash {
		return fmt.Errorf("CONFIG_VERSION_HASH_UNCHANGED")
	}
	return nil
}
