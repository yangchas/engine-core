package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"hash/crc32"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"guardiand/config"
)

var runtimeTemplateRE = regexp.MustCompile(`\{([^{}]+)\}`)

type ledgerRecord struct {
	Seq   uint64 `json:"seq"`
	TS    string `json:"ts"`
	Type  string `json:"type"`
	CRC32 uint32 `json:"crc32"`
	Data  any    `json:"data,omitempty"`
}
type gateOutput struct {
	Status       string `json:"status"`
	Reason       string `json:"reason"`
	RowCount     *int   `json:"row_count"`
	ElapsedMS    *int64 `json:"elapsed_ms"`
	QueryWindow  any    `json:"query_window"`
	CheckpointTS string `json:"checkpoint_ts"`
	Stdout       string `json:"stdout,omitempty"`
	Stderr       string `json:"stderr,omitempty"`
	Invalid      bool   `json:"-"`
}

func parseGateOutput(b []byte) gateOutput {
	var out gateOutput
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(lines) == 0 || strings.TrimSpace(string(b)) == "" || json.Unmarshal([]byte(lines[len(lines)-1]), &out) != nil {
		out.Invalid = true
		out.Status = "unknown"
	}
	return out
}
func truncateText(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}
func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		return ee.ExitCode()
	}
	return -1
}
func parseGateTimestamp(s string, loc *time.Location) (time.Time, error) {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05.999999", "2006-01-02 15:04:05"} {
		if t, err := time.ParseInLocation(layout, s, loc); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("invalid checkpoint timestamp")
}

type ledger struct {
	mu   sync.Mutex
	f    *os.File
	seq  uint64
	now  func() time.Time
	date string
}

func (l *ledger) sequence() uint64 { l.mu.Lock(); defer l.mu.Unlock(); return l.seq }

func recordCRC(r ledgerRecord) uint32 {
	r.CRC32 = 0
	if b, err := json.Marshal(r.Data); err == nil {
		var canonical any
		if json.Unmarshal(b, &canonical) == nil {
			r.Data = canonical
		}
	}
	b, _ := json.Marshal(r)
	return crc32.ChecksumIEEE(b)
}

func openLedger(root, date string) (*ledger, error) {
	dir := filepath.Join(root, "ledger")
	if err := os.MkdirAll(dir, 0750); err != nil {
		return nil, err
	}
	p := filepath.Join(dir, date+".jsonl")
	f, err := os.OpenFile(p, os.O_CREATE|os.O_RDWR, 0640)
	if err != nil {
		return nil, err
	}
	l := &ledger{f: f, now: time.Now, date: date}
	reader := bufio.NewReader(f)
	var good int64
	truncated := false
	backup := ""
	for {
		line, readErr := reader.ReadBytes('\n')
		if len(line) > 0 {
			var rec ledgerRecord
			valid := json.Unmarshal(bytesTrimNL(line), &rec) == nil && rec.Seq > 0 && rec.CRC32 == recordCRC(rec)
			if !valid || readErr != nil {
				truncated = true
				break
			}
			if rec.Seq > l.seq {
				l.seq = rec.Seq
			}
			good += int64(len(line))
		}
		if readErr != nil {
			break
		}
	}
	if truncated {
		backup = p + ".corrupt-" + time.Now().UTC().Format("20060102T150405.000000000Z")
		original, readErr := os.ReadFile(p)
		if readErr != nil {
			f.Close()
			return nil, readErr
		}
		bf, createErr := os.OpenFile(backup, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0640)
		if createErr != nil {
			f.Close()
			return nil, createErr
		}
		_, copyErr := bf.Write(original)
		if copyErr == nil {
			copyErr = bf.Sync()
		}
		closeErr := bf.Close()
		if copyErr != nil || closeErr != nil {
			f.Close()
			if copyErr != nil {
				return nil, copyErr
			}
			return nil, closeErr
		}
		if err = f.Truncate(good); err != nil {
			f.Close()
			return nil, err
		}
	}
	if _, err = f.Seek(0, io.SeekEnd); err != nil {
		f.Close()
		return nil, err
	}
	if truncated {
		if err = l.append("TAIL_TRUNCATED", map[string]any{"valid_bytes": good, "backup_path": backup}); err != nil {
			f.Close()
			return nil, err
		}
	}
	return l, nil
}
func bytesTrimNL(b []byte) []byte { return []byte(strings.TrimSuffix(string(b), "\n")) }
func (l *ledger) append(typ string, data any) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.seq++
	now := time.Now
	if l.now != nil {
		now = l.now
	}
	r := ledgerRecord{Seq: l.seq, TS: now().Format(time.RFC3339Nano), Type: typ, Data: data}
	r.CRC32 = recordCRC(r)
	b, err := json.Marshal(r)
	if err != nil {
		l.seq--
		return err
	}
	b = append(b, '\n')
	if _, err = l.f.Write(b); err != nil {
		return err
	}
	return l.f.Sync()
}
func (l *ledger) close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.f.Close()
}

type instanceState struct {
	Status        string   `json:"status"`
	Attempts      int      `json:"attempts"`
	Rung          int      `json:"rung,omitempty"`
	Tier          string   `json:"tier,omitempty"`
	RungAttempts  int      `json:"rung_attempts,omitempty"`
	SkippedTiers  []string `json:"skipped_tiers,omitempty"`
	StartedAt     string   `json:"started_at"`
	CompletedAt   string   `json:"completed_at"`
	LastError     string   `json:"last_error"`
	NextAttemptAt string   `json:"next_attempt_at,omitempty"`
	Late          bool     `json:"late,omitempty"`
	NextPhase     string   `json:"next_phase,omitempty"`
	DataDate      string   `json:"data_date,omitempty"`
	WaitFor       []string `json:"wait_for,omitempty"`
	Cost          string   `json:"cost,omitempty"`
}
type runState struct {
	EventID   string        `json:"event_id"`
	Instance  string        `json:"instance"`
	TradeDate string        `json:"trade_date"`
	State     instanceState `json:"state"`
}
type gateBlockRecord struct {
	Event  string `json:"event"`
	Reason string `json:"reason"`
	At     string `json:"at"`
}

type Clock interface{ Now() time.Time }
type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

type fakeClock struct {
	mu      sync.Mutex
	now     time.Time
	changed chan struct{}
	waits   int
}

func newFakeClock(t time.Time) *fakeClock { return &fakeClock{now: t, changed: make(chan struct{}, 1)} }
func (c *fakeClock) Now() time.Time       { c.mu.Lock(); defer c.mu.Unlock(); return c.now }
func (c *fakeClock) Advance(d time.Duration) time.Time {
	c.mu.Lock()
	c.now = c.now.Add(d)
	n := c.now
	close(c.changed)
	c.changed = make(chan struct{})
	c.mu.Unlock()
	return n
}
func (c *fakeClock) changedChan() <-chan struct{} { c.mu.Lock(); defer c.mu.Unlock(); return c.changed }
func (c *fakeClock) waitCount() int               { c.mu.Lock(); defer c.mu.Unlock(); return c.waits }
func (r *liveRunner) waitClock(d time.Duration) {
	if d <= 0 {
		return
	}
	if r.fake != nil {
		target := r.clock.Now().Add(d)
		for r.clock.Now().Before(target) {
			ch := r.fake.changedChan()
			r.fake.mu.Lock()
			r.fake.waits++
			r.fake.mu.Unlock()
			<-ch
		}
		return
	}
	t := time.NewTimer(d)
	<-t.C
}

func startupClassify(c config.Config, now time.Time) (missed, active []Instance) {
	date := now.Format("2006-01-02")
	xs, _, err := instances(c, date)
	if err != nil {
		return nil, nil
	}
	for _, x := range xs {
		event := findEvent(c, x.EventID)
		start, e1 := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Start, now.Location())
		deadline, deadlineErr := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Deadline, now.Location())
		end := deadline
		endErr := deadlineErr
		if x.LateUntil != "" {
			end, endErr = time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.LateUntil, now.Location())
		}
		if e1 != nil || deadlineErr != nil || endErr != nil {
			continue
		}
		if event != nil && event.OnDeadline == "missed" && !now.Truncate(time.Second).Before(deadline.Truncate(time.Second)) {
			missed = append(missed, x)
			continue
		}
		if windowExpired(now, end) {
			missed = append(missed, x)
			continue
		}
		if !now.Before(start) {
			if x.Catchup != "run_in_window" && pastScheduleSecond(now, start) {
				missed = append(missed, x)
				continue
			}
			if event != nil && event.OnDeadline == "degrade" && !now.Truncate(time.Second).Before(deadline.Truncate(time.Second)) {
				x.DegradeRequested = true
			}
			active = append(active, x)
		}
	}
	return
}

func pastScheduleSecond(now, scheduled time.Time) bool {
	return now.Truncate(time.Second).After(scheduled.Truncate(time.Second))
}

func windowExpired(now, end time.Time) bool {
	return pastScheduleSecond(now, end)
}

func triggerWindowAllowed(x Instance, date string, now time.Time, loc *time.Location) error {
	start, e := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Start, loc)
	if e != nil {
		return e
	}
	cutoff := x.Deadline
	if x.LateUntil != "" {
		cutoff = x.LateUntil
	}
	end, e := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, loc)
	if e != nil {
		return e
	}
	if now.Before(start) || windowExpired(now, end) {
		return errors.New("outside event window")
	}
	return nil
}

type liveRunner struct {
	c                config.Config
	hash, root, mode string
	loc              *time.Location
	start            time.Time
	mu               sync.Mutex
	configMu         sync.RWMutex
	states           map[string]instanceState
	running          map[string]bool
	paused           map[string]bool
	done             chan string
	missedStartup    []string
	ledger           *ledger
	ledgerMu         sync.RWMutex
	boot             string
	tick             uint64
	nextDue          string
	clock            Clock
	fake             *fakeClock
	classSlots       map[string]chan struct{}
	listening        bool
	heavyRunning     bool
	processSlots     chan struct{}
	loadavg          func() (float64, error)
	alertSeen        map[string]bool
	alertAttempts    map[string]int
	alertInFlight    map[string]bool
	alertMinutes     map[int64]int
	alertDaily       int
	gateBlocked      []gateBlockRecord
	gateAlerted      map[string]bool
	gateSummaryHour  string
	idempotency      map[string]any
	cfgPath          string
	bootSummary      map[string]string
	bootSequence     string
	bootReruns       int
	bootRerunIDs     map[string]bool
	bootRerunPending bool
}

func phaseIn(now time.Time, phases []config.Phase, tradingDay bool) (string, string) {
	if !tradingDay {
		return "NON_TRADING", ""
	}
	sec := now.Hour()*3600 + now.Minute()*60 + now.Second()
	for i, p := range phases {
		start, _ := time.Parse("15:04", p.Start)
		end := 0
		if p.End == "24:00" {
			end = 86400
		} else {
			t, _ := time.Parse("15:04", p.End)
			end = t.Hour()*3600 + t.Minute()*60
		}
		if sec >= start.Hour()*3600+start.Minute()*60 && sec < end {
			next := ""
			if i+1 < len(phases) {
				next = phases[i+1].Name
			}
			return p.Name, next
		}
	}
	return "NON_TRADING", ""
}
func (r *liveRunner) currentPhase(now time.Time) (string, string) {
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	trading := true
	if cal, ok := c.Calendars["trading"]; ok {
		if dates, err := readTradingDates(cal.File); err == nil {
			date := now.In(r.loc).Format("2006-01-02")
			trading = dates.days[date]
			if !trading {
				if dates.min == "" || date < dates.min || date > dates.max {
					return "CALENDAR_UNKNOWN", ""
				}
			}
		} else {
			return "CALENDAR_UNKNOWN", ""
		}
	} else {
		return "CALENDAR_UNKNOWN", ""
	}
	return phaseIn(now.In(r.loc), c.EffectivePhases(), trading)
}

func phaseAllowed(event *config.Event, phase string) bool {
	if event == nil || len(event.Phases) == 0 {
		return true
	}
	for _, p := range event.Phases {
		if p == "any" || p == phase {
			return true
		}
	}
	return false
}

func phaseAllowedAt(event *config.Event, phase string, now time.Time, c config.Config, loc *time.Location) bool {
	if !phaseAllowed(event, phase) {
		return false
	}
	cutoff, ok := c.HeavyCutoffs[phase]
	if !ok || eventCost(event) != "heavy" {
		return true
	}
	limit, err := time.ParseInLocation("15:04:05", cutoff, loc)
	if err != nil {
		return true
	}
	local := now.In(loc)
	return local.Hour()*3600+local.Minute()*60+local.Second() < limit.Hour()*3600+limit.Minute()*60+limit.Second()
}

func nextAllowedPhase(event *config.Event, phases []config.Phase, current string) string {
	if event == nil {
		return ""
	}
	if len(phases) == 0 {
		phases = config.DefaultPhases()
	}
	allowed := map[string]bool{}
	for _, p := range event.Phases {
		allowed[p] = true
	}
	idx := -1
	for i, p := range phases {
		if p.Name == current {
			idx = i
			break
		}
	}
	for step := 1; step <= len(phases); step++ {
		p := phases[(idx+step+len(phases))%len(phases)].Name
		if allowed[p] {
			return p
		}
	}
	if len(event.Phases) > 0 {
		return event.Phases[0]
	}
	return ""
}

func eventCost(event *config.Event) string {
	if event == nil || event.Cost == "" {
		return "medium"
	}
	return event.Cost
}

func fetchItemEvent(event *config.Event, state instanceState) *config.Event {
	if event == nil || state.Cost == "" {
		return event
	}
	copy := *event
	copy.Cost = state.Cost
	if state.Cost == "light" {
		copy.Phases = nil
	}
	return &copy
}

