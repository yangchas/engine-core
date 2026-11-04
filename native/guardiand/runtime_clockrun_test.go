package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"path/filepath"
	"testing"
	"time"

	"guardiand/config"
)

// startFakeDaemon runs the scheduling loop of a fake-clock runner and returns a function that sends one control
// request and returns the decoded result.
func startFakeDaemon(t *testing.T, c config.Config, start time.Time, date string) (*liveRunner, func(op string, args map[string]any) map[string]any) {
	t.Helper()
	root := t.TempDir()
	fc := newFakeClock(start)
	r, closeRunner := newC545Runner(t, root, date, fc, c)
	t.Cleanup(closeRunner)
	r.listening = true // no socket file: requests come over a pipe
	r.mu.Lock()
	r.bootDone = true // no boot chain in this runner
	r.mu.Unlock()
	go r.loop()
	server, client := net.Pipe()
	t.Cleanup(func() { client.Close() })
	go r.serve(server)
	reader := bufio.NewReader(client)
	n := 0
	return r, func(op string, args map[string]any) map[string]any {
		t.Helper()
		n++
		b, _ := json.Marshal(map[string]any{"t": "req", "id": fmt.Sprint(n), "op": op, "args": args})
		if _, err := client.Write(append(b, '\n')); err != nil {
			t.Fatal(err)
		}
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		var response map[string]any
		if err = json.Unmarshal([]byte(line), &response); err != nil {
			t.Fatal(err)
		}
		return response
	}
}

// A 30-second window must be entered and its handler allowed to finish before the clock moves again. With the blind
// clock.advance a driver could move past the window while the instance's goroutine had not started yet, which made the
// event MISSED depending on timing.
func TestClockRunNeverSkipsAShortWindowRepeatedly(t *testing.T) {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	for i := 0; i < 12; i++ {
		date := "2026-10-20"
		start, _ := time.ParseInLocation("2006-01-02 15:04:05", date+" 09:25:15", loc)
		script := writeC545Script(t, filepath.Join(t.TempDir(), "ok.sh"), `printf '%s\n' '{"contract":1,"status":"ok"}'`)
		e := config.Event{ID: "short", Handler: "h", Trigger: config.Trigger{Type: "window", Start: "09:25:20", Deadline: "09:25:50"}, OnDeadline: "missed"}
		c := config.Config{Events: []config.Event{e}, Handlers: map[string]config.Handler{"h": {Argv: []string{script}}}}
		r, send := startFakeDaemon(t, c, start, date)
		got := send("clock.run", map[string]any{"until": date + "T09:26:00+08:00", "step": "1s", "idle_timeout": "30s"})
		result, _ := got["result"].(map[string]any)
		if got["ok"] != true || result["settled"] != true {
			t.Fatalf("clock.run=%v", got)
		}
		r.mu.Lock()
		state := r.states[date+"/short"]
		r.mu.Unlock()
		if state.Status != "SUCCEEDED" {
			t.Fatalf("run %d: short window ended %s (%s), want SUCCEEDED", i, state.Status, state.LastError)
		}
	}
}

func TestClockStepAndRunAreRefusedOutsideFakeClock(t *testing.T) {
	r := &liveRunner{c: config.Config{ConfigVersion: 1}, mode: "live", loc: time.UTC, clock: realClock{}, states: map[string]instanceState{}, running: map[string]bool{}}
	server, client := net.Pipe()
	defer client.Close()
	go r.serve(server)
	for _, op := range []string{"clock.step", "clock.run", "clock.settle"} {
		fmt.Fprintf(client, `{"t":"req","id":"x","op":"%s","args":{}}`+"\n", op)
		line, err := bufio.NewReader(client).ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		var response map[string]any
		_ = json.Unmarshal([]byte(line), &response)
		if response["ok"] == true {
			t.Fatalf("%s must not work outside fakeclock mode: %v", op, response)
		}
	}
}

