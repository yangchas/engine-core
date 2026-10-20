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
	ID          string `json:"id"`
	EventID     string `json:"event_id"`
	Start       string `json:"start"`
	Deadline    string `json:"deadline"`
	Timeout     string `json:"timeout"`
	MaxAttempts int    `json:"max_attempts"`
	Catchup     string `json:"catchup"`
	OnDeadline  string `json:"on_deadline"`
	Calendar    string `json:"calendar"`
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
	b, e := os.ReadFile(file)
	if e != nil {
		return nil, "", e
	}
	var ds []string
	if e = json.Unmarshal(b, &ds); e != nil {
		return nil, "", fmt.Errorf("calendar format: expected date string array: %w", e)
	}
	m := map[string]bool{}
	for _, d := range ds {
		if _, e = time.Parse("2006-01-02", d); e != nil {
			return nil, "", fmt.Errorf("calendar invalid date %q", d)
		}
		m[d] = true
	}
	return m, fmt.Sprintf("%s (%s..%s)", file, ds[0], ds[len(ds)-1]), nil
}
func calendarStatus(raw json.RawMessage, date string, cal map[string]bool, source string) string {
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
				return "UNKNOWN (outside/absent date in calendar; source=" + source + ")"
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
	var ds map[string]bool
	source := "unconfigured"
	if x, ok := c.Calendars["trading"]; ok {
		var e error
		ds, source, e = dateSet(x.File)
		if e != nil {
			return nil, "", e
		}
	}
	var out []Instance
	for _, e := range c.Events {
		status := calendarStatus(e.Calendar, date, ds, source)
		if strings.HasPrefix(status, "EXCLUDED") {
			continue
		}
		if e.Trigger.Type == "window" {
			catch := e.Catchup
			if catch == "" {
				catch = "skip"
			}
			out = append(out, Instance{e.ID, e.ID, e.Trigger.Start, e.Trigger.Deadline, e.Timeout, e.Retry.MaxAttempts, catch, e.OnDeadline, status})
			continue
		}
		if e.Trigger.Type == "at" {
			d := time.Duration(configDuration(e.Trigger.Window))
			end := clock(e.Trigger.At).Add(d)
			out = append(out, Instance{e.ID, e.ID, e.Trigger.At, end.Format("15:04:05"), e.Timeout, e.Retry.MaxAttempts, e.Catchup, e.OnDeadline, status})
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
			out = append(out, Instance{e.ID + "@" + s, e.ID, s, t.Add(win).Format("15:04:05"), e.Timeout, e.Retry.MaxAttempts, catch, e.OnDeadline, status})
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
	xs, src, e := instances(c, *date)
	if e != nil {
		return e
	}
	var rows []any
	for _, x := range xs {
		if *event != "" && x.EventID != *event {
			continue
		}
		rows = append(rows, map[string]any{"instance": x, "decision": "included", "window": x.Start + ".." + x.Deadline, "calendar_source": src})
	}
	if *js {
		return output(rows, true)
	}
	fmt.Printf("date=%s calendar_source=%s\n", *date, src)
	if len(rows) == 0 {
		fmt.Println("no included instances (excluded instances omitted; unknown calendar dates remain UNKNOWN)")
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
	var clamped []string
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
		seconds, changed := clampTimeout(o.Start, o.Deadline, o.Timeout)
		if changed {
			clamped = append(clamped, fmt.Sprintf("%s: %ds -> %ds", o.ID, o.Timeout, seconds))
		}
		ev := config.Event{ID: o.ID, Enabled: true, Calendar: raw(cal), Trigger: config.Trigger{Type: "window", Start: o.Start, Deadline: o.Deadline}, DataDateOffset: o.Offset, AvailableAfter: o.Available, Handler: "legacy_" + o.Handler, Gate: config.Gate{Ref: gate}, Timeout: fmt.Sprintf("%ds", seconds), Retry: config.Retry{MaxAttempts: o.Attempts}, OnDeadline: "missed", Catchup: "skip", DependsOn: o.Depends}
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
	for _, item := range clamped {
		fmt.Fprintf(os.Stderr, "timeout clamped: %s\n", item)
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

func clampTimeout(start, deadline string, timeoutSeconds int) (int, bool) {
	startAt, e1 := time.Parse("15:04:05", start)
	deadlineAt, e2 := time.Parse("15:04:05", deadline)
	if e1 != nil || e2 != nil || timeoutSeconds <= 0 {
		return timeoutSeconds, false
	}
	window := int(deadlineAt.Sub(startAt).Seconds())
	if timeoutSeconds >= window && window > 1 {
		return window - 1, true
	}
	return timeoutSeconds, false
}
func raw(s string) json.RawMessage { b, _ := json.Marshal(s); return b }