func configuredHeavyLoad(c config.Config) float64 {
	if len(c.HeavyMaxLoad1) == 0 {
		return 0
	}
	var number float64
	if json.Unmarshal(c.HeavyMaxLoad1, &number) == nil {
		return number
	}
	var value string
	if json.Unmarshal(c.HeavyMaxLoad1, &value) == nil && value == "cpus" {
		return float64(runtime.NumCPU())
	}
	return 0
}

func readLoad1() (float64, error) {
	b, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return 0, err
	}
	fields := strings.Fields(string(b))
	if len(fields) == 0 {
		return 0, fmt.Errorf("empty /proc/loadavg")
	}
	return strconv.ParseFloat(fields[0], 64)
}

func (r *liveRunner) processSemaphore() chan struct{} {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.processSlots == nil {
		r.processSlots = make(chan struct{}, 16)
	}
	return r.processSlots
}
func (r *liveRunner) phaseAt(now time.Time) (string, string) { return r.currentPhase(now) }

func runCmd(args []string) error {
	f := flag.NewFlagSet("run", flag.ContinueOnError)
	cfgPath := f.String("config", "", "config")
	root := f.String("state-root", "", "state root")
	mode := f.String("mode", "live", "live|fakeclock")
	clockStart := f.String("clock-start", "", "fake clock start")
	if err := f.Parse(args); err != nil {
		return err
	}
	if *cfgPath == "" || *root == "" {
		return fmt.Errorf("usage: guardiand run --config CFG --state-root DIR [--mode live|fakeclock] [--clock-start T]")
	}
	if *mode != "live" && *mode != "fakeclock" {
		return fmt.Errorf("BAD_MODE")
	}
	c, h, err := load(*cfgPath)
	if err != nil {
		return err
	}
	loc, err := time.LoadLocation(c.Timezone)
	if err != nil {
		return err
	}
	now := time.Now().In(loc)
	if *mode == "fakeclock" {
		if *clockStart == "" {
			return fmt.Errorf("fakeclock requires --clock-start")
		}
		now, err = time.ParseInLocation(time.RFC3339, *clockStart, loc)
		if err != nil {
			return err
		}
	}
	if err = os.MkdirAll(*root, 0750); err != nil {
		return err
	}
	lock, err := os.OpenFile(filepath.Join(*root, "guardiand.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	if err = lock.Chmod(0600); err != nil {
		lock.Close()
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return fmt.Errorf("BUSY: guardian already running")
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	bootBytes := make([]byte, 16)
	_, _ = rand.Read(bootBytes)
	fc := (*fakeClock)(nil)
	var clk Clock = realClock{}
	if *mode == "fakeclock" {
		fc = newFakeClock(now)
		clk = fc
	}
	r := &liveRunner{c: c, hash: h, root: *root, mode: *mode, loc: loc, start: now, states: map[string]instanceState{}, running: map[string]bool{}, paused: map[string]bool{}, done: make(chan string, 256), boot: hex.EncodeToString(bootBytes), clock: clk, fake: fc, classSlots: map[string]chan struct{}{}, alertSeen: map[string]bool{}, alertMinutes: map[int64]int{}, idempotency: map[string]any{}, cfgPath: *cfgPath}
	r.ledger, err = openLedger(*root, now.Format("2006-01-02"))
	if err != nil {
		return err
	}
	r.ledger.now = r.clock.Now
	defer r.ledger.close()
	if err = r.recover(); err != nil {
		return err
	}
	if err = r.ledger.append("BOOT", map[string]any{"config_version": c.ConfigVersion, "config_hash": h, "build": "dev", "boot_id": r.boot}); err != nil {
		return err
	}
	// Control socket first: boot events can take minutes and must stay observable (status/pause) meanwhile.
	if err = r.listen(); err != nil {
		return err
	}
	r.listening = true
	// Boot events can run for minutes (real-source backfills); keep the heartbeat advancing so keepalive does not
	// mistake the busy boot chain for a stalled daemon and kill it mid-fetch.
	bootDone := make(chan struct{})
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-bootDone:
				return
			case <-ticker.C:
				r.mu.Lock()
				r.tick++
				r.mu.Unlock()
				_ = r.heartbeat(r.clock.Now())
			}
		}
	}()
	err = r.runBootEvents(now)
	close(bootDone)
	if err != nil {
		return err
	}
	missed, active := startupClassify(c, now)
	if err = r.applyStartup(now, missed, active); err != nil {
		return err
	}
	if err = r.startSocket(); err != nil {
		return err
	}
	return r.loop()
}

func (r *liveRunner) scheduleBootRerun(date string) {
	r.mu.Lock()
	if r.bootReruns >= 3 || r.bootRerunPending {
		r.mu.Unlock()
		return
	}
	ids := map[string]bool{}
	for id, status := range r.bootSummary {
		if status != "SUCCEEDED" && status != "SUCCEEDED_DEGRADED" && status != "SKIPPED_NA" {
			ids[id] = true
		}
	}
	r.mu.Unlock()
	r.configMu.RLock()
	events := append([]config.Event(nil), r.c.Events...)
	r.configMu.RUnlock()
	changed := true
	for changed {
		changed = false
		for _, event := range events {
			if event.Trigger.Type != "startup" || ids[event.ID] {
				continue
			}
			for _, dep := range event.Trigger.After {
				if ids[dep] {
					ids[event.ID] = true
					changed = true
					break
				}
			}
		}
	}
	if len(ids) == 0 {
		return
	}
	r.mu.Lock()
	if r.bootReruns >= 3 || r.bootRerunPending {
		r.mu.Unlock()
		return
	}
	r.bootReruns++
	r.bootRerunPending = true
	attempt := r.bootReruns
	r.bootRerunIDs = ids
	r.mu.Unlock()
	go func() {
		r.waitClock(time.Duration(60*(1<<(attempt-1))) * time.Second)
		_, _ = r.appendLedger(date, "BOOT_RERUN", map[string]any{"attempt": attempt, "events": ids, "sequence": r.bootSequence})
		_ = r.runBootEvents(r.clock.Now())
		r.mu.Lock()
		r.bootRerunIDs = nil
		r.bootRerunPending = false
		r.mu.Unlock()
	}()
}

func (r *liveRunner) runBootEvents(now time.Time) error {
	r.mu.Lock()
	rerunOnly := r.bootRerunIDs
	priorSummary := make(map[string]string, len(r.bootSummary))
	for id, status := range r.bootSummary {
		priorSummary[id] = status
	}
	r.mu.Unlock()
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	var events []config.Event
	for _, e := range c.Events {
		if e.Trigger.Type == "startup" {
			events = append(events, e)
		}
	}
	eventByID := map[string]config.Event{}
	for _, e := range events {
		eventByID[e.ID] = e
	}
	ordered := make([]config.Event, 0, len(events))
	emitted := map[string]bool{}
	for len(ordered) < len(events) {
		eligible := []config.Event{}
		for _, e := range events {
			if emitted[e.ID] {
				continue
			}
			ready := true
			for _, d := range e.Trigger.After {
				if _, isBoot := eventByID[d]; isBoot && !emitted[d] {
					ready = false
					break
				}
			}
			if ready {
				eligible = append(eligible, e)
			}
		}
		if len(eligible) == 0 {
			return fmt.Errorf("startup dependency cycle")
		}
		sort.Slice(eligible, func(i, j int) bool {
			if eligible[i].Trigger.Order == eligible[j].Trigger.Order {
				return eligible[i].ID < eligible[j].ID
			}
			return eligible[i].Trigger.Order < eligible[j].Trigger.Order
		})
		e := eligible[0]
		ordered = append(ordered, e)
		emitted[e.ID] = true
	}
	events = ordered
	date := now.In(r.loc).Format("2006-01-02")
	if r.bootSequence == "" {
		r.bootSequence = fmt.Sprint(r.ledger.sequence())
	}
	boot := r.bootSequence
	results := map[string]string{}
	for _, e := range events {
		if rerunOnly != nil && !rerunOnly[e.ID] {
			results[e.ID] = priorSummary[e.ID]
			continue
		}
		// The startup event exists as a template; only deferred per-item instances run it.
		if e.ID == "fetch_item" {
			results[e.ID] = "SUCCEEDED"
			continue
		}
		blocked := ""
		for _, dep := range e.Trigger.After {
			s := results[dep]
			if s != "SUCCEEDED" && s != "SUCCEEDED_DEGRADED" {
				blocked = dep
				break
			}
		}
		x := Instance{ID: e.ID + "@boot" + boot, EventID: e.ID, Start: "00:00:00", Deadline: "23:59:59", LateUntil: e.LateUntil, Timeout: e.Timeout, MaxAttempts: e.Retry.MaxAttempts, Calendar: "BOOT", TriggerType: "startup"}
		var calendarDates tradingDates
		if tc, ok := c.Calendars["trading"]; ok {
			calendarDates, _ = readTradingDates(tc.File)
		}
		if strings.HasPrefix(calendarStatusInRange(e.Calendar, date, calendarDates.days, calendarDates.source, calendarDates.min, calendarDates.max), "EXCLUDED") {
			st := instanceState{Status: "SKIPPED_NA", LastError: "calendar excluded", CompletedAt: r.clock.Now().Format(time.RFC3339)}
			r.setState(x, date, st)
			results[e.ID] = st.Status
			_, _ = r.appendLedger(date, "BOOT_CHECK", map[string]any{"event": e.ID, "instance": x.ID, "status": st.Status, "order": e.Trigger.Order})
			continue
		}
		phase, _ := r.currentPhase(now)
		phaseAllowed := len(e.Phases) == 0
		for _, p := range e.Phases {
			if p == "any" || p == phase {
				phaseAllowed = true
			}
		}
		if phase == "NON_TRADING" || phase == "CALENDAR_UNKNOWN" {
			phaseAllowed = e.ID == "boot_trade_day"
		}
		if !phaseAllowed {
			status := "SKIPPED_PHASE"
			st := instanceState{Status: status, LastError: phase, CompletedAt: r.clock.Now().Format(time.RFC3339)}
			if e.OnPhaseMismatch == "defer" && phase != "NON_TRADING" && phase != "CALENDAR_UNKNOWN" {
				st = instanceState{Status: "DEFERRED", LastError: "phase " + phase + " is not allowed", NextPhase: nextAllowedPhase(&e, c.EffectivePhases(), phase)}
			}
			r.setState(x, date, st)
			results[e.ID] = st.Status
			_, _ = r.appendLedger(date, "BOOT_CHECK", map[string]any{"event": e.ID, "instance": x.ID, "status": st.Status, "order": e.Trigger.Order})
			continue
		}
		if blocked != "" {
			st := instanceState{Status: "SKIPPED_DEP", LastError: "dependency failed: " + blocked, CompletedAt: r.clock.Now().Format(time.RFC3339)}
			r.setState(x, date, st)
			results[e.ID] = st.Status
			_, _ = r.appendLedger(date, "BOOT_CHECK", map[string]any{"event": e.ID, "instance": x.ID, "status": st.Status, "order": e.Trigger.Order})
			continue
		}
		r.execute(x, date)
		r.mu.Lock()
		st := r.states[date+"/"+x.ID]
		r.mu.Unlock()
		results[e.ID] = st.Status
		_, _ = r.appendLedger(date, "BOOT_CHECK", map[string]any{"event": e.ID, "instance": x.ID, "status": st.Status, "order": e.Trigger.Order})
	}
	r.mu.Lock()
	r.bootSummary = results
	r.mu.Unlock()
	// Reconcile windows that have passed their normal start while their late cutoff remains open.
	xs, _, err := instances(c, date)
	if err == nil {
		for _, x := range xs {
			e := findEvent(c, x.EventID)
			if e == nil || e.OnStart != "reconcile" {
				continue
			}
			end, parseErr := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Deadline, r.loc)
			if parseErr != nil || now.Before(end) {
				continue
			}
			key := date + "/" + x.ID
			r.mu.Lock()
			prior, exists := r.states[key]
			r.mu.Unlock()
			if exists && (prior.Status == "SUCCEEDED" || prior.Status == "SUCCEEDED_DEGRADED") {
				continue
			}
			if x.LateUntil == "" {
				r.bootSummary["gap:"+x.ID] = "MISSED_NO_LATE_UNTIL"
				continue
			}
			cutoff, pe := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.LateUntil, r.loc)
			if pe == nil && now.Before(cutoff) {
				go r.execute(x, date)
			}
		}
	}
	return nil
}

func (r *liveRunner) applyStartup(now time.Time, missed, active []Instance) error {
	for _, x := range missed {
		if _, exists := r.states[now.Format("2006-01-02")+"/"+x.ID]; exists {
			continue
		}
		r.setState(x, now.Format("2006-01-02"), instanceState{Status: "MISSED", CompletedAt: now.Format(time.RFC3339)})
		r.missedStartup = append(r.missedStartup, x.ID)
	}
	var summary []string
	for id, status := range r.bootSummary {
		if status != "SUCCEEDED" && status != "SUCCEEDED_DEGRADED" && status != "SKIPPED_PHASE" && status != "SKIPPED_NA" {
			summary = append(summary, id+":"+status)
		}
	}
	if len(r.missedStartup) > 0 {
		summary = append(summary, r.missedStartup...)
	}
	for id, status := range r.bootSummary {
		if strings.HasPrefix(id, "gap:") {
			summary = append(summary, id+":"+status)
		}
	}
	if len(summary) > 0 {
		sort.Strings(summary)
		if err := r.alert("MISSED", map[string]any{"count": len(summary), "instances": summary, "boot": r.bootSummary, "summary": true}); err != nil {
			return err
		}
	}
	for _, x := range active {
		if st, exists := r.states[now.Format("2006-01-02")+"/"+x.ID]; exists && isTerminal(st.Status) {
			continue
		}
		go r.execute(x, now.Format("2006-01-02"))
	}
	return nil
}
func isTerminal(status string) bool {
	switch status {
	case "SUCCEEDED", "SUCCEEDED_DEGRADED", "FAILED_FINAL", "CIRCUIT_OPEN", "MISSED", "GATE_BLOCKED", "SKIPPED", "SKIPPED_NA", "SKIPPED_SATISFIED", "SKIPPED_PHASE", "SKIPPED_DEP":
		return true
	}
	return false
}

