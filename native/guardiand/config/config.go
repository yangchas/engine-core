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
	Concurrency   map[string]json.RawMessage `json:"concurrency,omitempty"`
	HeavyMaxLoad1 json.RawMessage            `json:"heavy_max_load1,omitempty"`
	Handlers      map[string]Handler         `json:"handlers,omitempty"`
	Alerts        map[string]json.RawMessage `json:"alerts,omitempty"`
	Events        []Event                    `json:"events"`
	Phases        []Phase                    `json:"phases,omitempty"`
	Datasets      json.RawMessage            `json:"datasets,omitempty"`
}
type Phase struct {
	Name  string `json:"name"`
	Start string `json:"start"`
	End   string `json:"end"`
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
	Type     string   `json:"type"`
	Start    string   `json:"start,omitempty"`
	Deadline string   `json:"deadline,omitempty"`
	Until    string   `json:"until,omitempty"`
	Every    string   `json:"every,omitempty"`
	Offset   string   `json:"offset,omitempty"`
	Window   string   `json:"window,omitempty"`
	At       string   `json:"time,omitempty"`
	Order    int      `json:"order,omitempty"`
	After    []string `json:"after,omitempty"`
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
	Backoff          string   `json:"backoff,omitempty"`
	UntilLate        bool     `json:"until_late,omitempty"`
	SilentUntilLast  bool     `json:"silent_until_last,omitempty"`
	Jitter           float64  `json:"jitter,omitempty"`
	RetryOn          []string `json:"retry_on,omitempty"`
	NoRetryOnTimeout bool     `json:"no_retry_on_timeout,omitempty"`
}
type Dependency struct {
	ID             string `json:"id"`
	AcceptDegraded bool   `json:"accept_degraded,omitempty"`
}
type Event struct {
	ID              string          `json:"id"`
	Enabled         bool            `json:"enabled,omitempty"`
	Description     string          `json:"description,omitempty"`
	Calendar        json.RawMessage `json:"calendar,omitempty"`
	Trigger         Trigger         `json:"trigger"`
	DataDateOffset  int             `json:"data_date_offset,omitempty"`
	AvailableAfter  *string         `json:"available_after,omitempty"`
	Handler         string          `json:"handler,omitempty"`
	Args            map[string]any  `json:"args,omitempty"`
	Gate            Gate            `json:"gate,omitempty"`
	Timeout         string          `json:"timeout,omitempty"`
	LateUntil       string          `json:"late_until,omitempty"`
	Retry           Retry           `json:"retry,omitempty"`
	OnDeadline      string          `json:"on_deadline,omitempty"`
	Catchup         string          `json:"catchup,omitempty"`
	Class           string          `json:"class,omitempty"`
	Priority        int             `json:"priority,omitempty"`
	Alert           json.RawMessage `json:"alert,omitempty"`
	DependsOn       []Dependency    `json:"depends_on,omitempty"`
	Phases          []string        `json:"phases,omitempty"`
	OnStart         string          `json:"on_start,omitempty"`
	OnPhaseMismatch string          `json:"on_phase_mismatch,omitempty"`
	Cost            string          `json:"cost,omitempty"`
}

