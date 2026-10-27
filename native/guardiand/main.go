package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"guardiand/config"
)

type Instance struct {
	ID               string `json:"id"`
	EventID          string `json:"event_id"`
	Start            string `json:"start"`
	Deadline         string `json:"deadline"`
	LateUntil        string `json:"late_until,omitempty"`
	Timeout          string `json:"timeout"`
	MaxAttempts      int    `json:"max_attempts"`
	Catchup          string `json:"catchup"`
	OnDeadline       string `json:"on_deadline"`
	Calendar         string `json:"calendar"`
	TriggerType      string `json:"trigger_type,omitempty"`
	DataDate         string `json:"data_date,omitempty"`
	TargetDate       string `json:"target_date,omitempty"`
	DataDateOffset   int    `json:"data_date_offset,omitempty"`
	DegradeRequested bool   `json:"-"`
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	var err error
	switch os.Args[1] {
	case "validate":
		err = validateCmd(os.Args[2:])
	case "list":
		err = listCmd(os.Args[2:])
	case "explain":
		err = explainCmd(os.Args[2:])
	case "migrate":
		err = migrateCmd(os.Args[2:])
	case "run":
		err = runCmd(os.Args[2:])
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func usage() {
	fmt.Fprintln(os.Stderr, "usage: guardiand validate|list|explain|migrate|run ...")
	os.Exit(2)
}
func load(path string) (config.Config, string, error) {
	c, h, is, e := config.Load(path)
	if e != nil {
		return c, "", e
	}
	if len(is) > 0 {
		return c, "", fmt.Errorf("%s: %s", is[0].Path, is[0].Code+": "+is[0].Message)
	}
	return c, h, nil
}
func validateCmd(a []string) error {
	if len(a) != 1 {
		return fmt.Errorf("usage: validate <cfg>")
	}
	b, err := os.ReadFile(a[0])
	if err != nil {
		return err
	}
	var header struct {
		SchemaVersion int `json:"schema_version"`
	}
	if json.Unmarshal(b, &header) == nil && (header.SchemaVersion == 2 || header.SchemaVersion == 3) {
		c, err := compileSchedule(b, header.SchemaVersion)
		if err != nil {
			return err
		}
		fmt.Printf("VALID schedule_schema=%d events=%d\n", header.SchemaVersion, len(c.Events))
		return nil
	}
	c, h, e := load(a[0])
	if e == nil {
		fmt.Printf("VALID config_version=%d config_hash=%s\n", c.ConfigVersion, h)
	}
	return e
}
func dateSet(file string) (map[string]bool, string, error) {
	calendar, e := readTradingDates(file)
	if e != nil {
		return nil, "", e
	}
	return calendar.days, calendar.source, nil
}

type tradingDates struct {
	days   map[string]bool
	min    string
	max    string
	source string
}

func readTradingDates(file string) (tradingDates, error) {
	b, e := os.ReadFile(file)
	if e != nil {
		return tradingDates{}, e
	}
	var ds []string
	if e = json.Unmarshal(b, &ds); e != nil {
		return tradingDates{}, fmt.Errorf("calendar format: expected date string array: %w", e)
	}
	calendar := tradingDates{days: map[string]bool{}}
	for _, d := range ds {
		if _, e = time.Parse("2006-01-02", d); e != nil {
			return tradingDates{}, fmt.Errorf("calendar invalid date %q", d)
		}
		calendar.days[d] = true
		if calendar.min == "" || d < calendar.min {
			calendar.min = d
		}
		if calendar.max == "" || d > calendar.max {
			calendar.max = d
		}
	}
	if calendar.min == "" {
		calendar.source = file + " (empty calendar)"
	} else {
		calendar.source = fmt.Sprintf("%s (%s..%s)", file, calendar.min, calendar.max)
	}
	return calendar, nil
}
func calendarStatus(raw json.RawMessage, date string, cal map[string]bool, source string) string {
	min, max := calendarBounds(cal)
	return calendarStatusInRange(raw, date, cal, source, min, max)
}

func calendarBounds(cal map[string]bool) (string, string) {
	min, max := "", ""
	for date := range cal {
		if min == "" || date < min {
			min = date
		}
		if max == "" || date > max {
			max = date
		}
	}
	return min, max
}

func calendarStatusInRange(raw json.RawMessage, date string, cal map[string]bool, source, min, max string) string {
	if len(raw) == 0 {
		return "INCLUDED (calendar=unspecified)"
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		switch s {
		case "every_day":
			return "INCLUDED (every_day)"
		case "trading_day":
			if cal == nil {
				return "UNKNOWN (trading calendar unavailable)"
			}
			if _, e := time.Parse("2006-01-02", date); e != nil {
				return "UNKNOWN (invalid date)"
			}
			if !cal[date] {
				if min == "" || date < min || date > max {
					return "UNKNOWN (outside calendar coverage; source=" + source + ")"
				}
				return "EXCLUDED (trading_day absent within calendar coverage; source=" + source + ")"
			}
			return "INCLUDED (trading_day; source=" + source + ")"
		}
	}
	var o struct {
		Dates    []string `json:"dates"`
		Weekdays []int    `json:"weekdays"`
	}
	_ = json.Unmarshal(raw, &o)
	if o.Dates != nil {
		for _, d := range o.Dates {
			if d == date {
				return "INCLUDED (explicit date)"
			}
		}
		return "EXCLUDED (explicit date set)"
	}
	if o.Weekdays != nil {
		t, e := time.Parse("2006-01-02", date)
		if e != nil {
			return "UNKNOWN (invalid date)"
		}
		for _, w := range o.Weekdays {
			if int(t.Weekday()) == w {
				return "INCLUDED (weekday rule)"
			}
		}
		return "EXCLUDED (weekday rule)"
	}
	return "UNKNOWN (unsupported calendar rule)"
}
func instances(c config.Config, date string) ([]Instance, string, error) {
	return instancesWithExcluded(c, date, false)
}

func runtimeInstances(c config.Config, date string) ([]Instance, string, error) {
	return instancesWithExcluded(c, date, true)
}

func instancesWithExcluded(c config.Config, date string, includeExcluded bool) ([]Instance, string, error) {
	var calendar tradingDates
	source := "unconfigured"
	if x, ok := c.Calendars["trading"]; ok {
		var e error
		calendar, e = readTradingDates(x.File)
		if e != nil {
			if !includeExcluded {
				return nil, "", e
			}
			calendar = tradingDates{source: "unavailable"}
		}
		source = calendar.source
	}
	var out []Instance
	for _, e := range c.Events {
		if e.Disabled {
			continue
		}
		if e.Trigger.Type == "startup" {
			continue
		}
		targetDate, _ := e.Args["target"].(string)
		status := calendarStatusInRange(e.Calendar, date, calendar.days, source, calendar.min, calendar.max)
		if strings.HasPrefix(status, "EXCLUDED") {
			if !includeExcluded {
				continue
			}
		}
		if e.Trigger.Type == "window" {
			catch := e.Catchup
			if catch == "" {
				catch = "skip"
			}
			if len(e.Trigger.AtTimes) > 0 {
				for _, at := range e.Trigger.AtTimes {
					id := e.ID + "@" + at[:5]
					out = append(out, Instance{ID: id, EventID: e.ID, Start: at, Deadline: e.Trigger.Deadline, LateUntil: e.LateUntil, Timeout: e.Timeout, MaxAttempts: e.Retry.MaxAttempts, Catchup: catch, OnDeadline: e.OnDeadline, Calendar: status, TargetDate: targetDate, DataDateOffset: e.DataDateOffset})
					if len(out) > 10000 {
						return nil, "", fmt.Errorf("at instance limit exceeded")
					}
				}
				continue
			}
			out = append(out, Instance{ID: e.ID, EventID: e.ID, Start: e.Trigger.Start, Deadline: e.Trigger.Deadline, LateUntil: e.LateUntil, Timeout: e.Timeout, MaxAttempts: e.Retry.MaxAttempts, Catchup: catch, OnDeadline: e.OnDeadline, Calendar: status, TargetDate: targetDate, DataDateOffset: e.DataDateOffset})
			continue
		}
		if e.Trigger.Type == "at" {
			d := time.Duration(configDuration(e.Trigger.Window))
			end := clock(e.Trigger.At).Add(d)
			out = append(out, Instance{ID: e.ID, EventID: e.ID, Start: e.Trigger.At, Deadline: end.Format("15:04:05"), LateUntil: e.LateUntil, Timeout: e.Timeout, MaxAttempts: e.Retry.MaxAttempts, Catchup: e.Catchup, OnDeadline: e.OnDeadline, Calendar: status, TargetDate: targetDate, DataDateOffset: e.DataDateOffset})
			continue
		}
		start, end := clock(e.Trigger.Start), clock(e.Trigger.Until)
		step := time.Duration(configDuration(e.Trigger.Every))
		off := time.Duration(configDuration(e.Trigger.Offset))
		win := time.Duration(configDuration(e.Trigger.Window))
		if step <= 0 {
			return nil, "", fmt.Errorf("invalid interval %s", e.ID)
		}
		for t := start.Add(off); !t.After(end); t = t.Add(step) {
			s := t.Format("15:04:05")
			catch := e.Catchup
			if catch == "" {
				catch = "skip"
			}
			out = append(out, Instance{ID: e.ID + "@" + s, EventID: e.ID, Start: s, Deadline: t.Add(win).Format("15:04:05"), LateUntil: e.LateUntil, Timeout: e.Timeout, MaxAttempts: e.Retry.MaxAttempts, Catchup: catch, OnDeadline: e.OnDeadline, Calendar: status, TargetDate: targetDate, DataDateOffset: e.DataDateOffset})
			if len(out) > 10000 {
				return nil, "", fmt.Errorf("interval instance limit exceeded")
			}
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Start == out[j].Start {
			return out[i].ID < out[j].ID
		}
		return out[i].Start < out[j].Start
	})
	return out, source, nil
}
func clock(s string) time.Time { t, _ := time.Parse("15:04:05", s); return t }
func configDuration(s string) int64 {
	d, e := time.ParseDuration(s)
	if e != nil {
		return -1
	}
	return int64(d)
}
func listCmd(a []string) error {
	a = moveFlags(a)
	f := flag.NewFlagSet("list", flag.ContinueOnError)
	date := f.String("date", "2026-10-19", "date")
	js := f.Bool("json", false, "json")
	if e := f.Parse(a); e != nil {
		return e
	}
	if f.NArg() != 1 {
		return fmt.Errorf("usage: list <cfg> --date D")
	}
	c, _, e := load(f.Arg(0))
	if e != nil {
		return e
	}
	xs, _, e := instances(c, *date)
	if e != nil {
		return e
	}
	return output(xs, *js)
}
func explainCmd(a []string) error {
	a = moveFlags(a)
	f := flag.NewFlagSet("explain", flag.ContinueOnError)
	date := f.String("date", "", "date")
	event := f.String("event", "", "event id")
	js := f.Bool("json", false, "json")
	if e := f.Parse(a); e != nil {
		return e
	}
	if f.NArg() != 1 || *date == "" {
		return fmt.Errorf("usage: explain <cfg> --date D [--event ID]")
	}
	c, _, e := load(f.Arg(0))
	if e != nil {
		return e
	}
	xs, src, e := runtimeInstances(c, *date)
	if e != nil {
		return e
	}
	var rows []any
	for _, x := range xs {
		if *event != "" && x.EventID != *event {
			continue
		}
		ev := findEvent(c, x.EventID)
		if ev == nil {
			continue
		}
		dataDate, targetDateErr := resolveDataDate(c, ev, x, *date)
		targetDateError := ""
		if targetDateErr != nil {
			targetDateError = targetDateErr.Error()
		}
		decision := "included"
		if strings.HasPrefix(x.Calendar, "EXCLUDED") {
			decision = "excluded"
		} else if strings.HasPrefix(x.Calendar, "UNKNOWN") {
			decision = "unknown_included"
		}
		triggerPoints := append([]string(nil), ev.Trigger.AtTimes...)
		if len(triggerPoints) == 0 && ev.Trigger.Start != "" {
			triggerPoints = []string{ev.Trigger.Start}
		}
		ladder := make([]map[string]any, 0, len(ev.Ladder))
		for i, rung := range ev.Ladder {
			args, argsSource := rung.Args, rung.Source
			if args == nil {
				args, argsSource = ev.Args, "event"
				if ev.LadderSource == "legacy-field downgrade" {
					argsSource = "legacy-field downgrade"
				}
			}
			budgetEnabled := rung.BudgetEnabled == nil || *rung.BudgetEnabled
			ladder = append(ladder, map[string]any{
				"rung": i + 1, "name": rung.Name, "args": args, "args_source": argsSource,
				"attempts": rung.Attempts, "backoff": rung.Backoff, "backoff_cap": rung.BackoffCap,
				"timeout": rung.Timeout, "until": rung.Until, "degrade_requested": rung.DegradeRequested,
				"budget_enabled": budgetEnabled, "source": rung.Source,
			})
		}
		rows = append(rows, map[string]any{
			"instance": x, "decision": decision, "window": x.Start + ".." + x.Deadline, "calendar_source": src,
			"data_date": dataDate, "target_date_error": targetDateError,
			"handler": ev.Handler, "handler_source": "event", "event_args": ev.Args, "event_args_source": "event",
			"on_deadline":    ev.OnDeadline,
			"trigger_points": triggerPoints, "catchup": ev.Catchup, "catchup_source": ev.CatchupSource,
			"window_parameters": map[string]string{"deadline": ev.Trigger.Deadline, "late_until": ev.LateUntil, "source": ev.WindowSource},
			"ladder":            ladder, "ladder_source": ev.LadderSource, "profile": ev.Profile,
		})
	}
	if *js {
		return output(rows, true)
	}
	fmt.Printf("date=%s calendar_source=%s\n", *date, src)
	if len(rows) == 0 {
		fmt.Println("no instances")
	}
	for _, r := range rows {
		b, _ := json.Marshal(r)
		fmt.Println(string(b))
	}
	return nil
}
func moveFlags(a []string) []string {
	var flags, positional []string
	for i := 0; i < len(a); i++ {
		if strings.HasPrefix(a[i], "--") {
			flags = append(flags, a[i])
			if i+1 < len(a) && !strings.HasPrefix(a[i+1], "--") {
				flags = append(flags, a[i+1])
				i++
			}
		} else {
			positional = append(positional, a[i])
		}
	}
	return append(flags, positional...)
}
func output(v any, j bool) error {
	if j {
		b, e := json.MarshalIndent(v, "", "  ")
		fmt.Println(string(b))
		return e
	}
	switch x := v.(type) {
	case []Instance:
		for _, i := range x {
			fmt.Printf("%s %s..%s timeout=%s attempts=%d catchup=%s on_deadline=%s calendar=%s\n", i.ID, i.Start, i.Deadline, i.Timeout, i.MaxAttempts, i.Catchup, i.OnDeadline, i.Calendar)
		}
	}
	return nil
}
func migrateCmd(a []string) error {
	f := flag.NewFlagSet("migrate", flag.ContinueOnError)
	from := f.String("from", "", "old schedule")
	to := f.String("to", "", "new config")
	expand := f.Bool("expand", false, "expand")
	js := f.Bool("json", false, "json")
	if e := f.Parse(a); e != nil {
		return e
	}
	if *from == "" || *to == "" {
		return fmt.Errorf("usage: migrate --from <schedule.json> --to <cfg.json> [--expand]")
	}
	b, e := os.ReadFile(*from)
	if e != nil {
		return e
	}
	var header struct {
		SchemaVersion int `json:"schema_version"`
	}
	if e = json.Unmarshal(b, &header); e != nil {
		return e
	}
	if header.SchemaVersion == 2 || header.SchemaVersion == 3 {
		c, err := compileSchedule(b, header.SchemaVersion)
		if err != nil {
			return err
		}
		return writeMigratedConfig(c, *to, *expand, *js)
	}
	var old struct {
		Events []struct {
			ID        string              `json:"event_id"`
			Start     string              `json:"start"`
			Deadline  string              `json:"deadline"`
			Gate      string              `json:"gate"`
			Handler   string              `json:"handler"`
			Timeout   int                 `json:"timeout_seconds"`
			Attempts  int                 `json:"max_attempts"`
			Catchup   bool                `json:"catchup"`
			Degrade   bool                `json:"degraded_at_deadline"`
			Offset    int                 `json:"data_date_offset"`
			Available *string             `json:"available_after"`
			Depends   []config.Dependency `json:"depends_on"`
		} `json:"events"`
	}
	if e = json.Unmarshal(b, &old); e != nil {
		return e
	}
	c := config.Config{SchemaVersion: 1, ConfigVersion: 1, Name: "migrated", Timezone: "Asia/Shanghai", Calendars: map[string]struct {
		File string `json:"file"`
	}{"trading": {File: "/home/exedev/core-data/trade_dates.json"}}}
	c.Handlers = map[string]config.Handler{}
	gates := map[string]bool{}
	for _, o := range old.Events {
		cal := "trading_day"
		gate := o.Gate
		if gate == "always" {
			cal = "every_day"
		}
		if gate != "" && gate != "always" && gate != "calendar_ready" {
			// Legacy registered gates retain their names as gate.ref.
			gates[gate] = true
		}
		ev := config.Event{ID: o.ID, Enabled: true, Calendar: raw(cal), Trigger: config.Trigger{Type: "window", Start: o.Start, Deadline: o.Deadline}, DataDateOffset: o.Offset, AvailableAfter: o.Available, Handler: "legacy_" + o.Handler, Gate: config.Gate{Ref: gate}, Timeout: fmt.Sprintf("%ds", o.Timeout), Retry: config.Retry{MaxAttempts: o.Attempts}, OnDeadline: "missed", Catchup: "skip", DependsOn: o.Depends}
		c.Handlers[ev.Handler] = config.Handler{SupportsDegrade: true}
		if o.Catchup {
			ev.Catchup = "run_in_window"
		}
		if o.Degrade {
			ev.OnDeadline = "degrade"
		}
		c.Events = append(c.Events, ev)
	}
	if issues := config.Validate(c); len(issues) != 0 {
		return fmt.Errorf("migrated config invalid: %s %s", issues[0].Path, issues[0].Code)
	}
	if !*expand {
		fmt.Fprintln(os.Stderr, "compression unavailable: preserving all legacy events in expanded form")
	}
	if len(gates) > 0 {
		names := make([]string, 0, len(gates))
		for g := range gates {
			names = append(names, g)
		}
		sort.Strings(names)
		fmt.Fprintf(os.Stderr, "legacy gate mappings: %s -> gate.ref (calendar_ready -> calendar=trading_day; always -> calendar=every_day)\n", strings.Join(names, ", "))
	}
	bb, e := json.MarshalIndent(c, "", "  ")
	if e != nil {
		return e
	}
	if e = os.MkdirAll(filepath.Dir(*to), 0755); e != nil {
		return e
	}
	if e = os.WriteFile(*to, bb, 0644); e != nil {
		return e
	}
	fmt.Printf("migrated events=%d mode=%s output=%s\n", len(c.Events), map[bool]string{true: "expanded", false: "compressed"}[*expand], *to)
	if *js {
		fmt.Println(string(bb))
	}
	return nil
}

type scheduleGlobal struct {
	HeavyMaxLoad1          json.RawMessage   `json:"heavy_max_load1"`
	HeavyMaxLoad5          json.RawMessage   `json:"heavy_max_load5"`
	HeavyLoadDeferMaxPolls *int              `json:"heavy_load_defer_max_polls"`
	HeavyCutoffs           map[string]string `json:"heavy_cutoffs"`
	Phases                 []config.Phase    `json:"phases"`
}

type scheduleV2 struct {
	SchemaVersion int               `json:"schema_version"`
	Revision      int               `json:"revision"`
	Timezone      string            `json:"timezone"`
	Global        scheduleGlobal    `json:"global"`
	Events        []scheduleV2Event `json:"events"`
}

type scheduleV2Event struct {
	ID              string         `json:"event_id"`
	Enabled         bool           `json:"enabled"`
	Calendar        string         `json:"calendar"`
	TriggerType     string         `json:"trigger_type"`
	Start           string         `json:"start"`
	Deadline        string         `json:"deadline"`
	Order           int            `json:"order"`
	After           []string       `json:"after"`
	Gate            string         `json:"gate"`
	Handler         string         `json:"handler"`
	Timeout         int            `json:"timeout_seconds"`
	MaxAttempts     int            `json:"max_attempts"`
	RetryBackoff    string         `json:"retry_backoff"`
	RetryUntilLate  bool           `json:"retry_until_late"`
	SilentUntilLast bool           `json:"silent_until_last"`
	Catchup         string         `json:"catchup_mode"`
	OnDeadline      string         `json:"on_deadline"`
	LateUntil       string         `json:"late_until"`
	Cost            string         `json:"cost"`
	Phases          []string       `json:"phases"`
	OnPhaseMismatch string         `json:"on_phase_mismatch"`
	OnStart         string         `json:"on_start"`
	DataDateOffset  int            `json:"data_date_offset"`
	AvailableAfter  *string        `json:"available_after"`
	DependsOn       []string       `json:"depends_on"`
	Args            map[string]any `json:"args"`
}

type scheduleWindow struct {
	Deadline  string `json:"deadline"`
	LateUntil string `json:"late_until"`
}

type scheduleTier struct {
	Name          string         `json:"name"`
	Args          map[string]any `json:"args"`
	Attempts      int            `json:"attempts"`
	Backoff       string         `json:"backoff"`
	BackoffCap    string         `json:"backoff_cap"`
	Timeout       string         `json:"timeout"`
	Until         string         `json:"until"`
	BudgetEnabled *bool          `json:"budget_enabled"`
}

type scheduleProfile struct {
	Catchup         string          `json:"catchup"`
	RetryBackoff    string          `json:"retry_backoff"`
	RetryUntilLate  bool            `json:"retry_until_late"`
	SilentUntilLast bool            `json:"silent_until_last"`
	Ladder          []scheduleTier  `json:"ladder"`
	Window          *scheduleWindow `json:"window"`
}

type scheduleV3 struct {
	SchemaVersion int                        `json:"schema_version"`
	Revision      int                        `json:"revision"`
	Timezone      string                     `json:"timezone"`
	Global        scheduleGlobal             `json:"global"`
	Profiles      map[string]scheduleProfile `json:"profiles"`
	Events        []scheduleV3Event          `json:"events"`
}

type scheduleV3Event struct {
	ID              string          `json:"id"`
	EventID         string          `json:"event_id"`
	Enabled         bool            `json:"enabled"`
	Calendar        string          `json:"calendar"`
	TriggerType     string          `json:"trigger_type"`
	At              json.RawMessage `json:"at"`
	Deadline        string          `json:"deadline"`
	LateUntil       string          `json:"late_until"`
	Window          *scheduleWindow `json:"window"`
	Profile         string          `json:"profile"`
	Catchup         string          `json:"catchup"`
	Ladder          []scheduleTier  `json:"ladder"`
	Gate            string          `json:"gate"`
	Handler         string          `json:"handler"`
	Timeout         int             `json:"timeout_seconds"`
	MaxAttempts     int             `json:"max_attempts"`
	RetryBackoff    string          `json:"retry_backoff"`
	RetryUntilLate  bool            `json:"retry_until_late"`
	SilentUntilLast bool            `json:"silent_until_last"`
	CatchupMode     string          `json:"catchup_mode"`
	OnDeadline      string          `json:"on_deadline"`
	Order           int             `json:"order"`
	After           []string        `json:"after"`
	Cost            string          `json:"cost"`
	Phases          []string        `json:"phases"`
	OnPhaseMismatch string          `json:"on_phase_mismatch"`
	OnStart         string          `json:"on_start"`
	DataDateOffset  int             `json:"data_date_offset"`
	AvailableAfter  *string         `json:"available_after"`
	DependsOn       []string        `json:"depends_on"`
	Args            map[string]any  `json:"args"`
}

var scheduleHandlers = []string{"auction_report", "auction_watch", "baostock_probe", "boot_apply_plan",
	"boot_data_health", "boot_reconcile", "boot_trade_day", "calendar_refresh", "close_capture",
	"close_report", "fetch_item", "intraday_data", "kaipan_fetch", "kaipan_hot_capture", "selftest",
	"night_report", "open_report", "postmarket_data", "premarket_data", "snapshot_check"}

func decodeStrict(b []byte, target any) error {
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if err := d.Decode(target); err != nil {
		return err
	}
	var trailing any
	if err := d.Decode(&trailing); err != io.EOF {
		return fmt.Errorf("trailing JSON value")
	}
	return nil
}

func compileSchedule(b []byte, version int) (config.Config, error) {
	switch version {
	case 2:
		return compileScheduleV2(b)
	case 3:
		return compileScheduleV3(b)
	default:
		return config.Config{}, fmt.Errorf("unsupported schedule schema_version %d", version)
	}
}

func scheduleConfig(timezone string, global scheduleGlobal) config.Config {
	return config.Config{SchemaVersion: 1, ConfigVersion: 1, Name: "migrated", Timezone: timezone,
		Calendars: map[string]struct {
			File string `json:"file"`
		}{"trading": {File: "/home/exedev/core-data/trade_dates.json"}},
		Handlers: map[string]config.Handler{}, HeavyMaxLoad1: global.HeavyMaxLoad1,
		HeavyMaxLoad5: global.HeavyMaxLoad5, HeavyLoadDeferMaxPolls: global.HeavyLoadDeferMaxPolls,
		HeavyCutoffs: global.HeavyCutoffs, Phases: global.Phases}
}

func validateCompiledSchedule(c config.Config, version int) error {
	if issues := config.Validate(c); len(issues) != 0 {
		i := issues[0]
		return fmt.Errorf("schedule v%d invalid: %s %s: %s", version, i.Path, i.Code, i.Message)
	}
	return nil
}

func scheduleEventHandler(name string) (string, error) {
	for _, valid := range scheduleHandlers {
		if name == valid {
			return "legacy_" + name, nil
		}
	}
	return "", fmt.Errorf("unknown handler %q; available=%s", name, strings.Join(scheduleHandlers, ","))
}

func legacyTier(attempts int, backoff string, untilLate, silent bool, timeout string, args map[string]any) config.LadderTier {
	if attempts < 1 {
		attempts = 1
	}
	cap := "5m"
	if untilLate {
		cap = "10m"
		if backoff == "" {
			backoff = "1m"
		}
	} else if backoff == "" {
		backoff = "1s"
	}
	if !strings.Contains(backoff, "*") {
		backoff += "*2"
	}
	until := ""
	if untilLate {
		until = "late_until"
	}
	budget := false
	return config.LadderTier{Name: "legacy", Args: args, Attempts: attempts, Backoff: backoff,
		BackoffCap: cap, Timeout: timeout, Until: until, SilentUntilLast: silent,
		Source: "legacy-field downgrade", BudgetEnabled: &budget}
}

func appendDeadlineDegrade(ladder []config.LadderTier, timeout, source string) []config.LadderTier {
	if len(ladder) == 0 {
		return ladder
	}
	budget := false
	return append(ladder, config.LadderTier{Name: "degrade", Args: map[string]any{}, Attempts: 1,
		Timeout: timeout, DegradeRequested: true, Source: source, BudgetEnabled: &budget})
}

func compileScheduleV2(b []byte) (config.Config, error) {
	var source scheduleV2
	if err := decodeStrict(b, &source); err != nil {
		return config.Config{}, fmt.Errorf("schedule v2 decode: %w", err)
	}
	if source.SchemaVersion != 2 || source.Timezone != "Asia/Shanghai" {
		return config.Config{}, fmt.Errorf("schedule v2 schema/timezone invalid")
	}
	c := scheduleConfig(source.Timezone, source.Global)
	for _, event := range source.Events {
		if event.Timeout < 1 || event.MaxAttempts < 1 || event.Handler == "" {
			return config.Config{}, fmt.Errorf("invalid event in schedule v2: %s", event.ID)
		}
		handler, err := scheduleEventHandler(event.Handler)
		if err != nil {
			return config.Config{}, err
		}
		deps := make([]config.Dependency, 0, len(event.DependsOn))
		for _, id := range event.DependsOn {
			deps = append(deps, config.Dependency{ID: id})
		}
		timeout := fmt.Sprintf("%ds", event.Timeout)
		ev := config.Event{ID: event.ID, Enabled: event.Enabled, Disabled: !event.Enabled, Calendar: raw(event.Calendar),
			Trigger:        config.Trigger{Type: event.TriggerType, Start: event.Start, Deadline: event.Deadline, Order: event.Order, After: event.After},
			DataDateOffset: event.DataDateOffset, AvailableAfter: event.AvailableAfter, Handler: handler,
			Gate: config.Gate{Ref: event.Gate}, Timeout: timeout, LateUntil: event.LateUntil,
			Retry: config.Retry{MaxAttempts: event.MaxAttempts, Backoff: event.RetryBackoff,
				UntilLate: event.RetryUntilLate, SilentUntilLast: event.SilentUntilLast},
			OnDeadline: event.OnDeadline, Catchup: event.Catchup, Cost: event.Cost, Phases: event.Phases,
			OnPhaseMismatch: event.OnPhaseMismatch, OnStart: event.OnStart, DependsOn: deps, Args: event.Args,
			CatchupSource: "legacy-field downgrade", WindowSource: "legacy-field downgrade",
			LadderSource: "legacy-field downgrade"}
		if ev.Catchup == "" {
			ev.Catchup = "skip"
		}
		ev.Ladder = []config.LadderTier{legacyTier(event.MaxAttempts, event.RetryBackoff,
			event.RetryUntilLate, event.SilentUntilLast, timeout, event.Args)}
		if event.OnDeadline == "degrade" {
			ev.Ladder = appendDeadlineDegrade(ev.Ladder, timeout, "legacy on_deadline downgrade")
		}
		c.Events = append(c.Events, ev)
		if event.TriggerType == "window" {
			c.Handlers[handler] = config.Handler{SupportsDegrade: true}
		} else {
			c.Handlers[handler] = config.Handler{}
		}
	}
	if err := validateCompiledSchedule(c, 2); err != nil {
		return config.Config{}, err
	}
	return c, nil
}

func normalizeScheduleTime(value string) (string, error) {
	if t, err := time.Parse("15:04", value); err == nil {
		return t.Format("15:04:05"), nil
	}
	if t, err := time.Parse("15:04:05", value); err == nil {
		return t.Format("15:04:05"), nil
	}
	return "", fmt.Errorf("invalid time %q: expected HH:MM or HH:MM:SS", value)
}

func validateScheduleProfiles(profiles map[string]scheduleProfile) error {
	keys := make([]string, 0, len(profiles))
	for name := range profiles {
		keys = append(keys, name)
	}
	sort.Strings(keys)
	for _, name := range keys {
		profile := profiles[name]
		if profile.Catchup != "" && profile.Catchup != "skip" && profile.Catchup != "run_in_window" {
			return fmt.Errorf("profile %q has invalid catchup %q", name, profile.Catchup)
		}
		if profile.Window != nil {
			var deadline, lateUntil string
			if profile.Window.Deadline != "" {
				var err error
				deadline, err = normalizeScheduleTime(profile.Window.Deadline)
				if err != nil {
					return fmt.Errorf("profile %q window: %w", name, err)
				}
			}
			if profile.Window.LateUntil != "" {
				var err error
				lateUntil, err = normalizeScheduleTime(profile.Window.LateUntil)
				if err != nil {
					return fmt.Errorf("profile %q window: %w", name, err)
				}
			}
			if deadline != "" && lateUntil != "" && lateUntil < deadline {
				return fmt.Errorf("profile %q window.late_until must not precede deadline", name)
			}
		}
		if profile.Ladder == nil {
			continue
		}
		if len(profile.Ladder) == 0 {
			return fmt.Errorf("profile %q has empty ladder", name)
		}
		ladder := make([]config.LadderTier, 0, len(profile.Ladder))
		for _, rung := range profile.Ladder {
			ladder = append(ladder, compileScheduleTier(rung, "profile:"+name, ""))
		}
		validation := config.Config{
			SchemaVersion: 1, ConfigVersion: 1, Name: "profile-validation", Timezone: "Asia/Shanghai",
			Handlers: map[string]config.Handler{"profile-validation": {SupportsDegrade: true}},
			Events: []config.Event{{ID: "profile-validation", Handler: "profile-validation",
				Trigger: config.Trigger{Type: "startup"}, LateUntil: "23:59:59", Ladder: ladder}},
		}
		if issues := config.Validate(validation); len(issues) > 0 {
			issue := issues[0]
			return fmt.Errorf("profile %q %s: %s", name, issue.Code, issue.Message)
		}
	}
	return nil
}

func scheduleTimes(rawAt json.RawMessage, triggerType string) ([]string, bool, error) {
	if len(rawAt) == 0 || string(rawAt) == "null" {
		if triggerType == "startup" || triggerType == "" {
			return nil, false, nil
		}
		return nil, false, fmt.Errorf("at is required for trigger_type %q", triggerType)
	}
	var scalar string
	if err := json.Unmarshal(rawAt, &scalar); err == nil {
		at, err := normalizeScheduleTime(scalar)
		return []string{at}, false, err
	}
	var values []string
	if err := json.Unmarshal(rawAt, &values); err != nil {
		return nil, false, fmt.Errorf("at must be a time string or list of strings")
	}
	if len(values) == 0 {
		return nil, true, fmt.Errorf("at list must not be empty")
	}
	seen := map[string]bool{}
	for i, value := range values {
		at, err := normalizeScheduleTime(value)
		if err != nil {
			return nil, true, fmt.Errorf("at[%d]: %w", i, err)
		}
		if seen[at] {
			return nil, true, fmt.Errorf("duplicate at time %q", value)
		}
		seen[at] = true
		values[i] = at
	}
	return values, true, nil
}

func compileScheduleTier(t scheduleTier, source string, fallbackTimeout string) config.LadderTier {
	timeout := t.Timeout
	if timeout == "" {
		timeout = fallbackTimeout
	}
	return config.LadderTier{Name: t.Name, Args: t.Args, Attempts: t.Attempts, Backoff: t.Backoff,
		BackoffCap: t.BackoffCap, Timeout: timeout, Until: t.Until, BudgetEnabled: t.BudgetEnabled, Source: source}
}

func compileScheduleV3(b []byte) (config.Config, error) {
	var source scheduleV3
	if err := decodeStrict(b, &source); err != nil {
		return config.Config{}, fmt.Errorf("schedule v3 decode: %w", err)
	}
	if source.SchemaVersion != 3 || source.Timezone != "Asia/Shanghai" {
		return config.Config{}, fmt.Errorf("schedule v3 schema/timezone invalid")
	}
	if err := validateScheduleProfiles(source.Profiles); err != nil {
		return config.Config{}, err
	}
	c := scheduleConfig(source.Timezone, source.Global)
	for _, event := range source.Events {
		eventID := event.ID
		if eventID != "" && event.EventID != "" {
			return config.Config{}, fmt.Errorf("event must use only one of id or event_id")
		}
		if eventID == "" {
			eventID = event.EventID
		}
		handler, err := scheduleEventHandler(event.Handler)
		if err != nil {
			return config.Config{}, err
		}
		profile, hasProfile := source.Profiles[event.Profile]
		if event.Profile != "" && !hasProfile {
			return config.Config{}, fmt.Errorf("event %s references unknown profile %q", eventID, event.Profile)
		}
		triggerType := event.TriggerType
		if triggerType == "" {
			if len(event.At) > 0 && string(event.At) != "null" {
				triggerType = "window"
			} else {
				triggerType = "startup"
			}
		}
		if triggerType != "window" && triggerType != "startup" {
			return config.Config{}, fmt.Errorf("event %s has unsupported trigger_type %q", eventID, triggerType)
		}
		at, atList, err := scheduleTimes(event.At, triggerType)
		if err != nil {
			return config.Config{}, fmt.Errorf("event %s: %w", eventID, err)
		}
		window := scheduleWindow{}
		windowSource := "default"
		if hasProfile && profile.Window != nil {
			window = *profile.Window
			windowSource = "profile:" + event.Profile
		}
		if event.Window != nil {
			if event.Window.Deadline != "" {
				window.Deadline = event.Window.Deadline
			}
			if event.Window.LateUntil != "" {
				window.LateUntil = event.Window.LateUntil
			}
			windowSource = "event"
		}
		if event.Deadline != "" {
			window.Deadline = event.Deadline
			windowSource = "event"
		}
		if event.LateUntil != "" {
			window.LateUntil = event.LateUntil
			windowSource = "event"
		}
		if triggerType == "window" && window.Deadline == "" {
			return config.Config{}, fmt.Errorf("event %s requires window.deadline", eventID)
		}
		for _, v := range []*string{&window.Deadline, &window.LateUntil} {
			if *v == "" {
				continue
			}
			normalized, err := normalizeScheduleTime(*v)
			if err != nil {
				return config.Config{}, fmt.Errorf("event %s window: %w", eventID, err)
			}
			*v = normalized
		}
		catchup, catchupSource := "", "default"
		if hasProfile && profile.Catchup != "" {
			catchup, catchupSource = profile.Catchup, "profile:"+event.Profile
		}
		if event.CatchupMode != "" {
			catchup, catchupSource = event.CatchupMode, "legacy-field downgrade"
		}
		if event.Catchup != "" {
			catchup, catchupSource = event.Catchup, "event"
		}
		if catchup == "" {
			catchup = "skip"
		}
		var sourceLadder []scheduleTier
		hasLadder := false
		ladderSource := "legacy-field downgrade"
		if hasProfile && profile.Ladder != nil {
			sourceLadder, ladderSource = profile.Ladder, "profile:"+event.Profile
			hasLadder = true
		}
		if event.Ladder != nil {
			sourceLadder, ladderSource = event.Ladder, "event"
			hasLadder = true
		}
		var timeout string
		if event.Timeout > 0 {
			timeout = fmt.Sprintf("%ds", event.Timeout)
		}
		ladder := make([]config.LadderTier, 0)
		if hasLadder {
			if len(sourceLadder) == 0 {
				return config.Config{}, fmt.Errorf("event %s has empty ladder", eventID)
			}
			for _, rung := range sourceLadder {
				compiled := compileScheduleTier(rung, ladderSource, "")
				if compiled.Args == nil {
					compiled.Args = event.Args
				}
				ladder = append(ladder, compiled)
			}
		} else {
			if event.Timeout < 1 {
				return config.Config{}, fmt.Errorf("event %s legacy fields require timeout_seconds", eventID)
			}
			ladder = append(ladder, legacyTier(event.MaxAttempts, event.RetryBackoff, event.RetryUntilLate,
				event.SilentUntilLast, timeout, event.Args))
		}
		if event.OnDeadline == "degrade" {
			degradeTimeout := timeout
			if degradeTimeout == "" {
				degradeTimeout = ladder[0].Timeout
			}
			ladder = appendDeadlineDegrade(ladder, degradeTimeout, "legacy on_deadline downgrade")
		}
		start := ""
		if len(at) > 0 {
			start = at[0]
		}
		trigger := config.Trigger{Type: triggerType, Start: start, Deadline: window.Deadline, Order: event.Order, After: event.After}
		if atList {
			trigger.AtTimes = at
		}
		deps := make([]config.Dependency, 0, len(event.DependsOn))
		for _, id := range event.DependsOn {
			deps = append(deps, config.Dependency{ID: id})
		}
		retryBackoff := event.RetryBackoff
		if retryBackoff == "" {
			retryBackoff = profile.RetryBackoff
		}
		retryUntilLate := event.RetryUntilLate || profile.RetryUntilLate
		silentUntilLast := event.SilentUntilLast || profile.SilentUntilLast
		ev := config.Event{ID: eventID, Enabled: event.Enabled, Disabled: !event.Enabled, Calendar: raw(event.Calendar), Trigger: trigger,
			DataDateOffset: event.DataDateOffset, AvailableAfter: event.AvailableAfter, Handler: handler,
			Gate: config.Gate{Ref: event.Gate}, Timeout: ladder[0].Timeout, LateUntil: window.LateUntil,
			Retry: config.Retry{MaxAttempts: ladder[0].Attempts, Backoff: retryBackoff,
				UntilLate: retryUntilLate, SilentUntilLast: silentUntilLast},
			Profile: event.Profile, Ladder: ladder, LadderSource: ladderSource, CatchupSource: catchupSource,
			WindowSource: windowSource, OnDeadline: event.OnDeadline, Catchup: catchup, Cost: event.Cost, Phases: event.Phases,
			OnPhaseMismatch: event.OnPhaseMismatch, OnStart: event.OnStart, DependsOn: deps, Args: event.Args}
		if triggerType == "window" {
			c.Handlers[handler] = config.Handler{SupportsDegrade: true}
		} else {
			c.Handlers[handler] = config.Handler{}
		}
		c.Events = append(c.Events, ev)
	}
	if err := validateCompiledSchedule(c, 3); err != nil {
		return config.Config{}, err
	}
	return c, nil
}

func writeMigratedConfig(c config.Config, outputPath string, expand, printJSON bool) error {
	bb, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(outputPath), 0755); err != nil {
		return err
	}
	if err = os.WriteFile(outputPath, bb, 0644); err != nil {
		return err
	}
	fmt.Printf("migrated events=%d mode=%s output=%s\n", len(c.Events), map[bool]string{true: "expanded", false: "compressed"}[expand], outputPath)
	if printJSON {
		fmt.Println(string(bb))
	}
	return nil
}

func raw(s string) json.RawMessage { b, _ := json.Marshal(s); return b }