func fetchItemDependencies(ids []string) []string {
	waitFor := make([]string, 0, len(ids))
	for _, id := range ids {
		if id != "" {
			waitFor = append(waitFor, "fetch_item:"+id)
		}
	}
	return waitFor
}

// pendingOwnerEvents returns same-date data owners whose scheduled window can
// still perform the fetch. Their registry entries must not also be queued as
// fetch_item work by a reconciliation pass.
func (r *liveRunner) pendingOwnerEvents(date string, now time.Time) map[string]string {
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	xs, _, err := instances(c, date)
	if err != nil {
		return nil
	}
	pending := map[string]string{}
	for _, x := range xs {
		e := findEvent(c, x.EventID)
		if e == nil || e.Trigger.Type != "window" {
			continue
		}
		key := date + "/" + x.ID
		r.mu.Lock()
		prior, exists := r.states[key]
		r.mu.Unlock()
		if exists && isTerminal(prior.Status) {
			continue
		}
		if !exists {
			start, startErr := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Start, r.loc)
			cutoff := x.Deadline
			if x.LateUntil != "" {
				cutoff = x.LateUntil
			}
			end, parseErr := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
			if startErr != nil || parseErr != nil || now.Before(start) || !now.Before(end) {
				continue
			}
		}
		dataDate := x.DataDate
		if dataDate == "" {
			dataDate = date
		}
		pending[x.EventID] = dataDate
	}
	return pending
}

// rearmInterruptedFetchItems: a fetch_item recovered as RUNNING lost its process with the previous daemon. Put it back
// in the queue (the interrupted attempt does not count) so the job resumes from its own checkpoint instead of being
// ignored forever as "already running".
func (r *liveRunner) rearmInterruptedFetchItems() {
	for key, st := range r.states {
		if !strings.Contains(key, "/fetch_item:") || st.Status != "RUNNING" {
			continue
		}
		if st.Attempts > 0 {
			st.Attempts--
		}
		st.Status = "DEFERRED"
		st.LastError = "interrupted by restart"
		st.NextPhase = ""
		st.NextAttemptAt = ""
		r.states[key] = st
	}
}

