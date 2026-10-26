package main

import (
	"encoding/json"
	"flag"
	"fmt"
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
			return nil, "", e
		}
		source = calendar.source
	}
	var out []Instance
	for _, e := range c.Events {
		if e.Trigger.Type == "startup" {
			continue
		}
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
			out = append(out, Instance{ID: e.ID, EventID: e.ID, Start: e.Trigger.Start, Deadline: e.Trigger.Deadline, LateUntil: e.LateUntil, Timeout: e.Timeout, MaxAttempts: e.Retry.MaxAttempts, Catchup: catch, OnDeadline: e.OnDeadline, Calendar: status})
			continue
		}
		if e.Trigger.Type == "at" {
			d := time.Duration(configDuration(e.Trigger.Window))
			end := clock(e.Trigger.At).Add(d)
			out = append(out, Instance{ID: e.ID, EventID: e.ID, Start: e.Trigger.At, Deadline: end.Format("15:04:05"), LateUntil: e.LateUntil, Timeout: e.Timeout, MaxAttempts: e.Retry.MaxAttempts, Catchup: e.Catchup, OnDeadline: e.OnDeadline, Calendar: status})
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
			out = append(out, Instance{ID: e.ID + "@" + s, EventID: e.ID, Start: s, Deadline: t.Add(win).Format("15:04:05"), LateUntil: e.LateUntil, Timeout: e.Timeout, MaxAttempts: e.Retry.MaxAttempts, Catchup: catch, OnDeadline: e.OnDeadline, Calendar: status})
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
		decision := "included"
		if strings.HasPrefix(x.Calendar, "EXCLUDED") {
			decision = "excluded"
		} else if strings.HasPrefix(x.Calendar, "UNKNOWN") {
			decision = "unknown_included"
		}
		rows = append(rows, map[string]any{"instance": x, "decision": decision, "window": x.Start + ".." + x.Deadline, "calendar_source": src})
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
	if header.SchemaVersion == 2 {
		return migrateScheduleV2(b, *to, *expand, *js)
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

func migrateScheduleV2(b []byte, outputPath string, expand, printJSON bool) error {
	var source struct {
		SchemaVersion int    `json:"schema_version"`
		Revision      int    `json:"revision"`
		Timezone      string `json:"timezone"`
		Global        struct {
			HeavyMaxLoad1 json.RawMessage   `json:"heavy_max_load1"`
			HeavyCutoffs  map[string]string `json:"heavy_cutoffs"`
			Phases        []config.Phase    `json:"phases"`
		} `json:"global"`
		Events []struct {
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
		} `json:"events"`
	}
	if err := json.Unmarshal(b, &source); err != nil {
		return err
	}
	if source.SchemaVersion != 2 || source.Timezone != "Asia/Shanghai" {
		return fmt.Errorf("schedule v2 schema/timezone invalid")
	}
	availableHandlers := []string{"auction_report", "auction_watch", "baostock_probe", "boot_apply_plan",
		"boot_data_health", "boot_reconcile", "boot_trade_day", "calendar_refresh", "close_capture",
		"close_report", "fetch_item", "intraday_data", "kaipan_fetch", "kaipan_hot_capture",
		"night_report", "open_report", "postmarket_data", "premarket_data", "snapshot_check"}
	validHandlers := make(map[string]bool, len(availableHandlers))
	for _, name := range availableHandlers {
		validHandlers[name] = true
	}
	c := config.Config{SchemaVersion: 1, ConfigVersion: 1, Name: "migrated", Timezone: source.Timezone,
		Calendars: map[string]struct {
			File string `json:"file"`
		}{"trading": {File: "/home/exedev/core-data/trade_dates.json"}},
		Handlers: map[string]config.Handler{}, HeavyMaxLoad1: source.Global.HeavyMaxLoad1,
		HeavyCutoffs: source.Global.HeavyCutoffs, Phases: source.Global.Phases}
	for _, event := range source.Events {
		if event.Timeout < 1 || event.MaxAttempts < 1 || event.Handler == "" {
			return fmt.Errorf("invalid event in schedule v2: %s", event.ID)
		}
		if !validHandlers[event.Handler] {
			return fmt.Errorf("unknown handler %q; available=%s", event.Handler, strings.Join(availableHandlers, ","))
		}
		trigger := config.Trigger{Type: event.TriggerType, Start: event.Start, Deadline: event.Deadline, Order: event.Order, After: event.After}
		deps := make([]config.Dependency, 0, len(event.DependsOn))
		for _, id := range event.DependsOn {
			deps = append(deps, config.Dependency{ID: id})
		}
		handler := "legacy_" + event.Handler
		ev := config.Event{ID: event.ID, Enabled: event.Enabled, Calendar: raw(event.Calendar), Trigger: trigger,
			DataDateOffset: event.DataDateOffset, AvailableAfter: event.AvailableAfter, Handler: handler,
			Gate: config.Gate{Ref: event.Gate}, Timeout: fmt.Sprintf("%ds", event.Timeout), LateUntil: event.LateUntil,
			Retry: config.Retry{MaxAttempts: event.MaxAttempts, Backoff: event.RetryBackoff,
				UntilLate: event.RetryUntilLate, SilentUntilLast: event.SilentUntilLast},
			OnDeadline: event.OnDeadline, Catchup: event.Catchup, Cost: event.Cost, Phases: event.Phases,
			OnPhaseMismatch: event.OnPhaseMismatch, OnStart: event.OnStart, DependsOn: deps}
		ev.Args = event.Args
		c.Events = append(c.Events, ev)
		if event.TriggerType == "window" {
			c.Handlers[handler] = config.Handler{SupportsDegrade: true}
		} else {
			c.Handlers[handler] = config.Handler{}
		}
	}
	if issues := config.Validate(c); len(issues) != 0 {
		return fmt.Errorf("schedule v2 invalid: %s %s", issues[0].Path, issues[0].Code)
	}
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
