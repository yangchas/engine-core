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
	"strconv"
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
	Concurrency            map[string]json.RawMessage `json:"concurrency,omitempty"`
	HeavyMaxLoad1          json.RawMessage            `json:"heavy_max_load1,omitempty"`
	HeavyMaxLoad5          json.RawMessage            `json:"heavy_max_load5,omitempty"`
	HeavyLoadDeferMaxPolls *int                       `json:"heavy_load_defer_max_polls,omitempty"`
	HeavyCutoffs           map[string]string          `json:"heavy_cutoffs,omitempty"`
	Handlers               map[string]Handler         `json:"handlers,omitempty"`
	Alerts                 map[string]json.RawMessage `json:"alerts,omitempty"`
	Events                 []Event                    `json:"events"`
	Phases                 []Phase                    `json:"phases,omitempty"`
	Datasets               json.RawMessage            `json:"datasets,omitempty"`
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
	AtTimes  []string `json:"at_times,omitempty"`
	Order    int      `json:"order,omitempty"`
	After    []string `json:"after,omitempty"`
}
type Gate struct {
	Ref        string         `json:"ref,omitempty"`
	Params     map[string]any `json:"params,omitempty"`
	Poll       string         `json:"poll,omitempty"`
	BackoffMax string         `json:"backoff_max,omitempty"`
	Timeout    string         `json:"timeout,omitempty"`
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
type LadderTier struct {
	Name             string         `json:"name"`
	Args             map[string]any `json:"args,omitempty"`
	Attempts         int            `json:"attempts"`
	Backoff          string         `json:"backoff,omitempty"`
	BackoffCap       string         `json:"backoff_cap,omitempty"`
	Timeout          string         `json:"timeout"`
	Until            string         `json:"until,omitempty"`
	DegradeRequested bool           `json:"degrade_requested,omitempty"`
	SilentUntilLast  bool           `json:"silent_until_last,omitempty"`
	Source           string         `json:"source,omitempty"`
	BudgetEnabled    *bool          `json:"budget_enabled,omitempty"`
}
type Event struct {
	ID              string          `json:"id"`
	Enabled         bool            `json:"enabled,omitempty"`
	Disabled        bool            `json:"disabled,omitempty"`
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
	Profile         string          `json:"profile,omitempty"`
	Ladder          []LadderTier    `json:"ladder,omitempty"`
	LadderSource    string          `json:"ladder_source,omitempty"`
	CatchupSource   string          `json:"catchup_source,omitempty"`
	WindowSource    string          `json:"window_source,omitempty"`
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
var backoffMultiplierRE = regexp.MustCompile(`^(.+)\*([0-9]+(?:\.[0-9]+)?)$`)

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
		seenAt := map[string]bool{}
		for j, at := range e.Trigger.AtTimes {
			if checkTime(fmt.Sprintf("%s.trigger.at_times[%d]", p, j), at) < 0 {
				continue
			}
			instanceMinute := at[:5]
			if seenAt[instanceMinute] {
				add(p+".trigger.at_times", "TRIGGER_DUPLICATE", "trigger times must map to unique HH:MM instances")
			}
			seenAt[instanceMinute] = true
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
		if e.Ladder != nil && len(e.Ladder) == 0 {
			add(p+".ladder", "LADDER_EMPTY", "ladder must contain at least one rung")
		}
		if len(e.Ladder) > 0 {
			names := map[string]bool{}
			for j, rung := range e.Ladder {
				rk := fmt.Sprintf("%s.ladder[%d]", p, j)
				if !idRE.MatchString(rung.Name) {
					add(rk+".name", "LADDER_NAME", "name must be a valid identifier")
				} else if names[rung.Name] {
					add(rk+".name", "LADDER_NAME", "rung names must be unique")
				}
				names[rung.Name] = true
				if rung.Attempts < 1 {
					add(rk+".attempts", "LADDER_ATTEMPTS", "must be positive")
				}
				if rung.Timeout == "" {
					add(rk+".timeout", "LADDER_TIMEOUT", "required")
				} else if timeout, err := time.ParseDuration(rung.Timeout); err != nil || timeout <= 0 || timeout > 24*time.Hour {
					add(rk+".timeout", "DURATION", "timeout must be positive and at most 24h")
				}
				if rung.Attempts > 1 && rung.Backoff == "" {
					add(rk+".backoff", "LADDER_BACKOFF", "required when attempts exceeds one")
				} else if rung.Backoff != "" {
					if _, _, err := parseBackoff(rung.Backoff); err != nil {
						add(rk+".backoff", "LADDER_BACKOFF", err.Error())
					}
				}
				if rung.BackoffCap != "" {
					if cap, err := time.ParseDuration(rung.BackoffCap); err != nil || cap <= 0 || cap > 24*time.Hour {
						add(rk+".backoff_cap", "LADDER_BACKOFF", "cap must be positive and at most 24h")
					}
				}
				if rung.Until != "" && rung.Until != "late_until" {
					if _, err := time.Parse("15:04:05", rung.Until); err != nil {
						add(rk+".until", "LADDER_UNTIL", "expected late_until or HH:MM:SS")
					}
				}
				if rung.Until == "late_until" && e.LateUntil == "" {
					add(rk+".until", "LATE_UNTIL_REQUIRED", "late_until requires event late_until")
				}
				if rung.DegradeRequested && !c.Handlers[e.Handler].SupportsDegrade {
					add(rk+".degrade_requested", "DEGRADE_UNSUPPORTED", "handler must support degrade")
				}
			}
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
	validateLoadThreshold := func(name string, raw json.RawMessage) {
		if len(raw) == 0 {
			return
		}
		var value any
		if err := json.Unmarshal(raw, &value); err != nil {
			add(name, "LOAD_THRESHOLD", "expected nonnegative number or cpus")
		} else {
			switch v := value.(type) {
			case float64:
				if v < 0 {
					add(name, "LOAD_THRESHOLD", "must be nonnegative")
				}
			case string:
				if v != "cpus" {
					add(name, "LOAD_THRESHOLD", "expected nonnegative number or cpus")
				}
			default:
				add(name, "LOAD_THRESHOLD", "expected nonnegative number or cpus")
			}
		}
	}
	validateLoadThreshold("heavy_max_load1", c.HeavyMaxLoad1)
	validateLoadThreshold("heavy_max_load5", c.HeavyMaxLoad5)
	if c.HeavyLoadDeferMaxPolls != nil && *c.HeavyLoadDeferMaxPolls < 1 {
		add("heavy_load_defer_max_polls", "LOAD_DEFER_POLLS", "must be positive")
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

func parseBackoff(value string) (time.Duration, float64, error) {
	m := backoffMultiplierRE.FindStringSubmatch(value)
	if len(m) != 3 {
		return 0, 0, fmt.Errorf("expected base*multiplier, for example 2s*2")
	}
	base, err := time.ParseDuration(m[1])
	if err != nil || base <= 0 || base > 24*time.Hour {
		return 0, 0, fmt.Errorf("base must be positive and at most 24h")
	}
	factor, err := strconv.ParseFloat(m[2], 64)
	if err != nil || factor < 1 || factor > 1000 {
		return 0, 0, fmt.Errorf("multiplier must be between 1 and 1000")
	}
	return base, factor, nil
}

func BackoffDelay(value string, attempt int, capValue string) time.Duration {
	base, factor, err := parseBackoff(value)
	if err != nil {
		return 0
	}
	cap := 24 * time.Hour
	if parsed, parseErr := time.ParseDuration(capValue); parseErr == nil && parsed > 0 {
		cap = parsed
	}
	delay := float64(base)
	for i := 1; i < attempt; i++ {
		delay *= factor
		if delay >= float64(cap) {
			return cap
		}
	}
	if delay > float64(cap) {
		return cap
	}
	return time.Duration(delay)
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