func (r *liveRunner) recover() error {
	date := r.start.Format("2006-01-02")
	if b, err := os.ReadFile(filepath.Join(r.root, "ledger", "snapshot-"+date+".json")); err == nil {
		var snap struct {
			Events map[string]instanceState `json:"events"`
		}
		if json.Unmarshal(b, &snap) == nil {
			for id, st := range snap.Events {
				r.states[date+"/"+id] = st
			}
		}
	}
	p := filepath.Join(r.root, "ledger", date+".jsonl")
	f, e := os.Open(p)
	if e != nil {
		if os.IsNotExist(e) {
			return nil
		}
		return e
	}
	defer f.Close()
	defer r.rearmInterruptedFetchItems()
	scan := bufio.NewScanner(f)
	for scan.Scan() {
		var rec ledgerRecord
		if json.Unmarshal(scan.Bytes(), &rec) != nil || rec.CRC32 != recordCRC(rec) {
			continue
		}
		if rec.Type == "INSTANCE_STATE" {
			b, _ := json.Marshal(rec.Data)
			var s runState
			if json.Unmarshal(b, &s) == nil {
				r.states[s.TradeDate+"/"+s.Instance] = s.State
			}
		}
		if rec.Type == "ALERT_OUT" || rec.Type == "ALERT_FAILED" {
			b, _ := json.Marshal(rec.Data)
			var v struct {
				Key      string `json:"key"`
				Attempts int    `json:"attempts"`
			}
			_ = json.Unmarshal(b, &v)
			if v.Key != "" {
				if r.alertAttempts == nil {
					r.alertAttempts = map[string]int{}
				}
				if v.Attempts > r.alertAttempts[v.Key] {
					r.alertAttempts[v.Key] = v.Attempts
				} else {
					r.alertAttempts[v.Key]++
				}
				if at, e := time.Parse(time.RFC3339Nano, rec.TS); e == nil {
					r.alertMinutes[at.Unix()/60]++
				}
				r.alertDaily++
				if rec.Type == "ALERT_OUT" {
					r.alertSeen[v.Key] = true
				}
			}
		}
		if rec.Type == "CONTROL" {
			b, _ := json.Marshal(rec.Data)
			var v struct {
				Idem   string `json:"idem"`
				Result any    `json:"result"`
				Op     string `json:"op"`
				Event  string `json:"event"`
			}
			_ = json.Unmarshal(b, &v)
			if v.Idem != "" {
				r.idempotency[v.Idem] = v.Result
			}
			if v.Op == "pause" || v.Op == "resume" {
				r.paused[v.Event] = v.Op == "pause"
			}
		}
		if rec.Type == "GATE_HANDLER_BLOCKED" || rec.Type == "GATE_HANDLER_RECOVERED" {
			b, _ := json.Marshal(rec.Data)
			var v struct {
				Handler string `json:"handler"`
			}
			_ = json.Unmarshal(b, &v)
			if v.Handler != "" {
				if r.gateAlerted == nil {
					r.gateAlerted = map[string]bool{}
				}
				r.gateAlerted[v.Handler] = rec.Type == "GATE_HANDLER_BLOCKED"
			}
		}
	}
	return scan.Err()
}
func (r *liveRunner) setState(x Instance, date string, s instanceState) {
	key := date + "/" + x.ID
	r.mu.Lock()
	r.states[key] = s
	if x.TriggerType == "startup" {
		if r.bootSummary == nil {
			r.bootSummary = map[string]string{}
		}
		r.bootSummary[x.EventID] = s.Status
	}
	r.mu.Unlock()
	seq, err := r.appendLedger(date, "INSTANCE_STATE", runState{EventID: x.EventID, Instance: x.ID, TradeDate: date, State: s})
	if err == nil && seq > 0 {
		_ = r.writeSnapshot(date, seq)
	}
	r.exportState(date)
}
func (r *liveRunner) appendLedger(date, typ string, data any) (uint64, error) {
	r.ledgerMu.Lock()
	defer r.ledgerMu.Unlock()
	if r.ledger == nil {
		return 0, nil
	}
	if r.ledger.date == date {
		if err := r.ledger.append(typ, data); err != nil {
			return 0, err
		}
		return r.ledger.sequence(), nil
	}
	l, err := openLedger(r.root, date)
	if err != nil {
		return 0, err
	}
	if r.clock != nil {
		l.now = r.clock.Now
	}
	if err = l.append(typ, data); err != nil {
		_ = l.close()
		return 0, err
	}
	seq := l.sequence()
	return seq, l.close()
}
func (r *liveRunner) appendCurrentLedger(typ string, data any) error {
	r.ledgerMu.RLock()
	l := r.ledger
	if l == nil {
		r.ledgerMu.RUnlock()
		return nil
	}
	err := l.append(typ, data)
	r.ledgerMu.RUnlock()
	return err
}
func (r *liveRunner) writeSnapshot(date string, seq uint64) error {
	r.mu.Lock()
	states := map[string]instanceState{}
	for k, v := range r.states {
		if strings.HasPrefix(k, date+"/") {
			states[strings.TrimPrefix(k, date+"/")] = v
		}
	}
	r.mu.Unlock()
	b, err := json.Marshal(map[string]any{"date": date, "seq": seq, "events": states})
	if err != nil {
		return err
	}
	return atomicFile(filepath.Join(r.root, "ledger", "snapshot-"+date+".json"), b, 0600)
}
func (r *liveRunner) exportState(date string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	events := map[string]instanceState{}
	for k, v := range r.states {
		if strings.HasPrefix(k, date+"/") {
			events[strings.TrimPrefix(k, date+"/")] = v
		}
	}
	next := r.nextDue
	r.configMu.RLock()
	version := r.c.ConfigVersion
	r.configMu.RUnlock()
	paused := r.paused["*"]
	b, _ := json.MarshalIndent(map[string]any{"trade_date": date, "events": events, "heartbeat": map[string]any{"trade_date": date, "events": events, "config_revision": version, "health": "READY", "last_tick_at": r.clock.Now().Format(time.RFC3339Nano), "next_event": next, "paused": paused, "phase": ""}}, "", "  ")
	_ = atomicFile(filepath.Join(r.root, date, "state.json"), b, 0640)
}
func (r *liveRunner) execute(x Instance, date string) {
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	event := findEvent(c, x.EventID)
	if event == nil {
		return
	}
	deadlineAt, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Deadline, r.loc)
	degradeRequested := x.DegradeRequested || x.TriggerType != "startup" && event.OnDeadline == "degrade" && !r.clock.Now().Truncate(time.Second).Before(deadlineAt.Truncate(time.Second))
	r.mu.Lock()
	prior := r.states[date+"/"+x.ID]
	r.mu.Unlock()
	if x.EventID == "fetch_item" {
		event = fetchItemEvent(event, prior)
	}
	cost := eventCost(event)
	r.mu.Lock()
	if r.running[x.ID] {
		r.mu.Unlock()
		return
	}
	if cost == "heavy" && r.heavyRunning {
		prior := r.states[date+"/"+x.ID]
		r.mu.Unlock()
		if prior.Status != "QUEUED_HEAVY" {
			prior.Status = "QUEUED_HEAVY"
			prior.LastError = "heavy slot occupied"
			prior.CompletedAt = ""
			if prior.DataDate == "" {
				prior.DataDate = x.DataDate
			}
			r.setState(x, date, prior)
		}
		return
	}
	if cost == "heavy" {
		r.heavyRunning = true
	}
	r.running[x.ID] = true
	prior = r.states[date+"/"+x.ID]
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		delete(r.running, x.ID)
		if cost == "heavy" {
			r.heavyRunning = false
		}
		r.mu.Unlock()
	}()
	if prior.NextAttemptAt != "" && (!degradeRequested || prior.Late) {
		next, err := time.Parse(time.RFC3339, prior.NextAttemptAt)
		if err == nil && r.clock.Now().Before(next) {
			return
		}
	}
	if x.TriggerType != "startup" && event.OnDeadline == "missed" && !r.clock.Now().Truncate(time.Second).Before(deadlineAt.Truncate(time.Second)) {
		r.setState(x, date, instanceState{Status: "MISSED", Attempts: prior.Attempts, CompletedAt: r.clock.Now().Format(time.RFC3339), LastError: "ON_DEADLINE_MISSED"})
		return
	}
	if (x.TriggerType != "startup" || x.EventID == "fetch_item") && !phaseAllowedAt(event, func() string { p, _ := r.currentPhase(r.clock.Now()); return p }(), r.clock.Now(), c, r.loc) {
		phase, _ := r.currentPhase(r.clock.Now())
		if event.OnPhaseMismatch == "defer" {
			deferred := prior
			deferred.Status = "DEFERRED"
			deferred.LastError = "phase " + phase + " is not allowed"
			deferred.CompletedAt = ""
			deferred.NextPhase = nextAllowedPhase(event, c.EffectivePhases(), phase)
			if deferred.DataDate == "" {
				deferred.DataDate = x.DataDate
			}
			r.setState(x, date, deferred)
		} else {
			r.setState(x, date, instanceState{Status: "SKIPPED_PHASE", LastError: phase, CompletedAt: r.clock.Now().Format(time.RFC3339)})
		}
		return
	}
	var slots chan struct{}
	if cost == "medium" {
		class := event.Class
		if class == "" {
			class = "default"
		}
		limit := 4 // unconfigured classes: one slow handler must not starve the rest of the day
		if raw := c.Concurrency[class]; len(raw) > 0 {
			var v struct {
				Max int `json:"max"`
			}
			if json.Unmarshal(raw, &v) == nil && v.Max > 0 {
				limit = v.Max
			}
		}
		r.mu.Lock()
		slots = r.classSlots[class]
		if slots == nil {
			slots = make(chan struct{}, limit)
			r.classSlots[class] = slots
		}
		r.mu.Unlock()
	}
	isLate := x.TriggerType != "startup" && !r.clock.Now().Before(deadlineAt)
	st := instanceState{Status: "RUNNING", StartedAt: r.clock.Now().Format(time.RFC3339), Late: isLate}
	if isLate {
		st.Status = "LATE_RUN"
	}
	if prior.Status == "RUNNING" || prior.Status == "LATE_RUN" || prior.Status == "RETRY_WAIT" || prior.Status == "DEFERRED" || prior.Status == "DEFER_LOAD" || prior.Status == "QUEUED_HEAVY" {
		st.Attempts = prior.Attempts
		st.DataDate = prior.DataDate
		if st.DataDate == "" {
			st.DataDate = x.DataDate
		}
		st.NextAttemptAt = prior.NextAttemptAt
		st.Rung = prior.Rung
		st.Tier = prior.Tier
		st.RungAttempts = prior.RungAttempts
		st.SkippedTiers = append([]string(nil), prior.SkippedTiers...)
		if prior.StartedAt != "" {
			st.StartedAt = prior.StartedAt
		}
	}
	r.setState(x, date, st)
	if event.Gate.Ref != "" {
		h := c.Handlers[event.Handler]
		poll := 3 * time.Second
		if d, e := time.ParseDuration(event.Gate.Poll); e == nil && d > 0 {
			poll = d
		}
		cutoff := x.Deadline
		if x.LateUntil != "" && event.OnDeadline != "degrade" && event.OnDeadline != "missed" {
			cutoff = x.LateUntil
		}
		deadline, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
		ready := false
		lastGateReason := "gate not ready"
		pollCount := 0
		firstPollAt, lastPollAt := "", ""
		lastSignature := ""
		for r.clock.Now().Before(deadline) {
			if len(h.GateArgv) == 0 {
				break
			}
			processSlots := r.processSemaphore()
			processSlots <- struct{}{}
			gateEvent := *event
			gateEvent.Timeout = event.Gate.Timeout
			if gateEvent.Timeout == "" {
				gateEvent.Timeout = "15s"
			}
			out, stderr, err := r.invokeDetailed(h.GateArgv, &gateEvent, x, date, 1, true)
			<-processSlots
			polledAt := r.clock.Now().Format(time.RFC3339Nano)
			pollCount++
			if firstPollAt == "" {
				firstPollAt = polledAt
			}
			lastPollAt = polledAt
			info := parseGateOutput(out)
			if err != nil && info.Reason == "" {
				info.Reason = err.Error()
			}
			if info.Status == "" {
				info.Status = "unknown"
			}
			if info.Reason == "" {
				info.Reason = gateReason(out, err)
			}
			lastGateReason = info.Reason
			if info.Invalid {
				info.Stdout = truncateText(string(out), 300)
				info.Stderr = truncateText(string(stderr), 300)
			}
			signature, _ := json.Marshal([]any{info.Status, info.Reason})
			if string(signature) != lastSignature {
				_, _ = r.appendLedger(date, "GATE_POLL", map[string]any{"event": x.EventID, "instance": x.ID, "polled_at": polledAt, "status": info.Status, "reason": info.Reason, "row_count": info.RowCount, "elapsed_ms": info.ElapsedMS, "query_window": info.QueryWindow, "exit_code": exitCode(err), "stdout": info.Stdout, "stderr": info.Stderr})
				lastSignature = string(signature)
			}
			if err == nil && gateReady(out) && r.clock.Now().Before(deadline) {
				ready = true
				_, _ = r.appendLedger(date, "GATE_POLL_SUMMARY", map[string]any{"event": x.EventID, "instance": x.ID, "poll_count": pollCount, "first_polled_at": firstPollAt, "last_polled_at": lastPollAt, "outcome": "ready"})
				if info.CheckpointTS != "" {
					if checkpoint, parseErr := parseGateTimestamp(info.CheckpointTS, r.loc); parseErr == nil {
						_, _ = r.appendLedger(date, "GATE_READY", map[string]any{"event": x.EventID, "instance": x.ID, "polled_at": polledAt, "window_end": info.CheckpointTS, "visible_lag_seconds": r.clock.Now().Sub(checkpoint).Seconds()})
					}
				}
				break
			}
			if err == nil && gateReady(out) && !r.clock.Now().Before(deadline) {
				lastGateReason = "start deadline expired while waiting for gate"
			}
			left := deadline.Sub(r.clock.Now())
			if left <= 0 {
				break
			}
			if left < poll {
				poll = left
			}
			r.waitClock(poll)
		}
		if !ready {
			if event.OnDeadline == "degrade" {
				degradeRequested = true
				_, _ = r.appendLedger(date, "GATE_POLL_SUMMARY", map[string]any{"event": x.EventID, "instance": x.ID, "poll_count": pollCount, "first_polled_at": firstPollAt, "last_polled_at": lastPollAt, "outcome": "degrade_requested", "reason": lastGateReason})
			} else if event.OnDeadline == "missed" {
				_, _ = r.appendLedger(date, "GATE_POLL_SUMMARY", map[string]any{"event": x.EventID, "instance": x.ID, "poll_count": pollCount, "first_polled_at": firstPollAt, "last_polled_at": lastPollAt, "outcome": "missed", "reason": lastGateReason})
				r.setState(x, date, instanceState{Status: "MISSED", Attempts: prior.Attempts, LastError: lastGateReason, CompletedAt: r.clock.Now().Format(time.RFC3339)})
				return
			} else {
				_, _ = r.appendLedger(date, "GATE_POLL_SUMMARY", map[string]any{"event": x.EventID, "instance": x.ID, "poll_count": pollCount, "first_polled_at": firstPollAt, "last_polled_at": lastPollAt, "outcome": "blocked", "reason": lastGateReason})
				r.setState(x, date, instanceState{Status: "GATE_BLOCKED", LastError: lastGateReason, CompletedAt: r.clock.Now().Format(time.RFC3339)})
				r.mu.Lock()
				r.gateBlocked = append(r.gateBlocked, gateBlockRecord{Event: x.EventID, Reason: lastGateReason, At: r.clock.Now().Format(time.RFC3339Nano)})
				if r.gateAlerted == nil {
					r.gateAlerted = map[string]bool{}
				}
				first := !r.gateAlerted[event.Handler]
				r.gateAlerted[event.Handler] = true
				r.mu.Unlock()
				if first {
					_, _ = r.appendLedger(date, "GATE_HANDLER_BLOCKED", map[string]any{"handler": event.Handler, "event": x.EventID})
					_ = r.alert("GATE_BLOCKED_FIRST", map[string]any{"event": x.EventID, "handler": event.Handler, "gate_reason": lastGateReason})
				}
				return
			}
		}
	}
	// Gate polling must not hold a worker or concurrency slot.
	if cost == "heavy" {
		threshold := configuredHeavyLoad(c)
		if threshold > 0 {
			loader := r.loadavg
			if loader == nil {
				loader = readLoad1
			}
			cutoff := x.Deadline
			if x.LateUntil != "" {
				cutoff = x.LateUntil
			}
			end, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
			for {
				load, err := loader()
				if err == nil && load <= threshold {
					break
				}
				reason := fmt.Sprintf("load1 %.2f exceeds %.2f", load, threshold)
				if err != nil {
					reason = "load1 unavailable: " + err.Error()
				}
				if !r.clock.Now().Before(end) || r.clock.Now().In(r.loc).Format("2006-01-02") != date {
					r.setState(x, date, instanceState{Status: "MISSED", LastError: "DEFER_LOAD_EXPIRED: " + reason, CompletedAt: r.clock.Now().Format(time.RFC3339)})
					_ = r.alert("MISSED", map[string]any{"date": date, "instances": []string{x.ID}, "count": 1, "summary": true})
					return
				}
				r.setState(x, date, instanceState{Status: "DEFER_LOAD", LastError: reason, NextAttemptAt: r.clock.Now().Add(time.Minute).Format(time.RFC3339), Late: st.Late})
				delay := time.Minute
				if left := end.Sub(r.clock.Now()); left < delay {
					delay = left
				}
				r.waitClock(delay)
			}
		}
	}
	processSlots := r.processSemaphore()
	processSlots <- struct{}{}
	defer func() { <-processSlots }()
	if slots != nil {
		slots <- struct{}{}
	}
	slotHeld := slots != nil
	defer func() {
		if slotHeld {
			<-slots
		}
	}()
	cutoff := x.Deadline
	if x.LateUntil != "" && event.OnDeadline != "missed" {
		cutoff = x.LateUntil
	}
	startCutoff, parseErr := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
	isLadder := len(event.Ladder) > 0
	if !isLadder && x.TriggerType != "startup" && parseErr == nil && !r.clock.Now().Before(startCutoff) && prior.Status == "RETRY_WAIT" {
		r.finishLateRetries(x, date, event, st)
		return
	}
	if !isLadder && x.TriggerType != "startup" && parseErr == nil && windowExpired(r.clock.Now(), startCutoff) {
		r.setState(x, date, instanceState{Status: "MISSED", CompletedAt: r.clock.Now().Format(time.RFC3339), LastError: "START_DEADLINE_EXPIRED"})
		_ = r.alert("MISSED", map[string]any{"event": x.EventID, "date": date, "error": "START_DEADLINE_EXPIRED"})
		return
	}
	if x.TriggerType != "startup" && !r.clock.Now().Before(deadlineAt) {
		st.Late = true
		st.Status = "LATE_RUN"
	}
	h := c.Handlers[event.Handler]
	if len(h.Argv) == 0 {
		r.setState(x, date, instanceState{Status: "FAILED_FINAL", Attempts: 1, LastError: "handler argv empty", CompletedAt: r.clock.Now().Format(time.RFC3339)})
		return
	}
	if isLadder {
		r.executeLadder(x, date, event, st, h, degradeRequested)
		return
	}
	max := event.Retry.MaxAttempts
	if max < 1 {
		max = 1
	}
	var last string
	cycleEnd := max
	if event.Retry.UntilLate && event.LateUntil != "" {
		if st.Attempts >= max {
			r.finishRetriesExhausted(x, date, event, st)
			return
		}
		cycleEnd = st.Attempts + 1
	}
	var lastRetryable bool
	for n := st.Attempts + 1; n <= cycleEnd; n++ {
		if !slotHeld && slots != nil {
			slots <- struct{}{}
			slotHeld = true
		}
		st.Attempts = n
		st.Status = "RUNNING"
		if st.Late {
			st.Status = "LATE_RUN"
		}
		r.setState(x, date, st)
		attemptDegrade := degradeRequested || x.TriggerType != "startup" && event.OnDeadline == "degrade" && !r.clock.Now().Truncate(time.Second).Before(deadlineAt.Truncate(time.Second))
		out, err := r.invokeWithDegrade(h.Argv, event, x, date, n, false, attemptDegrade)
		status, code, retryable, detail := decodeResult(out, err)
		if status == "not_applicable" {
			r.setState(x, date, instanceState{Status: "SKIPPED_NA", Attempts: n, StartedAt: st.StartedAt, CompletedAt: r.clock.Now().Format(time.RFC3339), DataDate: x.DataDate})
			return
		}
		if status == "ok" || status == "degraded" {
			stateStatus := "SUCCEEDED"
			if status == "degraded" || attemptDegrade {
				stateStatus = "SUCCEEDED_DEGRADED"
			}
			r.setState(x, date, instanceState{Status: stateStatus, Attempts: n, StartedAt: st.StartedAt, CompletedAt: r.clock.Now().Format(time.RFC3339), Late: st.Late, DataDate: x.DataDate})
			if strings.Contains(event.ID, "calendar_refresh") {
				r.scheduleBootRerun(date)
			}
			if event.ID == "boot_apply_plan" || strings.TrimPrefix(event.Handler, "legacy_") == "boot_apply_plan" {
				var response struct {
					Detail struct {
						Deferred []struct {
							ID          string   `json:"id"`
							Partition   string   `json:"partition"`
							RunInPhases []string `json:"run_in_phases"`
							DependsOn   []string `json:"depends_on"`
							Cost        string   `json:"cost"`
						} `json:"deferred"`
						Results []struct {
							ID                 string          `json:"id"`
							Status             string          `json:"status"`
							Partition          string          `json:"partition"`
							Retryable          bool            `json:"retryable"`
							ErrorCode          string          `json:"error_code"`
							FailedDependencies []string        `json:"failed_dependencies"`
							Cost               string          `json:"cost"`
							Detail             json.RawMessage `json:"detail"`
						} `json:"results"`
					} `json:"detail"`
				}
				lines := strings.Split(strings.TrimSpace(string(out)), "\n")
				if len(lines) > 0 && json.Unmarshal([]byte(lines[len(lines)-1]), &response) == nil {
					// Items that failed inside the plan are not silently dropped: record the detail and requeue them as
					// independent fetch_item instances so the normal 3-attempt/backoff/one-alert rules apply.
					phaseNow, _ := r.currentPhase(r.clock.Now())
					for _, res := range response.Detail.Results {
						if res.Status != "failed" && res.Status != "DEPENDENCY_FAILED" {
							continue
						}
						_, _ = r.appendLedger(date, "APPLY_ITEM_FAILED", map[string]any{"item": res.ID, "status": res.Status, "error_code": res.ErrorCode, "retryable": res.Retryable, "detail": res.Detail})
						if res.ID == "" || res.Partition == "" || !res.Retryable {
							continue
						}
						instanceID := "fetch_item:" + res.ID
						r.mu.Lock()
						priorResult, exists := r.states[date+"/"+instanceID]
						r.mu.Unlock()
						if exists && priorResult.Status != "SUCCEEDED" && priorResult.Status != "SUCCEEDED_DEGRADED" {
							continue
						}
						r.setState(Instance{ID: instanceID, EventID: "fetch_item", Start: "00:00:00", Deadline: "23:59:59", TriggerType: "startup"}, date,
							instanceState{Status: "DEFERRED", LastError: "requeued after failed plan item " + res.ErrorCode,
								NextPhase: phaseNow, DataDate: res.Partition, Cost: res.Cost, WaitFor: fetchItemDependencies(res.FailedDependencies)})
					}
					for _, deferred := range response.Detail.Deferred {
						if deferred.ID == "" || len(deferred.RunInPhases) == 0 {
							continue
						}
						instanceID := "fetch_item:" + deferred.ID
						key := date + "/" + instanceID
						r.mu.Lock()
						prior, exists := r.states[key]
						r.mu.Unlock()
						// The plan lists an item only when its partition is missing in the catalog. A recorded success means the
						// data was removed or invalidated since (e.g. rebuilt after a partial input): the old success is stale,
						// so re-arm it with a fresh budget. Terminal failures stay for the operator (requeue).
						if exists && prior.Status != "SUCCEEDED" && prior.Status != "SUCCEEDED_DEGRADED" {
							continue
						}
						deferredInstance := Instance{ID: instanceID, EventID: "fetch_item", Start: "00:00:00", Deadline: "23:59:59", TriggerType: "startup"}
						wait := make([]string, 0, len(deferred.DependsOn))
						for _, dep := range deferred.DependsOn {
							wait = append(wait, "fetch_item:"+dep)
						}
						r.setState(deferredInstance, date, instanceState{Status: "DEFERRED", LastError: "waiting for " + deferred.RunInPhases[0], NextPhase: deferred.RunInPhases[0], DataDate: deferred.Partition, WaitFor: wait, Cost: deferred.Cost})
					}
				}
			}
			r.mu.Lock()
			wasBlocked := r.gateAlerted != nil && r.gateAlerted[event.Handler]
			if wasBlocked {
				r.gateAlerted[event.Handler] = false
			}
			delete(r.alertSeen, date+"/"+x.EventID+"/GATE_BLOCKED_FIRST")
			r.mu.Unlock()
			if wasBlocked {
				_, _ = r.appendLedger(date, "GATE_HANDLER_RECOVERED", map[string]any{"handler": event.Handler, "event": x.EventID})
			}
			return
		}
		last = code
		lastRetryable = retryable && !(code == "HANDLER_TIMEOUT" && event.Retry.NoRetryOnTimeout)
		_, _ = r.appendLedger(date, "FAILURE_OBSERVED", map[string]any{"event": x.EventID, "date": date, "attempt": n, "error_code": code, "retryable": lastRetryable, "detail": detail})
		if !lastRetryable || n == cycleEnd {
			break
		}
		if slotHeld {
			<-slots
			slotHeld = false
		}
		if !(event.Retry.UntilLate && event.LateUntil != "") {
			// config generators set "backoff" (60s) for data events; "base" is the legacy field. Using only Base made
			// fetch_item retry after 1s,2s (all three attempts inside seconds) instead of exponentially from 60s.
			base := event.Retry.Base
			if base == "" {
				base = event.Retry.Backoff
			}
			r.waitClock(eventRetryDelay(base, n))
		}
	}
	if lastRetryable && event.Retry.UntilLate && event.LateUntil != "" {
		if st.Attempts >= max {
			r.finishRetriesExhausted(x, date, event, st)
			return
		}
		cutoff, err := time.ParseInLocation("2006-01-02 15:04:05", date+" "+event.LateUntil, r.loc)
		if err == nil && r.clock.Now().Before(cutoff) {
			delay := retryBackoff(event.Retry.Backoff, st.Attempts, max)
			next := r.clock.Now().Add(delay)
			if next.After(cutoff) {
				next = cutoff
			}
			st.Status = "RETRY_WAIT"
			st.LastError = last
			st.CompletedAt = ""
			st.NextAttemptAt = next.Format(time.RFC3339)
			r.setState(x, date, st)
			return
		}
		if err == nil {
			r.finishLateRetries(x, date, event, st)
			return
		}
	}
	status := "FAILED_FINAL"
	if lastRetryable && retryableFailure(last) {
		status = "CIRCUIT_OPEN"
	}
	r.setState(x, date, instanceState{Status: status, Attempts: st.Attempts, StartedAt: st.StartedAt, CompletedAt: r.clock.Now().Format(time.RFC3339), LastError: last})
	if event.Trigger.Type != "startup" {
		_ = r.alert(status, map[string]any{"event": x.EventID, "date": date, "error": last})
	} else if x.EventID == "fetch_item" {
		// Queued data fetches are startup-type instances but are real work: a spent budget must reach a human once,
		// keyed by the item so different items are not collapsed into one alert.
		_ = r.alert(status, map[string]any{"event": x.ID, "date": date, "error": last})
	}
}

