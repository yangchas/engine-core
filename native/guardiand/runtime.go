package main

import (
	"bufio"
	"context"
	"crypto/rand"
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
	"sort"
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
	Status      string `json:"status"`
	Attempts    int    `json:"attempts"`
	StartedAt   string `json:"started_at"`
	CompletedAt string `json:"completed_at"`
	LastError   string `json:"last_error"`
	Late        bool   `json:"late,omitempty"`
}
type runState struct {
	EventID   string        `json:"event_id"`
	Instance  string        `json:"instance"`
	TradeDate string        `json:"trade_date"`
	State     instanceState `json:"state"`
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
		start, e1 := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Start, now.Location())
		end, e2 := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Deadline, now.Location())
		if x.LateUntil != "" {
			end, e2 = time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.LateUntil, now.Location())
		}
		if e1 != nil || e2 != nil {
			continue
		}
		if !now.Before(end) {
			missed = append(missed, x)
		} else if !now.Before(start) {
			active = append(active, x)
		}
	}
	return
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
	if now.Before(start) || !now.Before(end) {
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
	alertSeen        map[string]bool
	alertMinutes     map[int64]int
	alertDaily       int
	gateBlocked      []string
	gateAlerted      map[string]bool
	idempotency      map[string]any
	cfgPath          string
}

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
	missed, active := startupClassify(c, now)
	if err = r.applyStartup(now, missed, active); err != nil {
		return err
	}
	if err = r.startSocket(); err != nil {
		return err
	}
	return r.loop()
}