func DefaultPhases() []Phase {
	return []Phase{{"NIGHT", "00:00", "08:00"}, {"PRE_MARKET", "08:00", "09:15"}, {"AUCTION", "09:15", "09:25"}, {"AUCTION_END", "09:25", "09:30"}, {"OPEN", "09:30", "09:45"}, {"INTRADAY_AM", "09:45", "11:30"}, {"MIDDAY_BREAK", "11:30", "13:00"}, {"PM_OPEN", "13:00", "13:15"}, {"INTRADAY_PM", "13:15", "14:57"}, {"CLOSE_AUCTION", "14:57", "15:00"}, {"CLOSE", "15:00", "15:30"}, {"POST_MARKET", "15:30", "24:00"}}
}
func (c Config) EffectivePhases() []Phase {
	if c.Phases == nil {
		return DefaultPhases()
	}
	return c.Phases
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
	phases := c.EffectivePhases()
	phaseNames := map[string]bool{}
	phaseTime := func(v string, end bool) int {
		if end && v == "24:00" {
			return 86400
		}
		t, e := time.Parse("15:04", v)
		if e != nil {
			return -1
		}
		return t.Hour()*3600 + t.Minute()*60
	}
	last := 0
	for i, p := range phases {
		a, b := phaseTime(p.Start, false), phaseTime(p.End, true)
		path := fmt.Sprintf("phases[%d]", i)
		if p.Name == "" || phaseNames[p.Name] {
			add(path+".name", "PHASE_NAME", "phase names must be nonempty and unique")
		}
		phaseNames[p.Name] = true
		if a < 0 {
			add(path+".start", "PHASE_TIME", "expected HH:MM")
		}
		if b < 0 {
			add(path+".end", "PHASE_TIME", "expected HH:MM or 24:00")
		}
		if a >= 0 && b >= 0 {
			if a >= b {
				add(path, "PHASE_ORDER", "start must be before end")
			}
			if i == 0 && a != 0 {
				add(path+".start", "PHASE_COVERAGE", "phases must start at 00:00")
			}
			if i > 0 && a != last {
				add(path+".start", "PHASE_COVERAGE", "phases must be contiguous and non-overlapping")
			}
			last = b
		}
	}
	if len(phases) > 0 && last != 86400 {
		add("phases", "PHASE_COVERAGE", "phases must end at 24:00")
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
		case "startup":
			// Startup events are dispatched once per process boot.
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
		if e.OnStart != "" && e.OnStart != "reconcile" {
			add(p+".on_start", "ENUM", "invalid on_start")
		}
		if e.OnPhaseMismatch != "" && e.OnPhaseMismatch != "defer" && e.OnPhaseMismatch != "skip" {
			add(p+".on_phase_mismatch", "ENUM", "expected defer or skip")
		}
		if e.Cost != "" && e.Cost != "light" && e.Cost != "medium" && e.Cost != "heavy" {
			add(p+".cost", "ENUM", "expected light, medium, or heavy")
		}
		for _, name := range e.Phases {
			if name != "any" && !phaseNames[name] {
				add(p+".phases", "PHASE_UNKNOWN", "unknown phase "+name)
			}
		}
		if start >= 0 && end >= 0 && start > end {
			add(p+".trigger.deadline", "WINDOW_ORDER", "start must not exceed deadline")
		}
		if e.Timeout != "" {
			to := durationSeconds(e.Timeout)
			if to <= 0 {
				add(p+".timeout", "DURATION", "invalid positive duration")
			} else if to > int64(24*time.Hour) {
				add(p+".timeout", "DURATION", "timeout must not exceed 24h")
			}
		}
		if e.Retry.Backoff != "" {
			backoff, err := time.ParseDuration(e.Retry.Backoff)
			if err != nil || backoff <= 0 || backoff > 10*time.Minute {
				add(p+".retry.backoff", "DURATION", "backoff must be positive and at most 10m")
			}
		}
		if e.Retry.MaxAttempts < 0 {
			add(p+".retry.max_attempts", "RETRY_ATTEMPTS", "must be zero (default) or positive")
		}
		if e.Retry.Base != "" {
			base, err := time.ParseDuration(e.Retry.Base)
			if err != nil || base <= 0 || base > 5*time.Minute {
				add(p+".retry.base", "DURATION", "base must be positive and at most 5m")
			}
		}
		if e.Retry.UntilLate && e.LateUntil == "" {
			add(p+".retry.until_late", "LATE_UNTIL_REQUIRED", "until_late requires late_until")
		}
		if e.LateUntil != "" {
			late := checkTime(p+".late_until", e.LateUntil)
			if end >= 0 && late >= 0 && late < end {
				add(p+".late_until", "LATE_UNTIL_ORDER", "late_until must not precede deadline")
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
	if len(c.HeavyMaxLoad1) > 0 {
		var value any
		if err := json.Unmarshal(c.HeavyMaxLoad1, &value); err != nil {
			add("heavy_max_load1", "LOAD_THRESHOLD", "expected nonnegative number or cpus")
		} else {
			switch v := value.(type) {
			case float64:
				if v < 0 {
					add("heavy_max_load1", "LOAD_THRESHOLD", "must be nonnegative")
				}
			case string:
				if v != "cpus" {
					add("heavy_max_load1", "LOAD_THRESHOLD", "expected nonnegative number or cpus")
				}
			default:
				add("heavy_max_load1", "LOAD_THRESHOLD", "expected nonnegative number or cpus")
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
		for _, dep := range e.Trigger.After {
			if _, ok := by[dep]; !ok {
				add(fmt.Sprintf("events[%d].trigger.after", i), "DEPENDENCY_MISSING", "unknown dependency "+dep)
			} else {
				graph[e.ID] = append(graph[e.ID], dep)
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