func (r *liveRunner) executeLadder(x Instance, date string, event *config.Event, state instanceState, handler config.Handler, deadlineDegrade bool) {
	if len(event.Ladder) == 0 {
		return
	}
	if state.Status == "SUCCEEDED" || state.Status == "SUCCEEDED_DEGRADED" || state.Status == "SKIPPED_NA" || state.Status == "SKIPPED_SATISFIED" {
		return
	}
	state.Status = "RUNNING"
	if state.StartedAt == "" {
		state.StartedAt = r.clock.Now().Format(time.RFC3339)
	}
	if state.DataDate == "" {
		state.DataDate = x.DataDate
	}
	skipped := append([]string(nil), state.SkippedTiers...)
	first := state.Rung - 1
	if first < 0 || first >= len(event.Ladder) {
		first = 0
		state.Rung = 0
		state.RungAttempts = 0
		state.Tier = ""
	}
	deadlineAt, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Deadline, r.loc)

	finishFailure := func(code, detail string, rung int, tier config.LadderTier) {
		state.Status = "FAILED_FINAL"
		state.LastError = code
		if detail != "" {
			state.LastError += ": " + detail
		}
		state.Rung = rung + 1
		state.Tier = tier.Name
		state.RungAttempts = 0
		state.SkippedTiers = append([]string(nil), skipped...)
		state.CompletedAt = r.clock.Now().Format(time.RFC3339)
		r.setState(x, date, state)
		_ = r.alert("FAILED_FINAL", map[string]any{"event": x.ID, "event_id": x.EventID, "date": date, "error": state.LastError, "tier": tier.Name})
	}

	for index := first; index < len(event.Ladder); index++ {
		tier := event.Ladder[index]
		rungNumber := index + 1
		if state.Rung != rungNumber {
			state.Rung, state.Tier, state.RungAttempts = rungNumber, tier.Name, 0
		}
		budgetEnabled := tier.BudgetEnabled == nil || *tier.BudgetEnabled
		if budgetEnabled && !deadlineAt.IsZero() {
			remaining := deadlineAt.Sub(r.clock.Now())
			timeout, err := time.ParseDuration(tier.Timeout)
			if err != nil || remaining <= timeout {
				skipped = append(skipped, tier.Name)
				state.Rung, state.Tier, state.RungAttempts = rungNumber, tier.Name, 0
				state.SkippedTiers = append([]string(nil), skipped...)
				state.LastError = "TIER_SKIPPED_TIME_BUDGET"
				_, _ = r.appendLedger(date, "TIER_SKIPPED", map[string]any{"instance": x.ID, "rung": rungNumber, "tier": tier.Name, "reason": state.LastError, "remaining_ms": remaining.Milliseconds(), "timeout": tier.Timeout})
				r.setState(x, date, state)
				continue
			}
		}
		if tier.Until != "" {
			cutoffText := tier.Until
			if cutoffText == "late_until" {
				cutoffText = event.LateUntil
			}
			if cutoff, err := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoffText, r.loc); err == nil && !r.clock.Now().Before(cutoff) {
				if state.RungAttempts == 0 {
					skipped = append(skipped, tier.Name)
				}
				state.Rung, state.Tier = rungNumber, tier.Name
				state.SkippedTiers = append([]string(nil), skipped...)
				state.LastError = "TIER_UNTIL_EXPIRED"
				_, _ = r.appendLedger(date, "TIER_SKIPPED", map[string]any{"instance": x.ID, "rung": rungNumber, "tier": tier.Name, "reason": state.LastError})
				r.setState(x, date, state)
				continue
			}
		}
		for state.RungAttempts < tier.Attempts {
			if tier.Until != "" {
				cutoffText := tier.Until
				if cutoffText == "late_until" {
					cutoffText = event.LateUntil
				}
				if cutoff, err := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoffText, r.loc); err == nil && !r.clock.Now().Before(cutoff) {
					break
				}
			}
			if budgetEnabled && !deadlineAt.IsZero() {
				remaining := deadlineAt.Sub(r.clock.Now())
				timeout, err := time.ParseDuration(tier.Timeout)
				if err != nil || remaining <= timeout {
					break
				}
			}
			state.RungAttempts++
			state.Attempts++
			state.Rung, state.Tier = rungNumber, tier.Name
			state.Status = "RUNNING"
			state.LastError = ""
			state.CompletedAt = ""
			state.SkippedTiers = append([]string(nil), skipped...)
			r.setState(x, date, state)

			effectiveEvent := *event
			effectiveEvent.Timeout = tier.Timeout
			effectiveEvent.Args = tier.Args
			if effectiveEvent.Args == nil {
				effectiveEvent.Args = event.Args
			}
			isDegraded := rungNumber > 1
			degrade := tier.DegradeRequested || deadlineDegrade || x.DegradeRequested
			_, _ = r.appendLedger(date, "TIER_ATTEMPT", map[string]any{"instance": x.ID, "rung": rungNumber, "tier": tier.Name, "rung_attempt": state.RungAttempts, "attempt": state.Attempts, "degraded": isDegraded, "degrade_requested": degrade})
			out, _, invokeErr := r.invokeDetailedTier(handler.Argv, &effectiveEvent, x, date, state.Attempts, false, degrade, rungNumber, tier.Name, isDegraded, skipped)
			status, code, retryable, detail := decodeResult(out, invokeErr)
			if status == "already_satisfied" || code == "ALREADY_SATISFIED" {
				state.Status = "SKIPPED_SATISFIED"
				state.LastError = "ALREADY_SATISFIED"
				state.CompletedAt = r.clock.Now().Format(time.RFC3339)
				state.SkippedTiers = append([]string(nil), skipped...)
				_, _ = r.appendLedger(date, "ALREADY_SATISFIED", map[string]any{"instance": x.ID, "event": x.EventID, "rung": rungNumber, "tier": tier.Name})
				r.setState(x, date, state)
				return
			}
			if status == "not_applicable" {
				state.Status = "SKIPPED_NA"
				state.LastError = code
				state.CompletedAt = r.clock.Now().Format(time.RFC3339)
				state.SkippedTiers = append([]string(nil), skipped...)
				r.setState(x, date, state)
				return
			}
			if status == "ok" || status == "degraded" {
				state.Status = "SUCCEEDED"
				if status == "degraded" || isDegraded || degrade {
					state.Status = "SUCCEEDED_DEGRADED"
				}
				state.LastError = ""
				state.CompletedAt = r.clock.Now().Format(time.RFC3339)
				state.SkippedTiers = append([]string(nil), skipped...)
				r.setState(x, date, state)
				return
			}
			if code == "TIER_NO_DATA" {
				_, _ = r.appendLedger(date, "TIER_FAILED", map[string]any{"instance": x.ID, "rung": rungNumber, "tier": tier.Name, "rung_attempt": state.RungAttempts, "attempt": state.Attempts, "error_code": code, "detail": detail, "advance": true})
				if index == len(event.Ladder)-1 {
					finishFailure(code, detail, index, tier)
					return
				}
				state.RungAttempts = 0
				break
			}
			_, _ = r.appendLedger(date, "TIER_FAILED", map[string]any{"instance": x.ID, "rung": rungNumber, "tier": tier.Name, "rung_attempt": state.RungAttempts, "attempt": state.Attempts, "error_code": code, "detail": detail, "retryable": retryable})
			if !retryable {
				finishFailure(code, detail, index, tier)
				return
			}
			if state.RungAttempts >= tier.Attempts {
				break
			}
			delay := config.BackoffDelay(tier.Backoff, state.RungAttempts, tier.BackoffCap)
			if tier.Until == "late_until" {
				if cutoff, err := time.ParseInLocation("2006-01-02 15:04:05", date+" "+event.LateUntil, r.loc); err == nil && r.clock.Now().Before(cutoff) {
					next := r.clock.Now().Add(delay)
					if next.After(cutoff) {
						next = cutoff
					}
					state.Status = "RETRY_WAIT"
					state.LastError = code
					state.CompletedAt = ""
					state.NextAttemptAt = next.Format(time.RFC3339)
					state.SkippedTiers = append([]string(nil), skipped...)
					_, _ = r.appendLedger(date, "TIER_RETRY_SCHEDULED", map[string]any{"instance": x.ID, "rung": rungNumber, "tier": tier.Name, "rung_attempt": state.RungAttempts, "next_attempt_at": state.NextAttemptAt, "until": cutoff.Format(time.RFC3339)})
					r.setState(x, date, state)
					return
				}
			}
			if delay > 0 {
				r.waitClock(delay)
			}
		}
		if state.RungAttempts >= tier.Attempts {
			_, _ = r.appendLedger(date, "TIER_EXHAUSTED", map[string]any{"instance": x.ID, "rung": rungNumber, "tier": tier.Name, "attempts": state.RungAttempts, "advance": index < len(event.Ladder)-1})
			if index == len(event.Ladder)-1 {
				finishFailure("TIER_ATTEMPTS_EXHAUSTED", "", index, tier)
				return
			}
			state.RungAttempts = 0
		}
	}
	lastIndex := len(event.Ladder) - 1
	if lastIndex >= 0 {
		tier := event.Ladder[lastIndex]
		finishFailure("NO_RUNGS_FIT_BUDGET", "all ladder tiers were skipped", lastIndex, tier)
	}
}

func retryBackoff(value string, attempts, maxAttempts int) time.Duration {
	base := time.Minute
	if d, err := time.ParseDuration(value); err == nil && d > 0 {
		base = d
	}
	return exponentialDelay(base, attempts, 10*time.Minute)
}

func eventRetryDelay(value string, attempts int) time.Duration {
	base := time.Second
	if d, err := time.ParseDuration(value); err == nil && d > 0 {
		base = d
	}
	return exponentialDelay(base, attempts, 5*time.Minute)
}

func exponentialDelay(base time.Duration, attempts int, cap time.Duration) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	for i := 1; i < attempts && base < cap; i++ {
		if base > cap/2 {
			return cap
		}
		base *= 2
	}
	if base > cap {
		return cap
	}
	return base
}

func (r *liveRunner) finishRetriesExhausted(x Instance, date string, event *config.Event, prior instanceState) {
	st := instanceState{Status: "FAILED_FINAL", Attempts: prior.Attempts, StartedAt: prior.StartedAt, CompletedAt: r.clock.Now().Format(time.RFC3339), LastError: "RETRIES_EXHAUSTED"}
	r.setState(x, date, st)
	if !event.Retry.SilentUntilLast {
		_ = r.alert("RETRIES_EXHAUSTED", map[string]any{"event": x.EventID, "date": date, "error": st.LastError})
	}
}

func (r *liveRunner) finishLateRetries(x Instance, date string, event *config.Event, prior instanceState) {
	st := instanceState{Status: "FAILED_FINAL", Attempts: prior.Attempts, StartedAt: prior.StartedAt, CompletedAt: r.clock.Now().Format(time.RFC3339), LastError: "RETRIES_EXHAUSTED_BY_LATE_UNTIL"}
	r.setState(x, date, st)
	if !event.Retry.SilentUntilLast {
		_ = r.alert("RETRIES_EXHAUSTED_BY_LATE_UNTIL", map[string]any{"event": x.EventID, "date": date, "error": st.LastError})
	}
}
func retryableFailure(code string) bool {
	return code == "HANDLER_TIMEOUT" || code == "INTERNAL_ERROR" || strings.Contains(code, "DEPENDENCY")
}
func (r *liveRunner) invoke(argv []string, e *config.Event, x Instance, date string, attempt int, gate bool) ([]byte, error) {
	out, _, err := r.invokeDetailed(argv, e, x, date, attempt, gate)
	return out, err
}