func (r *liveRunner) applyStartup(now time.Time, missed, active []Instance) error {
	for _, x := range missed {
		if _, exists := r.states[now.Format("2006-01-02")+"/"+x.ID]; exists {
			continue
		}
		r.setState(x, now.Format("2006-01-02"), instanceState{Status: "MISSED", CompletedAt: now.Format(time.RFC3339)})
		r.missedStartup = append(r.missedStartup, x.ID)
	}
	if len(missed) > 0 {
		if len(r.missedStartup) > 0 {
			if err := r.alert("MISSED", map[string]any{"count": len(r.missedStartup), "instances": r.missedStartup, "summary": true}); err != nil {
				return err
			}
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
	case "SUCCEEDED", "SUCCEEDED_DEGRADED", "FAILED_FINAL", "CIRCUIT_OPEN", "MISSED", "GATE_BLOCKED", "SKIPPED", "SKIPPED_NA":
		return true
	}
	return false
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
		if rec.Type == "ALERT_OUT" {
			b, _ := json.Marshal(rec.Data)
			var v struct {
				Key string `json:"key"`
			}
			_ = json.Unmarshal(b, &v)
			if v.Key != "" {
				r.alertSeen[v.Key] = true
				r.alertDaily++
				if at, e := time.Parse(time.RFC3339Nano, rec.TS); e == nil {
					r.alertMinutes[at.Unix()/60]++
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
	r.mu.Lock()
	if r.running[x.ID] {
		r.mu.Unlock()
		return
	}
	r.running[x.ID] = true
	prior := r.states[date+"/"+x.ID]
	r.mu.Unlock()
	defer func() { r.mu.Lock(); delete(r.running, x.ID); r.mu.Unlock() }()
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	event := findEvent(c, x.EventID)
	if event == nil {
		return
	}
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
	slots := r.classSlots[class]
	if slots == nil {
		slots = make(chan struct{}, limit)
		r.classSlots[class] = slots
	}
	r.mu.Unlock()
	deadlineAt, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Deadline, r.loc)
	isLate := !r.clock.Now().Before(deadlineAt)
	st := instanceState{Status: "RUNNING", StartedAt: r.clock.Now().Format(time.RFC3339), Late: isLate}
	if isLate {
		st.Status = "LATE_RUN"
	}
	if prior.Status == "RUNNING" || prior.Status == "LATE_RUN" {
		st.Attempts = prior.Attempts
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
		if x.LateUntil != "" {
			cutoff = x.LateUntil
		}
		deadline, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
		ready := false
		lastGateReason := "gate not ready"
		for r.clock.Now().Before(deadline) {
			if len(h.GateArgv) == 0 {
				break
			}
			out, err := r.invoke(h.GateArgv, event, x, date, 1, true)
			if err == nil && gateReady(out) && r.clock.Now().Before(deadline) {
				ready = true
				break
			}
			lastGateReason = gateReason(out, err)
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
			r.setState(x, date, instanceState{Status: "GATE_BLOCKED", LastError: "gate not ready", CompletedAt: r.clock.Now().Format(time.RFC3339)})
			r.mu.Lock()
			r.gateBlocked = append(r.gateBlocked, x.EventID)
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
	// Gate polling must not hold a class slot, or a blocked gate starves every other event of the class.
	slots <- struct{}{}
	defer func() { <-slots }()
	cutoff := x.Deadline
	if x.LateUntil != "" {
		cutoff = x.LateUntil
	}
	startCutoff, parseErr := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
	if parseErr == nil && !r.clock.Now().Before(startCutoff) {
		r.setState(x, date, instanceState{Status: "MISSED", CompletedAt: r.clock.Now().Format(time.RFC3339), LastError: "START_DEADLINE_EXPIRED"})
		_ = r.alert("MISSED", map[string]any{"event": x.EventID, "date": date, "error": "START_DEADLINE_EXPIRED"})
		return
	}
	if !r.clock.Now().Before(deadlineAt) {
		st.Late = true
		st.Status = "LATE_RUN"
	}
	h := c.Handlers[event.Handler]
	if len(h.Argv) == 0 {
		r.setState(x, date, instanceState{Status: "FAILED_FINAL", Attempts: 1, LastError: "handler argv empty", CompletedAt: r.clock.Now().Format(time.RFC3339)})
		return
	}
	max := event.Retry.MaxAttempts
	if max < 1 {
		max = 1
	}
	var last string
	if st.Attempts >= max {
		r.setState(x, date, instanceState{Status: "FAILED_FINAL", Attempts: max, StartedAt: st.StartedAt, CompletedAt: r.clock.Now().Format(time.RFC3339), LastError: "RECOVERY_ATTEMPTS_EXHAUSTED"})
		_ = r.alert("FAILED_FINAL", map[string]any{"event": x.EventID, "date": date, "error": "RECOVERY_ATTEMPTS_EXHAUSTED"})
		return
	}
	for n := st.Attempts + 1; n <= max; n++ {
		st.Attempts = n
		st.Status = "RUNNING"
		if st.Late {
			st.Status = "LATE_RUN"
		}
		r.setState(x, date, st)
		out, err := r.invoke(h.Argv, event, x, date, n, false)
		status, code, retryable := decodeResult(out, err)
		if status == "not_applicable" {
			r.setState(x, date, instanceState{Status: "SKIPPED_NA", Attempts: n, StartedAt: st.StartedAt, CompletedAt: r.clock.Now().Format(time.RFC3339)})
			return
		}
		if status == "ok" || status == "degraded" {
			stateStatus := "SUCCEEDED"
			if status == "degraded" {
				stateStatus = "SUCCEEDED_DEGRADED"
			}
			r.setState(x, date, instanceState{Status: stateStatus, Attempts: n, StartedAt: st.StartedAt, CompletedAt: r.clock.Now().Format(time.RFC3339), Late: st.Late})
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
		if !retryable || n == max || (code == "HANDLER_TIMEOUT" && event.Retry.NoRetryOnTimeout) {
			break
		}
		delay := time.Duration(n) * time.Second
		if event.Retry.Base != "" {
			if d, e := time.ParseDuration(event.Retry.Base); e == nil {
				delay = d * time.Duration(n)
			}
		}
		r.waitClock(delay)
	}
	status := "FAILED_FINAL"
	if last == "HANDLER_TIMEOUT" && !event.Retry.NoRetryOnTimeout || retryableFailure(last) {
		status = "CIRCUIT_OPEN"
	}
	r.setState(x, date, instanceState{Status: status, Attempts: st.Attempts, StartedAt: st.StartedAt, CompletedAt: r.clock.Now().Format(time.RFC3339), LastError: last})
	_ = r.alert(status, map[string]any{"event": x.EventID, "date": date, "error": last})
}
func retryableFailure(code string) bool {
	return code == "HANDLER_TIMEOUT" || code == "INTERNAL_ERROR" || strings.Contains(code, "DEPENDENCY")
}
func (r *liveRunner) invoke(argv []string, e *config.Event, x Instance, date string, attempt int, gate bool) ([]byte, error) {
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
		if e.LateUntil != "" {
			cutoff = e.LateUntil
		}
		deadline, parseErr := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
		if parseErr == nil {
			remaining := deadline.Sub(r.clock.Now())
			if remaining <= 0 {
				return nil, fmt.Errorf("GATE_DEADLINE_EXPIRED")
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
	in := map[string]any{"contract": 1, "event": e.ID, "instance": x.ID, "attempt": attempt, "run_id": r.boot + "-" + x.ID, "idem_key": date + "/" + x.ID, "trade_date": date, "data_date": date, "asof": date + "T" + x.Start + "+08:00", "deadline_at": date + "T" + x.Deadline + "+08:00", "degrade_requested": false, "sandbox": map[string]any{"on": false, "root": ""}, "args": e.Args, "gate": map[string]any{"ref": e.Gate.Ref, "params": e.Gate.Params, "probe": gate}}
	b, _ := json.Marshal(in)
	cmd.Stdin = strings.NewReader(string(b) + "\n")
	err := cmd.Start()
	if err != nil {
		return nil, err
	}
	if !gate {
		_, _ = r.appendLedger(date, "ATTEMPT_START", map[string]any{"instance": x.ID, "attempt": attempt, "pid": cmd.Process.Pid, "pgid": cmd.Process.Pid, "starttime": r.clock.Now().Format(time.RFC3339Nano)})
	}
	wait := make(chan error, 1)
	go func() { wait <- cmd.Wait() }()
	select {
	case err = <-wait:
		if !gate {
			_, _ = r.appendLedger(date, "ATTEMPT_END", map[string]any{"instance": x.ID, "attempt": attempt, "exit_error": fmt.Sprint(err)})
		}
		return out.Bytes(), err
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
			_, _ = r.appendLedger(date, "ATTEMPT_END", map[string]any{"instance": x.ID, "attempt": attempt, "status": "HANDLER_TIMEOUT"})
		}
		return out.Bytes(), fmt.Errorf("HANDLER_TIMEOUT")
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
func decodeResult(out []byte, err error) (status, code string, retry bool) {
	if err != nil {
		if strings.Contains(err.Error(), "HANDLER_TIMEOUT") {
			return "failed", "HANDLER_TIMEOUT", true
		}
		if ee, ok := err.(*exec.ExitError); ok && ee.ExitCode() == 64 {
			return "failed", "CONFIG_ERROR", false
		}
		return "failed", "INTERNAL_ERROR", true
	}
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(lines) == 0 {
		return "failed", "CONTRACT_INVALID", false
	}
	var v struct {
		Contract  int    `json:"contract"`
		Status    string `json:"status"`
		ErrorCode string `json:"error_code"`
		Retryable bool   `json:"retryable"`
	}
	if json.Unmarshal([]byte(lines[len(lines)-1]), &v) != nil || v.Contract != 1 {
		return "failed", "CONTRACT_INVALID", false
	}
	return v.Status, v.ErrorCode, v.Retryable
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
	r.mu.Lock()
	if r.alertSeen[key] {
		r.mu.Unlock()
		return nil
	}
	r.alertSeen[key] = true
	minute := r.clock.Now().Unix() / 60
	if r.alertDaily >= 30 || r.alertMinutes[minute] >= 3 {
		r.mu.Unlock()
		_ = r.appendCurrentLedger("ALERT_SUPPRESSED", map[string]any{"key": key, "reason": reason, "request": data})
		return nil
	}
	r.alertDaily++
	r.alertMinutes[minute]++
	r.mu.Unlock()
	_ = r.appendCurrentLedger("ALERT_OUT", map[string]any{"key": key, "reason": reason, "request": data})
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	h, ok := c.Handlers["alert"]
	if !ok || len(h.Argv) == 0 {
		return nil
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
	cmd := exec.Command(args[0], args[1:]...)
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
	if err := cmd.Start(); err != nil {
		return err
	}
	go func() {
		wait := make(chan error, 1)
		go func() { wait <- cmd.Wait() }()
		timer := time.NewTimer(30 * time.Second)
		defer timer.Stop()
		select {
		case <-wait:
			return
		case <-timer.C:
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
			select {
			case <-wait:
			case <-time.After(time.Second):
				_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
				<-wait
			}
		}
	}()
	return nil // alert delivery is asynchronous; the ledger records the request before dispatch
}

func (r *liveRunner) loop() error {
	if err := r.listen(); err != nil {
		return err
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
		r.mu.Lock()
		ids := append([]string(nil), r.gateBlocked...)
		r.gateBlocked = nil
		r.mu.Unlock()
		if len(ids) > 0 {
			_ = r.alert("GATE_BLOCKED", map[string]any{"events": ids, "count": len(ids), "summary": true})
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
func (r *liveRunner) schedule(now time.Time) {
	date := now.Format("2006-01-02")
	r.configMu.RLock()
	c := r.c
	r.configMu.RUnlock()
	xs, _, err := instances(c, date)
	if err != nil {
		return
	}
	r.mu.Lock()
	r.nextDue = ""
	r.mu.Unlock()
	var missedNow []string
	for _, x := range xs {
		start, e1 := time.ParseInLocation("2006-01-02 15:04:05", date+" "+x.Start, r.loc)
		cutoff := x.Deadline
		if x.LateUntil != "" {
			cutoff = x.LateUntil
		}
		end, e2 := time.ParseInLocation("2006-01-02 15:04:05", date+" "+cutoff, r.loc)
		if e1 != nil || e2 != nil {
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
		if !now.Before(end) && !exists {
			r.setState(x, date, instanceState{Status: "MISSED", CompletedAt: now.Format(time.RFC3339)})
			missedNow = append(missedNow, x.ID)
			continue
		}
		if !now.Before(start) && now.Before(end) && !running && !paused {
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
				result = map[string]any{"tick_seq": r.tick, "running": r.running, "states": r.states, "next_due": r.nextDue}
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