func (r *liveRunner) invokeDetailed(argv []string, e *config.Event, x Instance, date string, attempt int, gate bool) ([]byte, []byte, error) {
	return r.invokeDetailedWithDegrade(argv, e, x, date, attempt, gate, false)
}

func (r *liveRunner) invokeWithDegrade(argv []string, e *config.Event, x Instance, date string, attempt int, gate, degradeRequested bool) ([]byte, error) {
	out, _, err := r.invokeDetailedWithDegrade(argv, e, x, date, attempt, gate, degradeRequested)
	return out, err
}

func (r *liveRunner) invokeDetailedWithDegrade(argv []string, e *config.Event, x Instance, date string, attempt int, gate, degradeRequested bool) ([]byte, []byte, error) {
	return r.invokeDetailedTier(argv, e, x, date, attempt, gate, degradeRequested, 0, "", false, nil)
}

func (r *liveRunner) invokeDetailedTier(argv []string, e *config.Event, x Instance, date string, attempt int, gate, degradeRequested bool, rung int, tier string, degraded bool, skippedTiers []string) ([]byte, []byte, error) {
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	args := make([]string, len(argv))
	values := map[string]string{"trade_date": date, "data_date": date, "asof": date + "T" + x.Start + "+08:00", "event_id": x.EventID, "instance": x.ID, "attempt": fmt.Sprint(attempt), "run_id": r.boot + "-" + x.ID, "sandbox_root": "", "python": "python3"}
	for i, a := range argv {
		args[i] = runtimeTemplateRE.ReplaceAllStringFunc(a, func(m string) string {
			parts := runtimeTemplateRE.FindStringSubmatch(m)
			if len(parts) > 1 {
				return values[parts[1]]
			}
			return m
		})
	}
	ctx := context.Background()
	timeout := configDuration(e.Timeout)
	if timeout <= 0 {
		timeout = int64(60 * time.Second)
	}
	if gate {
		cutoff := e.Trigger.Deadline
		if e.LateUntil != "" && e.OnDeadline != "degrade" && e.OnDeadline != "missed" {
			cutoff = e.LateUntil
		}
		deadline, parseErr := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
		if parseErr == nil {
			remaining := deadline.Sub(r.clock.Now())
			if remaining <= 0 {
				return nil, nil, fmt.Errorf("GATE_DEADLINE_EXPIRED")
			}
			if int64(remaining) < timeout {
				timeout = int64(remaining)
			}
		}
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(timeout))
	defer cancel()
	cmd := exec.Command(args[0], args[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if h := c.Handlers[e.Handler]; h.Cwd != "" {
		cmd.Dir = h.Cwd
	}
	var out, stderr limitedBuffer
	limit := c.Handlers[e.Handler].MaxOutputBytes
	if limit <= 0 {
		limit = 1 << 20
	}
	out.limit = limit
	stderr.limit = limit
	cmd.Stdout = &out
	cmd.Stderr = &stderr
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "LANG=C.UTF-8"}
	for _, name := range c.Handlers[e.Handler].EnvAllow {
		if value, ok := os.LookupEnv(name); ok {
			cmd.Env = append(cmd.Env, name+"="+value)
		}
	}
	dataDate := x.DataDate
	if dataDate == "" {
		dataDate = date
	}
	in := map[string]any{"contract": 1, "event": e.ID, "instance": x.ID, "attempt": attempt, "run_id": r.boot + "-" + x.ID, "idem_key": date + "/" + x.ID, "trade_date": date, "data_date": dataDate, "asof": date + "T" + x.Start + "+08:00", "deadline_at": date + "T" + x.Deadline + "+08:00", "degrade_requested": degradeRequested && !gate, "tier": tier, "degraded": degraded, "skipped_tiers": skippedTiers, "sandbox": map[string]any{"on": false, "root": ""}, "args": e.Args, "gate": map[string]any{"ref": e.Gate.Ref, "params": e.Gate.Params, "probe": gate}}
	if e.ID == "fetch_item" {
		in["item_id"] = strings.TrimPrefix(x.ID, "fetch_item:")
	}
	phase, _ := r.currentPhase(r.clock.Now())
	in["phase"] = phase
	in["now"] = r.clock.Now().In(r.loc).Format(time.RFC3339)
	in["active_events"] = r.pendingOwnerEvents(date, r.clock.Now())
	b, _ := json.Marshal(in)
	cmd.Stdin = strings.NewReader(string(b) + "\n")
	err := cmd.Start()
	if err != nil {
		return nil, nil, err
	}
	if !gate {
		_, _ = r.appendLedger(date, "ATTEMPT_START", map[string]any{"instance": x.ID, "attempt": attempt, "rung": rung, "tier": tier, "pid": cmd.Process.Pid, "pgid": cmd.Process.Pid, "starttime": r.clock.Now().Format(time.RFC3339Nano)})
	}
	wait := make(chan error, 1)
	go func() { wait <- cmd.Wait() }()
	select {
	case err = <-wait:
		if !gate {
			_, _ = r.appendLedger(date, "ATTEMPT_END", map[string]any{"instance": x.ID, "attempt": attempt, "rung": rung, "tier": tier, "exit_code": exitCode(err), "exit_error": fmt.Sprint(err)})
		}
		return out.Bytes(), stderr.Bytes(), err
	case <-ctx.Done():
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		select {
		case <-wait:
		case <-time.After(1 * time.Second):
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			<-wait
		}
		// A child can exit on TERM while grandchildren keep the process group alive.
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if !gate {
			_, _ = r.appendLedger(date, "ATTEMPT_END", map[string]any{"instance": x.ID, "attempt": attempt, "rung": rung, "tier": tier, "status": "HANDLER_TIMEOUT"})
		}
		return out.Bytes(), stderr.Bytes(), fmt.Errorf("HANDLER_TIMEOUT")
	}
}

type limitedBuffer struct {
	b     []byte
	limit int
}

func (b *limitedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	if len(b.b)+n > b.limit {
		return 0, errors.New("handler output limit exceeded")
	}
	b.b = append(b.b, p...)
	return n, nil
}
func (b *limitedBuffer) Bytes() []byte { return b.b }
func decodeResult(out []byte, err error) (status, code string, retry bool, detail string) {
	var v struct {
		Contract  int             `json:"contract"`
		Status    string          `json:"status"`
		ErrorCode string          `json:"error_code"`
		Retryable bool            `json:"retryable"`
		Detail    json.RawMessage `json:"detail"`
	}
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	parsed := json.Unmarshal([]byte(lines[len(lines)-1]), &v) == nil && v.Contract == 1
	detailText := strings.TrimSpace(string(v.Detail))
	var asString string
	if json.Unmarshal(v.Detail, &asString) == nil {
		detailText = asString
	}
	if len(detailText) > 300 {
		detailText = detailText[:300]
	}
	if err != nil {
		if strings.Contains(err.Error(), "HANDLER_TIMEOUT") {
			return "failed", "HANDLER_TIMEOUT", true, ""
		}
		if parsed && v.ErrorCode == "TIER_NO_DATA" && exitCode(err) == 40 {
			return "failed", "TIER_NO_DATA", false, detailText
		}
		if parsed && v.ErrorCode == "ALREADY_SATISFIED" && exitCode(err) == 20 {
			return "already_satisfied", "ALREADY_SATISFIED", false, detailText
		}
		// A non-zero exit that still printed a contract-1 failure keeps the INTERNAL_ERROR class (retry rules key on it)
		// but the handler's own code and detail are recorded instead of being dropped.
		if parsed && v.Status == "failed" && v.ErrorCode != "" && v.Retryable {
			if len(detailText) > 0 {
				detailText = v.ErrorCode + ": " + detailText
			} else {
				detailText = v.ErrorCode
			}
			return "failed", "INTERNAL_ERROR", true, detailText
		}
		if ee, ok := err.(*exec.ExitError); ok && ee.ExitCode() == 64 {
			return "failed", "CONFIG_ERROR", false, ""
		}
		return "failed", "INTERNAL_ERROR", true, err.Error()
	}
	if !parsed {
		return "failed", "CONTRACT_INVALID", false, ""
	}
	return v.Status, v.ErrorCode, v.Retryable, detailText
}
func gateReady(b []byte) bool {
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(lines) == 0 {
		return false
	}
	var v map[string]any
	if json.Unmarshal([]byte(lines[len(lines)-1]), &v) != nil {
		return false
	}
	return v["ready"] == true || v["status"] == "ok" || v["status"] == "ready"
}
func gateReason(b []byte, err error) string {
	if err != nil {
		return err.Error()
	}
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(lines) > 0 {
		var v struct {
			Reason string `json:"reason"`
		}
		if json.Unmarshal([]byte(lines[len(lines)-1]), &v) == nil && v.Reason != "" {
			return v.Reason
		}
	}
	return "gate not ready"
}
func findEvent(c config.Config, id string) *config.Event {
	for i := range c.Events {
		if c.Events[i].ID == id {
			return &c.Events[i]
		}
	}
	return nil
}
func (r *liveRunner) alert(reason string, data any) error {
	date := r.clock.Now().In(r.loc).Format("2006-01-02")
	event := ""
	if m, ok := data.(map[string]any); ok {
		event, _ = m["event"].(string)
	}
	key := date + "/" + event + "/" + reason
	if m, ok := data.(map[string]any); ok && m["summary"] == true {
		b, _ := json.Marshal(alertSummaryIdentity(data))
		sum := sha256.Sum256(b)
		key += "/" + hex.EncodeToString(sum[:8])
	}
	r.mu.Lock()
	if r.alertSeen[key] {
		r.mu.Unlock()
		return nil
	}
	if r.alertInFlight == nil {
		r.alertInFlight = map[string]bool{}
	}
	if r.alertAttempts == nil {
		r.alertAttempts = map[string]int{}
	}
	if r.alertInFlight[key] {
		r.mu.Unlock()
		return nil
	}
	if r.alertAttempts[key] >= 3 {
		r.mu.Unlock()
		_ = r.appendCurrentLedger("ALERT_RETRY_EXHAUSTED", map[string]any{"key": key, "reason": reason, "request": data})
		return errors.New("alert delivery retry limit reached")
	}
	minute := r.clock.Now().Unix() / 60
	if r.alertDaily >= 30 || r.alertMinutes[minute] >= 3 {
		r.mu.Unlock()
		_ = r.appendCurrentLedger("ALERT_SUPPRESSED", map[string]any{"key": key, "reason": reason, "request": data})
		return nil
	}
	r.alertMinutes[minute]++
	r.alertDaily++
	r.alertAttempts[key]++
	attempt := r.alertAttempts[key]
	r.alertInFlight[key] = true
	r.mu.Unlock()
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	h, ok := c.Handlers["alert"]
	if !ok || len(h.Argv) == 0 {
		return r.finishAlert(key, reason, data, attempt, errors.New("alert handler argv empty"))
	}
	b, _ := json.Marshal(map[string]any{"contract": 1, "event": "alert", "reason": reason, "request": data})
	args := make([]string, len(h.Argv))
	for i, a := range h.Argv {
		args[i] = runtimeTemplateRE.ReplaceAllStringFunc(a, func(m string) string {
			p := runtimeTemplateRE.FindStringSubmatch(m)
			if len(p) > 1 {
				switch p[1] {
				case "trade_date":
					return date
				case "event_id":
					return event
				case "python":
					return "python3"
				}
			}
			return m
		})
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, args[0], args[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Stdin = strings.NewReader(string(b) + "\n")
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "LANG=C.UTF-8"}
	for _, name := range h.EnvAllow {
		if value, ok := os.LookupEnv(name); ok {
			cmd.Env = append(cmd.Env, name+"="+value)
		}
	}
	if h.Cwd != "" {
		cmd.Dir = h.Cwd
	}
	err := cmd.Run()
	if ctx.Err() != nil {
		err = ctx.Err()
	}
	return r.finishAlert(key, reason, data, attempt, err)
}

// alertSummaryIdentity keeps a summary's event/reason content in its key while
// excluding observation timestamps, which otherwise defeat same-content dedup.
func alertSummaryIdentity(data any) any {
	b, err := json.Marshal(data)
	if err != nil {
		return data
	}
	var value any
	if json.Unmarshal(b, &value) != nil {
		return data
	}
	var normalize func(any) any
	normalize = func(v any) any {
		switch x := v.(type) {
		case map[string]any:
			out := make(map[string]any, len(x))
			for k, item := range x {
				if k == "at" || k == "timestamp" || k == "created_at" {
					continue
				}
				normalized := normalize(item)
				if k == "events" || k == "instances" || k == "blocks" {
					if values, ok := normalized.([]any); ok {
						sort.Slice(values, func(i, j int) bool {
							left, _ := json.Marshal(values[i])
							right, _ := json.Marshal(values[j])
							return string(left) < string(right)
						})
					}
				}
				out[k] = normalized
			}
			return out
		case []any:
			out := make([]any, len(x))
			for i, item := range x {
				out[i] = normalize(item)
			}
			return out
		default:
			return v
		}
	}
	return normalize(value)
}

func (r *liveRunner) finishAlert(key, reason string, data any, attempt int, deliveryErr error) error {
	r.mu.Lock()
	delete(r.alertInFlight, key)
	if deliveryErr == nil {
		r.alertSeen[key] = true
	}
	r.mu.Unlock()
	typ := "ALERT_OUT"
	if deliveryErr != nil {
		typ = "ALERT_FAILED"
	}
	ledgerErr := r.appendCurrentLedger(typ, map[string]any{"key": key, "reason": reason, "request": data, "attempts": attempt, "error": fmt.Sprint(deliveryErr)})
	if deliveryErr != nil {
		return deliveryErr
	}
	return ledgerErr
}

func (r *liveRunner) loop() error {
	if !r.listening {
		if err := r.listen(); err != nil {
			return err
		}
	}
	defer os.Remove(filepath.Join(r.root, "guardiand.sock"))
	lastWall := r.clock.Now().In(r.loc)
	lastMono := time.Now()
	for {
		var fakeChanged <-chan struct{}
		if r.fake != nil {
			fakeChanged = r.fake.changedChan()
		}
		wall := r.clock.Now().In(r.loc)
		mono := time.Now()
		if r.fake == nil && (wall.Sub(lastWall)-mono.Sub(lastMono) > 5*time.Second || mono.Sub(lastMono)-wall.Sub(lastWall) > 5*time.Second) {
			_ = r.appendCurrentLedger("CLOCK_JUMP", map[string]any{"wall": wall.Format(time.RFC3339Nano)})
		}
		lastWall, lastMono = wall, mono
		if err := r.rollDate(wall); err != nil {
			return err
		}
		r.mu.Lock()
		r.tick++
		r.mu.Unlock()
		r.schedule(wall)
		// One summary per clock hour: a persistently blocked 3-minute family must not mail every cycle
		// (the first block per handler is already alerted separately).
		var blocks []gateBlockRecord
		r.mu.Lock()
		if hour := wall.Format("2006-01-02T15"); len(r.gateBlocked) > 0 && r.gateSummaryHour != hour {
			if r.gateSummaryHour != "" {
				blocks = append(blocks, r.gateBlocked...)
				r.gateBlocked = nil
			}
			r.gateSummaryHour = hour
		} else if len(r.gateBlocked) == 0 && r.gateSummaryHour == "" {
			r.gateSummaryHour = hour
		}
		r.mu.Unlock()
		if len(blocks) > 0 {
			ids := make([]string, 0, len(blocks))
			for _, block := range blocks {
				ids = append(ids, block.Event)
			}
			sort.Strings(ids)
			_ = r.alert("GATE_BLOCKED", map[string]any{"events": ids, "blocks": blocks, "count": len(ids), "summary": true})
		}
		_ = r.heartbeat(wall)
		if r.fake != nil {
			<-fakeChanged
			continue
		}
		next := r.clock.Now().Truncate(time.Second).Add(time.Second)
		delay := time.Until(next)
		if delay <= 0 {
			delay = time.Second
		}
		timer := time.NewTimer(delay)
		select {
		case <-timer.C:
		case <-r.done:
		}
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
	}
}
func (r *liveRunner) rollDate(wall time.Time) error {
	date := wall.In(r.loc).Format("2006-01-02")
	if date == r.start.In(r.loc).Format("2006-01-02") {
		return nil
	}
	r.ledgerMu.Lock()
	if r.ledger != nil {
		if err := r.ledger.close(); err != nil {
			r.ledgerMu.Unlock()
			return err
		}
	}
	l, err := openLedger(r.root, date)
	if err != nil {
		r.ledgerMu.Unlock()
		return err
	}
	l.now = r.clock.Now
	r.ledger = l
	r.ledgerMu.Unlock()
	r.start = wall
	r.mu.Lock()
	r.states = map[string]instanceState{}
	r.gateAlerted = map[string]bool{}
	for key := range r.alertSeen {
		if !strings.HasPrefix(key, date+"/") {
			delete(r.alertSeen, key)
		}
	}
	r.alertDaily = 0
	r.alertMinutes = map[int64]int{}
	r.mu.Unlock()
	return r.recover()
}
func (r *liveRunner) dependencyStatus(date, id string, c config.Config) (string, bool) {
	event := findEvent(c, id)
	r.mu.Lock()
	defer r.mu.Unlock()
	if event == nil {
		return "", false
	}
	if event.Trigger.Type != "startup" {
		state, ok := r.states[date+"/"+id]
		return state.Status, ok
	}
	if status := r.bootSummary[id]; status != "" {
		return status, true
	}
	prefix := date + "/" + id + "@boot"
	var selected instanceState
	found := false
	for key, state := range r.states {
		if strings.HasPrefix(key, prefix) && (!found || state.CompletedAt > selected.CompletedAt) {
			selected = state
			found = true
		}
	}
	return selected.Status, found
}

func (r *liveRunner) windowDependencyDecision(date string, event *config.Event, c config.Config) (pending []string, blocked string) {
	if event == nil {
		return nil, ""
	}
	for _, dep := range event.DependsOn {
		status, exists := r.dependencyStatus(date, dep.ID, c)
		if !exists || !isTerminal(status) {
			pending = append(pending, dep.ID)
			continue
		}
		if status == "SUCCEEDED" || status == "SUCCEEDED_DEGRADED" && dep.AcceptDegraded {
			continue
		}
		return nil, dep.ID
	}
	return pending, ""
}

func (r *liveRunner) reconcileWindowDependencies(xs []Instance, date string, c config.Config, now time.Time) {
	for pass := 0; pass < len(xs); pass++ {
		changed := false
		for _, x := range xs {
			event := findEvent(c, x.EventID)
			if event == nil || event.Trigger.Type != "window" || len(event.DependsOn) == 0 {
				continue
			}
			key := date + "/" + x.ID
			r.mu.Lock()
			state, exists := r.states[key]
			running := r.running[x.ID]
			r.mu.Unlock()
			if running || exists && isTerminal(state.Status) {
				continue
			}
			pending, blocked := r.windowDependencyDecision(date, event, c)
			if blocked != "" {
				if !exists || state.Status != "SKIPPED_DEP" {
					r.setState(x, date, instanceState{Status: "SKIPPED_DEP", LastError: "dependency failed: " + blocked, Attempts: state.Attempts, CompletedAt: now.Format(time.RFC3339)})
					changed = true
				}
				continue
			}
			if len(pending) > 0 {
				if !exists || state.Status != "DEFERRED" || strings.Join(state.WaitFor, "\x00") != strings.Join(pending, "\x00") {
					state.Status = "DEFERRED"
					state.LastError = "waiting for dependencies: " + strings.Join(pending, ",")
					state.CompletedAt = ""
					state.WaitFor = pending
					r.setState(x, date, state)
					changed = true
				}
			}
		}
		if !changed {
			return
		}
	}
}

func (r *liveRunner) schedule(now time.Time) {
	date := now.Format("2006-01-02")
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	xs, _, err := runtimeInstances(c, date)
	if err != nil {
		return
	}
	for _, x := range xs {
		if !strings.HasPrefix(x.Calendar, "EXCLUDED") {
			continue
		}
		key := date + "/" + x.ID
		r.mu.Lock()
		state, exists := r.states[key]
		r.mu.Unlock()
		if !exists || !isTerminal(state.Status) {
			r.setState(x, date, instanceState{Status: "SKIPPED_NA", LastError: "calendar excluded", CompletedAt: now.Format(time.RFC3339)})
		}
	}
	r.reconcileWindowDependencies(xs, date, c, now)
	r.mu.Lock()
	r.nextDue = ""
	r.mu.Unlock()
	var missedNow []string
	for _, x := range xs {
		event := findEvent(c, x.EventID)
		if event == nil || strings.HasPrefix(x.Calendar, "EXCLUDED") {
			continue
		}
		start, e1 := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Start, r.loc)
		deadlineAt, e3 := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Deadline, r.loc)
		cutoff := x.Deadline
		if x.LateUntil != "" {
			cutoff = x.LateUntil
		}
		if event.OnDeadline == "missed" {
			cutoff = x.Deadline
		}
		end, e2 := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
		if e1 != nil || e2 != nil || e3 != nil {
			continue
		}
		key := date + "/" + x.ID
		r.mu.Lock()
		st, exists := r.states[key]
		running := r.running[x.ID]
		paused := r.paused[x.EventID] || r.paused["*"]
		r.mu.Unlock()
		if exists && isTerminal(st.Status) {
			continue
		}
		if x.Catchup != "run_in_window" && pastScheduleSecond(now, start) && !running && st.Attempts == 0 && st.Status != "RUNNING" && st.Status != "LATE_RUN" {
			r.setState(x, date, instanceState{Status: "MISSED", CompletedAt: now.Format(time.RFC3339), LastError: "CATCHUP_SKIPPED"})
			missedNow = append(missedNow, x.ID)
			continue
		}
		pendingDeps, blockedDep := r.windowDependencyDecision(date, event, c)
		if blockedDep != "" {
			if !running {
				r.setState(x, date, instanceState{Status: "SKIPPED_DEP", LastError: "dependency failed: " + blockedDep, Attempts: st.Attempts, CompletedAt: now.Format(time.RFC3339)})
			}
			continue
		}
		if len(pendingDeps) > 0 {
			continue
		}
		phase, _ := r.currentPhase(now)
		if exists && st.Status == "DEFERRED" && windowExpired(now, end) {
			r.setState(x, date, instanceState{Status: "MISSED", CompletedAt: now.Format(time.RFC3339), LastError: "PHASE_DEFER_EXPIRED"})
			missedNow = append(missedNow, x.ID)
			continue
		}
		if event != nil && !phaseAllowedAt(event, phase, now, c, r.loc) {
			if event.OnPhaseMismatch == "defer" {
				if !exists || st.Status != "DEFERRED" || st.NextPhase != nextAllowedPhase(event, c.EffectivePhases(), phase) {
					st.Status = "DEFERRED"
					st.LastError = "phase " + phase + " is not allowed"
					st.CompletedAt = ""
					st.NextPhase = nextAllowedPhase(event, c.EffectivePhases(), phase)
					r.setState(x, date, st)
				}
				continue
			}
			if !exists {
				r.setState(x, date, instanceState{Status: "SKIPPED_PHASE", LastError: phase, CompletedAt: now.Format(time.RFC3339)})
			}
			continue
		}
		if event.OnDeadline == "missed" && !running && !now.Truncate(time.Second).Before(deadlineAt.Truncate(time.Second)) {
			r.setState(x, date, instanceState{Status: "MISSED", Attempts: st.Attempts, CompletedAt: now.Format(time.RFC3339), LastError: "ON_DEADLINE_MISSED"})
			missedNow = append(missedNow, x.ID)
			continue
		}
		degradeDue := event.OnDeadline == "degrade" && !now.Truncate(time.Second).Before(deadlineAt.Truncate(time.Second))
		if exists && st.Status == "RETRY_WAIT" && !now.Before(end) {
			if event := findEvent(c, x.EventID); event != nil {
				r.finishLateRetries(x, date, event, st)
			}
			continue
		}
		if exists && !running && windowExpired(now, end) && !isTerminal(st.Status) {
			r.setState(x, date, instanceState{Status: "MISSED", CompletedAt: now.Format(time.RFC3339), LastError: "SCHEDULING_WINDOW_EXPIRED"})
			missedNow = append(missedNow, x.ID)
			continue
		}
		if windowExpired(now, end) && !exists {
			r.setState(x, date, instanceState{Status: "MISSED", CompletedAt: now.Format(time.RFC3339)})
			missedNow = append(missedNow, x.ID)
			continue
		}
		retryDue := true
		if exists && st.Status == "RETRY_WAIT" && (!degradeDue || st.Late) {
			next, err := time.Parse(time.RFC3339, st.NextAttemptAt)
			retryDue = err != nil || !now.Before(next)
		}
		if !now.Before(start) && !windowExpired(now, end) && !running && !paused && retryDue {
			x.DegradeRequested = degradeDue
			go r.execute(x, date)
		}
		if start.After(now) {
			r.mu.Lock()
			if r.nextDue == "" || x.Start < r.nextDue {
				r.nextDue = x.Start
			}
			r.mu.Unlock()
		}
	}
	phase, _ := r.currentPhase(now)
	for _, event := range c.Events {
		if event.Trigger.Type != "startup" || event.OnPhaseMismatch != "defer" {
			continue
		}
		prefix := date + "/" + event.ID + "@boot"
		r.mu.Lock()
		var deferredID string
		for key, state := range r.states {
			if strings.HasPrefix(key, prefix) && state.Status == "DEFERRED" {
				deferredID = strings.TrimPrefix(key, date+"/")
				break
			}
		}
		running := r.running[deferredID]
		r.mu.Unlock()
		if deferredID == "" {
			continue
		}
		cutoff := event.LateUntil
		if cutoff == "" {
			cutoff = "23:59:59"
		}
		end, parseErr := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
		if parseErr == nil && !now.Before(end) {
			x := Instance{ID: deferredID, EventID: event.ID, Start: "00:00:00", Deadline: "23:59:59", LateUntil: event.LateUntil, TriggerType: "startup"}
			r.setState(x, date, instanceState{Status: "MISSED", CompletedAt: now.Format(time.RFC3339), LastError: "PHASE_DEFER_EXPIRED"})
			missedNow = append(missedNow, deferredID)
			continue
		}
		if phase != "NON_TRADING" && phase != "CALENDAR_UNKNOWN" && phaseAllowed(&event, phase) && !running {
			x := Instance{ID: deferredID, EventID: event.ID, Start: "00:00:00", Deadline: "23:59:59", LateUntil: event.LateUntil, Timeout: event.Timeout, MaxAttempts: event.Retry.MaxAttempts, TriggerType: "startup"}
			go r.execute(x, date)
		}
	}
	if event := findEvent(c, "fetch_item"); event != nil {
		prefix := date + "/fetch_item:"
		r.mu.Lock()
		pending := make([]struct {
			id string
			st instanceState
		}, 0)
		for key, state := range r.states {
			if strings.HasPrefix(key, prefix) && (state.Status == "DEFERRED" || state.Status == "DEFER_LOAD" || state.Status == "RETRY_WAIT" || state.Status == "QUEUED_HEAVY") {
				pending = append(pending, struct {
					id string
					st instanceState
				}{id: strings.TrimPrefix(key, date+"/"), st: state})
			}
		}
		r.mu.Unlock()
		for _, item := range pending {
			itemEvent := fetchItemEvent(event, item.st)
			// A queued item already passed its phase check once; it is re-dispatched in any allowed phase.
			if item.st.Cost != "light" && item.st.Status != "QUEUED_HEAVY" && item.st.NextPhase != "" && item.st.NextPhase != phase {
				continue
			}
			r.mu.Lock()
			running := r.running[item.id]
			paused := r.paused["fetch_item"] || r.paused["*"]
			r.mu.Unlock()
			if running || paused || phase == "NON_TRADING" || phase == "CALENDAR_UNKNOWN" {
				continue
			}
			// A fetch that derives from another queued fetch waits for it (no attempts burned on a missing input).
			waiting := false
			r.mu.Lock()
			for _, dep := range item.st.WaitFor {
				if depState, exists := r.states[date+"/"+dep]; exists && depState.Status != "SUCCEEDED" && depState.Status != "SUCCEEDED_DEGRADED" {
					waiting = true
					break
				}
			}
			r.mu.Unlock()
			if waiting {
				continue
			}
			if !phaseAllowedAt(itemEvent, phase, now, c, r.loc) {
				if item.st.Status == "QUEUED_HEAVY" || item.st.Status == "DEFERRED" || item.st.Status == "RETRY_WAIT" {
					st := item.st
					if st.Status == "QUEUED_HEAVY" {
						st.Status = "DEFERRED"
					}
					st.LastError = "phase " + phase + " is not allowed"
					st.NextPhase = nextAllowedPhase(itemEvent, c.EffectivePhases(), phase)
					r.setState(Instance{ID: item.id, EventID: "fetch_item", Start: "00:00:00", Deadline: "23:59:59", DataDate: item.st.DataDate, TriggerType: "startup"}, date, st)
				}
				continue
			}
			if item.st.Status == "RETRY_WAIT" {
				next, err := time.Parse(time.RFC3339, item.st.NextAttemptAt)
				if err == nil && now.Before(next) {
					continue
				}
			}
			x := Instance{ID: item.id, EventID: "fetch_item", Start: "00:00:00", Deadline: "23:59:59", Timeout: itemEvent.Timeout, DataDate: item.st.DataDate,
				MaxAttempts: event.Retry.MaxAttempts, TriggerType: "startup"}
			go r.execute(x, date)
		}
	}
	if len(missedNow) > 0 {
		_ = r.alert("MISSED", map[string]any{"date": date, "instances": missedNow, "count": len(missedNow), "summary": true})
	}
}
func (r *liveRunner) heartbeat(now time.Time) error {
	r.mu.Lock()
	running := []string{}
	for id, v := range r.running {
		if v {
			running = append(running, id)
		}
	}
	r.mu.Unlock()
	sort.Strings(running)
	r.mu.Lock()
	tick := r.tick
	next := r.nextDue
	r.mu.Unlock()
	r.configMu.RLock()
	version, hash := r.c.ConfigVersion, r.hash
	r.configMu.RUnlock()
	b, _ := json.Marshal(map[string]any{"tick_seq": tick, "last_tick_at": now.Format(time.RFC3339Nano), "next_due": next, "running": running, "config_version": version, "config_hash": hash, "build": "dev", "boot_id": r.boot})
	return atomicFile(filepath.Join(r.root, "heartbeat.json"), b, 0640)
}
func atomicFile(path string, b []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0750); err != nil {
		return err
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".tmp-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(mode); e == nil {
		_, e = f.Write(b)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e != nil {
		return e
	}
	if e = os.Rename(name, path); e != nil {
		return e
	}
	d, e := os.Open(filepath.Dir(path))
	if e == nil {
		e = d.Sync()
		_ = d.Close()
	}
	return e
}

func (r *liveRunner) listen() error {
	path := filepath.Join(r.root, "guardiand.sock")
	_ = os.Remove(path)
	ln, e := net.Listen("unix", path)
	if e != nil {
		return e
	}
	if e = os.Chmod(path, 0600); e != nil {
		ln.Close()
		return e
	}
	go func() {
		for {
			c, e := ln.Accept()
			if e != nil {
				return
			}
			go r.serve(c)
		}
	}()
	return nil
}
func (r *liveRunner) startSocket() error { return nil }
func (r *liveRunner) serve(c net.Conn) {
	defer c.Close()
	s := bufio.NewScanner(c)
	s.Buffer(make([]byte, 4096), 4<<20)
	w := bufio.NewWriter(c)
	for s.Scan() {
		var q map[string]any
		if json.Unmarshal(s.Bytes(), &q) != nil {
			b, _ := json.Marshal(map[string]any{"t": "resp", "ok": false, "error": map[string]any{"code": "BAD_REQUEST", "msg": "invalid JSON"}})
			_, _ = w.Write(append(b, '\n'))
			_ = w.Flush()
			continue
		}
		op, _ := q["op"].(string)
		id := q["id"]
		if q["t"] == "hello" {
			proto, _ := q["proto"].(float64)
			r.configMu.RLock()
			version := r.c.ConfigVersion
			r.configMu.RUnlock()
			response := map[string]any{"t": "hello_ok", "proto": 1, "build": "dev", "config_version": version, "mode": r.mode}
			if proto != 1 {
				response = map[string]any{"t": "error", "error": map[string]any{"code": "PROTO_VERSION"}}
			}
			b, _ := json.Marshal(response)
			_, _ = w.Write(append(b, '\n'))
			_ = w.Flush()
			continue
		}
		if q["t"] == "req" {
			op, _ = q["op"].(string)
		}
		var result any
		ok := true
		code := ""
		idem, _ := q["idem"].(string)
		if idem != "" {
			r.mu.Lock()
			if previous, exists := r.idempotency[idem]; exists {
				result = previous
				code = "IDEMPOTENT_REPLAY"
			}
			r.mu.Unlock()
		}
		if result != nil { /* return the original operation result below */
		} else {
			switch op {
			case "hello":
				r.configMu.RLock()
				version := r.c.ConfigVersion
				r.configMu.RUnlock()
				result = map[string]any{"proto": 1, "build": "dev", "config_version": version, "mode": r.mode}
			case "status":
				r.mu.Lock()
				phase, nextPhase := r.currentPhase(r.clock.Now())
				result = map[string]any{"tick_seq": r.tick, "running": r.running, "states": r.states, "next_due": r.nextDue, "phase": phase, "next_phase": nextPhase, "boot": r.bootSummary}
				r.mu.Unlock()
			case "list":
				date, _ := q["date"].(string)
				r.configMu.RLock()
				c := r.c
				r.configMu.RUnlock()
				xs, _, e := instances(c, date)
				if e != nil {
					ok = false
					code = "BAD_REQUEST"
				} else {
					result = xs
				}
			case "pause", "resume":
				a, _ := q["args"].(map[string]any)
				event, _ := a["event"].(string)
				r.configMu.RLock()
				c := r.c
				r.configMu.RUnlock()
				if event != "" && event != "*" && findEvent(c, event) == nil {
					ok = false
					code = "UNKNOWN_EVENT"
					break
				}
				r.mu.Lock()
				if event == "" {
					event = "*"
				}
				r.paused[event] = op == "pause"
				r.mu.Unlock()
				_ = r.appendCurrentLedger("CONTROL", map[string]any{"op": op, "event": event})
				result = map[string]any{"event": event, "paused": op == "pause"}
			case "clock.advance":
				if r.fake == nil {
					ok = false
					code = "NOT_ALLOWED_IN_MODE"
					break
				}
				a, _ := q["args"].(map[string]any)
				by, _ := a["by"].(string)
				if by == "" {
					if n, yes := a["seconds"].(float64); yes {
						by = fmt.Sprintf("%fs", n)
					}
				}
				d, err := time.ParseDuration(by)
				if err != nil {
					ok = false
					code = "BAD_REQUEST"
					break
				}
				now := r.fake.Advance(d)
				result = map[string]any{"now": now.In(r.loc).Format(time.RFC3339Nano)}
			case "skip":
				a, _ := q["args"].(map[string]any)
				event, _ := a["event"].(string)
				date, _ := a["date"].(string)
				reason, _ := a["reason"].(string)
				if date == "" {
					date = r.clock.Now().In(r.loc).Format("2006-01-02")
				}
				r.configMu.RLock()
				c := r.c
				r.configMu.RUnlock()
				xs, _, err := instances(c, date)
				if err != nil {
					ok = false
					code = "BAD_REQUEST"
					break
				}
				found := false
				for _, x := range xs {
					if x.EventID == event || x.ID == event {
						found = true
						r.setState(x, date, instanceState{Status: "SKIPPED", LastError: reason, CompletedAt: r.clock.Now().Format(time.RFC3339)})
					}
				}
				if !found {
					ok = false
					code = "UNKNOWN_EVENT"
				} else {
					result = map[string]any{"event": event, "status": "SKIPPED"}
				}
			case "requeue":
				// Operator re-arm of a fetch_item that ended terminal (CIRCUIT_OPEN/FAILED_FINAL/MISSED): back to DEFERRED
				// with a fresh attempt budget, same data date. Never touches non-fetch events or live/succeeded instances.
				a, _ := q["args"].(map[string]any)
				instance, _ := a["instance"].(string)
				date, _ := a["trade_date"].(string)
				if date == "" {
					date = r.clock.Now().In(r.loc).Format("2006-01-02")
				}
				r.mu.Lock()
				prior, exists := r.states[date+"/"+instance]
				r.mu.Unlock()
				terminal := prior.Status == "CIRCUIT_OPEN" || prior.Status == "FAILED_FINAL" || prior.Status == "MISSED"
				if !exists || !strings.HasPrefix(instance, "fetch_item:") || !terminal {
					ok = false
					code = "BAD_REQUEST"
					break
				}
				phaseNow, _ := r.currentPhase(r.clock.Now())
				r.setState(Instance{ID: instance, EventID: "fetch_item", Start: "00:00:00", Deadline: "23:59:59", DataDate: prior.DataDate, TriggerType: "startup"}, date,
					instanceState{Status: "DEFERRED", LastError: "requeued by operator", NextPhase: phaseNow, DataDate: prior.DataDate, WaitFor: prior.WaitFor})
				_, _ = r.appendLedger(date, "OPERATOR_REQUEUE", map[string]any{"instance": instance, "was": prior.Status})
				result = map[string]any{"instance": instance, "status": "DEFERRED"}
			case "trigger":
				a, _ := q["args"].(map[string]any)
				event, _ := a["event"].(string)
				date, _ := a["trade_date"].(string)
				if date == "" {
					date = r.clock.Now().In(r.loc).Format("2006-01-02")
				}
				r.configMu.RLock()
				c := r.c
				r.configMu.RUnlock()
				xs, _, err := instances(c, date)
				if err != nil {
					ok = false
					code = "BAD_REQUEST"
					break
				}
				found := false
				for _, x := range xs {
					if x.EventID == event || x.ID == event {
						found = true
						r.mu.Lock()
						existing, done := r.states[date+"/"+x.ID]
						r.mu.Unlock()
						if done && (existing.Status == "SUCCEEDED" || existing.Status == "SUCCEEDED_DEGRADED" || existing.Status == "MISSED" || existing.Status == "CIRCUIT_OPEN" || existing.Status == "FAILED_FINAL" || existing.Status == "SKIPPED") {
							result = map[string]any{"event": event, "instance": x.ID, "status": existing.Status}
						} else if err := triggerWindowAllowed(x, date, r.clock.Now().In(r.loc), r.loc); err != nil {
							ok = false
							code = "NOT_ALLOWED_IN_MODE"
						} else {
							go r.execute(x, date)
							result = map[string]any{"event": event, "instance": x.ID, "status": "QUEUED"}
						}
						break
					}
				}
				if !found {
					ok = false
					code = "UNKNOWN_EVENT"
				}
			case "reload":
				c, h, err := load(r.cfgPath)
				if err != nil {
					ok = false
					code = "CONFIG_INVALID"
					break
				}
				r.configMu.RLock()
				oldVersion, oldHash := r.c.ConfigVersion, r.hash
				r.configMu.RUnlock()
				if err = config.CheckAgainstApplied(oldVersion, oldHash, c.ConfigVersion, h); err != nil {
					ok = false
					code = "CONFIG_INVALID"
					break
				}
				r.configMu.Lock()
				r.c, r.hash = c, h
				r.configMu.Unlock()
				_ = r.appendCurrentLedger("CONFIG_APPLIED", map[string]any{"config_version": c.ConfigVersion, "config_hash": h})
				result = map[string]any{"config_version": c.ConfigVersion, "config_hash": h}
			case "config.validate":
				args, _ := q["args"].(map[string]any)
				doc, _ := args["doc"]
				b, _ := json.Marshal(doc)
				_, issues := config.Decode(b)
				result = map[string]any{"valid": len(issues) == 0, "issues": issues}
				if len(issues) > 0 {
					ok = false
					code = "CONFIG_INVALID"
				}
			default:
				ok = false
				code = "BAD_REQUEST"
			}
			if idem != "" && ok && result != nil {
				r.mu.Lock()
				r.idempotency[idem] = result
				r.mu.Unlock()
				_ = r.appendCurrentLedger("CONTROL", map[string]any{"idem": idem, "result": result})
			}
		}
		if code == "IDEMPOTENT_REPLAY" {
			ok = true
		}
		resp := map[string]any{"t": "resp", "id": id, "ok": ok}
		if ok {
			resp["result"] = result
		} else {
			resp["error"] = map[string]any{"code": code, "msg": errorMessage(code)}
		}
		b, _ := json.Marshal(resp)
		_, _ = w.Write(append(b, '\n'))
		_ = w.Flush()
	}
}

func errorMessage(code string) string {
	switch code {
	case "UNKNOWN_EVENT":
		return "no such event in the active config (or no instance on that date)"
	case "NOT_ALLOWED_IN_MODE":
		return "operation not allowed now (outside the event window, or not in fake-clock mode)"
	case "CONFIG_INVALID":
		return "configuration rejected; the previous config stays active"
	case "BAD_REQUEST":
		return "bad request: unknown operation or invalid arguments"
	default:
		return code
	}
}
