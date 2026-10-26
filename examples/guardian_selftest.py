#!/usr/bin/env python3
"""Run isolated, wall-clock guardian self-tests against a locally built binary.

Fast windows start 5--25 seconds ahead.  Retry delays retain the runtime's
eventRetryDelay formula (backoff * 2**(attempt-1), capped at five minutes),
except that the suite runs independent scenarios concurrently.  The minutes
mode moves the S01/S08/S15 start points to +2m/+3m/+5m and maps the production
schedule's 75m30s span onto six minutes.  DataDateOffset is observed as
ignored by the current runtime: both -1 and 0 use trade_date (neither
calendar-day nor trading-day offset is applied).

This tool only builds a private binary and writes below --work-dir.  It never
contacts services or reads the user's core-data tree.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
GO_ROOT = ROOT / "native" / "guardiand"
GO = Path("/bin/go")
HANDLER = ROOT / "examples" / "guardian_selftest_handler.py"
TZ = ZoneInfo("Asia/Shanghai")
TERMINAL = {
    "SUCCEEDED", "SUCCEEDED_DEGRADED", "FAILED_FINAL", "CIRCUIT_OPEN",
    "MISSED", "GATE_BLOCKED", "SKIPPED", "SKIPPED_NA", "SKIPPED_PHASE",
    "SKIPPED_DEP",
}
_ACTIVE_CASES: list["Scenario"] = []


def _cleanup_cases() -> None:
    for case in _ACTIVE_CASES:
        case.stop()


def _stop_on_signal(signum: int, _frame: Any) -> None:
    _cleanup_cases()
    raise SystemExit(128 + signum)


atexit.register(_cleanup_cases)
signal.signal(signal.SIGTERM, _stop_on_signal)
signal.signal(signal.SIGINT, _stop_on_signal)


def _iso_clock() -> datetime:
    return datetime.now(TZ)


def _clock(value: datetime) -> str:
    return value.strftime("%H:%M:%S")


def _seconds_clock(value: datetime) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _append(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        pass
    return rows


def _fmt(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else "-"


def _event(
    event_id: str,
    start: datetime,
    *,
    deadline: datetime | None = None,
    behavior: str = "ok",
    trigger_type: str = "window",
    order: int = 0,
    after: list[str] | None = None,
    args: dict[str, Any] | None = None,
    calendar: str = "every_day",
    phases: list[str] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": event_id,
        "enabled": True,
        "calendar": calendar,
        "handler": "selftest",
        "args": {"behavior": behavior, **(args or {})},
        "trigger": {"type": trigger_type},
        "retry": {"max_attempts": 1},
        "phases": phases if phases is not None else ["any"],
    }
    if trigger_type == "startup":
        event["trigger"].update({"order": order})
    else:
        event["trigger"].update({"start": _clock(start), "deadline": _clock(deadline or (start + timedelta(seconds=20)))})
    if after:
        event["trigger"]["after"] = after
    event.update(fields)
    return event


def _handler_config() -> dict[str, Any]:
    py = sys.executable
    handler = str(HANDLER)
    return {
        "selftest": {
            "argv": [py, handler, "run"],
            "gate_argv": [py, handler, "gate"],
            "cwd": str(ROOT),
            "env_allow": ["SELFTEST_DIR"],
            "supports_degrade": True,
            "max_output_bytes": 1048576,
        },
        "alert": {
            "argv": [py, handler, "alert"],
            "cwd": str(ROOT),
            "env_allow": ["SELFTEST_DIR"],
        },
    }


@dataclass
class Scenario:
    name: str
    events: list[dict[str, Any]]
    ids: list[str]
    expected: str
    scheduled: datetime | None
    timeout: float
    work: Path
    date: str
    special: str = ""
    actions: list[tuple[float, Path, str]] = field(default_factory=list)
    config_path: Path = field(init=False)
    state_root: Path = field(init=False)
    selftest_dir: Path = field(init=False)
    calendar_path: Path = field(init=False)
    log_path: Path = field(init=False)
    process: subprocess.Popen[bytes] | None = None
    log_stream: Any = None
    started_monotonic: float = 0.0
    restarted: bool = False
    stopped: bool = False
    fault: str = ""
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.config_path = self.work / "config.json"
        self.state_root = self.work / "state-root"
        self.selftest_dir = self.work / "selftest"
        self.calendar_path = self.work / "trade_dates.json"
        self.log_path = self.work / "guardiand.log"

    @property
    def ledger_path(self) -> Path:
        return self.state_root / "ledger" / f"{self.date}.jsonl"

    @property
    def call_path(self) -> Path:
        return self.selftest_dir / "calls.jsonl"

    def write_config(self, dates: list[str] | None = None) -> None:
        self.selftest_dir.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.calendar_path.write_text(json.dumps(dates if dates is not None else [self.date]) + "\n", encoding="utf-8")
        config = {
            "schema_version": 1,
            "config_version": 1,
            "name": f"guardian-selftest-{self.name}",
            "timezone": "Asia/Shanghai",
            "calendars": {"trading": {"file": str(self.calendar_path)}},
            "handlers": _handler_config(),
            "alerts": {"enabled": True},
            "events": self.events,
        }
        if self.fault == "start" and self.name == "S01 punctual trigger":
            self.events[0]["trigger"]["start"] = _clock(self.scheduled + timedelta(seconds=30))
        if self.fault == "backoff" and self.name == "S03 exponential retry":
            self.events[0]["retry"]["backoff"] = "30s"
        if self.fault == "catchup" and self.name == "S06 startup catchup":
            for event in self.events:
                if event["id"] == "catchup_run":
                    event["catchup"] = "skip"
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for event in self.events:
            gate = event.get("gate")
            if gate and gate.get("ref") == "always_ready":
                gate_dir = self.selftest_dir / "gates"
                gate_dir.mkdir(parents=True, exist_ok=True)
                (gate_dir / "always_ready").write_text("ready\n", encoding="utf-8")

    def ledger(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.ledger_path)

    def states(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for record in self.ledger():
            if record.get("type") != "INSTANCE_STATE":
                continue
            data = record.get("data") or {}
            state = data.get("state") or {}
            event_id = data.get("event_id")
            if event_id:
                result[str(event_id)] = state
        return result

    def calls(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.call_path)

    def calls_for(self, event_id: str) -> list[dict[str, Any]]:
        return [row for row in self.calls() if row.get("event") == event_id]

    def terminal(self) -> bool:
        states = self.states()
        return all(event_id in states and states[event_id].get("status") in TERMINAL for event_id in self.ids)

    def launch(self, append: bool = False) -> None:
        self.log_stream = self.log_path.open("ab" if append else "wb")
        safe_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "SELFTEST_DIR": str(self.selftest_dir),
            "GOTOOLCHAIN": "local",
            "GOPROXY": "off",
        }
        self.process = subprocess.Popen(
            [str(self.binary), "run", "--config", str(self.config_path), "--state-root", str(self.state_root)],
            cwd=str(self.work),
            env=safe_env,
            stdout=self.log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if not self.started_monotonic:
            self.started_monotonic = time.monotonic()

    def kill_active_handlers(self) -> None:
        starts: dict[tuple[str, int], int] = {}
        for rec in self.ledger():
            data = rec.get("data") or {}
            if rec.get("type") == "ATTEMPT_START":
                starts[(str(data.get("instance")), int(data.get("attempt", 0)))] = int(data.get("pgid", 0) or 0)
            elif rec.get("type") == "ATTEMPT_END":
                starts.pop((str(data.get("instance")), int(data.get("attempt", 0))), None)
        for pgid in set(starts.values()):
            if pgid > 1 and pgid not in {os.getpid(), os.getpgrp()}:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def stop(self, sig: int = signal.SIGTERM) -> None:
        proc = self.process
        if proc is not None and proc.poll() is None:
            try:
                if sig == signal.SIGKILL:
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.send_signal(sig)
                proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        self.kill_active_handlers()
        if self.log_stream:
            self.log_stream.close()
            self.log_stream = None
        self.stopped = True

    binary: Path = Path()


def _build_binary(work: Path) -> Path:
    if not GO.is_file():
        raise RuntimeError("/bin/go is missing; guardian self-test cannot build its isolated binary")
    if not (GO_ROOT / "go.mod").is_file():
        raise RuntimeError(f"{GO_ROOT}/go.mod is missing; guardian self-test cannot build its isolated binary")
    binary = work / "guardiand-selftest"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "GOTOOLCHAIN": "local",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOCACHE": str(work / "go-cache"),
    }
    result = subprocess.run([str(GO), "build", "-o", str(binary), "."], cwd=GO_ROOT, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"isolated /bin/go build failed ({result.returncode}):\n{result.stdout}{result.stderr}")
    return binary


def _make_scenarios(mode: str, minutes: int, base: Path, date: str, fault: str) -> list[Scenario]:
    now = _iso_clock()
    fast = mode == "fast"
    starts = {
        "S01": 5 if fast else 120,
        "S02": 6 if fast else 135,
        "S03": 7 if fast else 150,
        "S04": 8 if fast else 165,
        "S05": 9 if fast else 180,
        "S07": 10 if fast else 195,
        "S08": 11 if fast else 180,
        "S09": 12 if fast else 210,
        "S10": 13 if fast else 225,
        "S11": 14 if fast else 240,
        "S12": 15 if fast else 255,
        "S13": 16 if fast else 270,
        "S14": 17 if fast else 285,
    }

    def at(key: str) -> datetime:
        value = (now + timedelta(seconds=starts[key])).replace(microsecond=0)
        if value.date().isoformat() != date:
            raise RuntimeError("self-test start crossed local midnight; run it earlier in the day")
        return value

    def mk(name: str, suffix: str, events: list[dict[str, Any]], ids: list[str], expected: str, scheduled: datetime | None, timeout: float, special: str = "", actions: list[tuple[float, Path, str]] | None = None) -> Scenario:
        sc = Scenario(name, events, ids, expected, scheduled, timeout, base / suffix, date, special, actions or [], fault=fault)
        return sc

    # S01
    s = at("S01")
    e = _event("punctual", s, deadline=s + timedelta(seconds=20), args={"marker": "S01"})
    cases = [mk("S01 punctual trigger", "S01", [e], ["punctual"], "SUCCEEDED, call in [start,start+2s]", s, 32)]

    # S02: startup `order`/`after` and normal-window depends_on are asserted.
    s = at("S02")
    cases.append(mk("S02 order and dependencies", "S02", [
        _event("boot_fail", now, trigger_type="startup", order=10, behavior="permanent_fail"),
        _event("boot_after", now, trigger_type="startup", order=20, after=["boot_fail"]),
        _event("window_dep", s, deadline=s + timedelta(seconds=18), depends_on=[{"id": "boot_fail"}]),
    ], ["boot_fail", "boot_after", "window_dep"], "boot_after=SKIPPED_DEP; window_dep=SKIPPED_DEP", s, 32))

    # S03 exponential retry; fault mode stops after the second call for a quick
    # and unambiguous 30s-vs-2s differential.
    s = at("S03")
    cases.append(mk("S03 exponential retry", "S03", [
        _event("retry_twice", s, deadline=s + timedelta(seconds=24), behavior="fail:2", retry={"max_attempts": 3, "backoff": "2s"}),
    ], ["retry_twice"], "3 attempts; intervals 2s,4s ±1.5s; SUCCEEDED", s, 34, "early_backoff_probe" if fault == "backoff" else ""))

    # S04 retry exhaustion and the local alert sink.
    s = at("S04")
    cases.append(mk("S04 retry exhaustion", "S04", [
        _event("always_fail", s, deadline=s + timedelta(seconds=18), behavior="fail_always", retry={"max_attempts": 3, "backoff": "1s"}),
    ], ["always_fail"], "terminal retry failure; one local alert", s, 32))

    # S05 retries cycle through one attempt at a time until late_until.
    s = at("S05")
    late = s + timedelta(seconds=20 if fast else 35)
    cases.append(mk("S05 retry through late_until", "S05", [
        _event("late_retry", s, deadline=s + timedelta(seconds=18), behavior="fail_always",
               late_until=_clock(late), retry={"max_attempts": 20, "backoff": "3s", "until_late": True}),
    ], ["late_retry"], "FAILED_FINAL / RETRIES_EXHAUSTED_BY_LATE_UNTIL", s, (late - now).total_seconds() + 8))

    # S06 exercises late startup catchup values and an already-expired window.
    start_run = (now - timedelta(seconds=10)).replace(microsecond=0)
    start_skip = start_run
    missed_start = (now - timedelta(seconds=120)).replace(microsecond=0)
    cases.append(mk("S06 startup catchup", "S06", [
        _event("catchup_run", start_run, deadline=now + timedelta(seconds=60), catchup="run_in_window"),
        _event("catchup_skip", start_skip, deadline=now + timedelta(seconds=60), catchup="skip"),
        _event("already_missed", missed_start, deadline=now - timedelta(seconds=60)),
    ], ["catchup_run", "catchup_skip", "already_missed"], "run_in_window=1 call; skip=0; expired=MISSED", None, 16, "catchup_probe" if fault == "catchup" else ""))

    # S07 checks deadline degradation and the terminal missed action.
    s = at("S07")
    gate_event = _event("deadline_degrade", s, deadline=s + timedelta(seconds=8 if fast else 12), behavior="need_degrade",
                        on_deadline="degrade", timeout="2s", gate={"ref": "never_ready", "poll": "1s", "timeout": "1s"})
    cases.append(mk("S07 deadline degradation", "S07", [
        gate_event,
        _event("deadline_missed", s, deadline=s + timedelta(seconds=8 if fast else 12), on_deadline="missed",
               gate={"ref": "never_ready_2", "poll": "1s", "timeout": "1s"}),
    ], ["deadline_degrade", "deadline_missed"], "degrade call has degrade_requested=true; missed is MISSED", s, 32))

    # S08 gate becomes ready half way to its start.
    s = at("S08")
    gate_ready = _event("gate_mid_ready", s, deadline=s + timedelta(seconds=18), gate={"ref": "delayed_ready", "poll": "1s", "timeout": "1s"})
    gate_path = base / "S08" / "selftest" / "gates" / "delayed_ready"
    cases.append(mk("S08 gate becomes ready", "S08", [gate_ready], ["gate_mid_ready"], "call ≤ poll+1s after ready", s, 34,
                    actions=[((s - now).total_seconds() + 2.5, gate_path, "ready\n")]))

    # S09 timeout: each timed-out invocation is logged once, then retry occurs.
    s = at("S09")
    cases.append(mk("S09 handler timeout", "S09", [
        _event("timed_handler", s, deadline=s + timedelta(seconds=24), behavior="sleep:6", timeout="2s",
               retry={"max_attempts": 2, "backoff": "2s"}),
    ], ["timed_handler"], "2 calls, HANDLER_TIMEOUT records, terminal circuit state", s, 36))

    # S10 response mapping and non-retryable failure.
    s = at("S10")
    ids = ["map_ok", "map_degraded", "map_na", "map_permanent"]
    cases.append(mk("S10 status mapping", "S10", [
        _event(ids[0], s, deadline=s + timedelta(seconds=20), behavior="ok"),
        _event(ids[1], s, deadline=s + timedelta(seconds=20), behavior="degraded"),
        _event(ids[2], s, deadline=s + timedelta(seconds=20), behavior="not_applicable"),
        _event(ids[3], s, deadline=s + timedelta(seconds=20), behavior="permanent_fail"),
    ], ids, "SUCCEEDED / SUCCEEDED_DEGRADED / SKIPPED_NA / FAILED_FINAL (one attempt)", s, 34))

    # S11 excludes today's date from the calendar file. The test intentionally
    # uses phases=["any"] so calendar handling is tested without phase masking.
    s = at("S11")
    dates = [(now - timedelta(days=2)).date().isoformat(), (now + timedelta(days=2)).date().isoformat()]
    s11 = mk("S11 non-trading calendar", "S11", [
        _event("trading_only", s, deadline=s + timedelta(seconds=18), calendar="trading_day", phases=["any"]),
        _event("every_day", s, deadline=s + timedelta(seconds=18), calendar="every_day", phases=["any"]),
    ], ["trading_only", "every_day"], "trading_day=0 calls; every_day succeeds", s, 32)
    s11._calendar_dates = dates  # type: ignore[attr-defined]
    cases.append(s11)

    # S12 preserves args and observes data_date_offset directly from request.
    s = at("S12")
    cases.append(mk("S12 args and target date", "S12", [
        _event("offset_minus_one", s, deadline=s + timedelta(seconds=18), data_date_offset=-1, args={"marker": "passthrough", "nested": {"x": 1}}),
        _event("offset_zero", s, deadline=s + timedelta(seconds=18), data_date_offset=0),
    ], ["offset_minus_one", "offset_zero"], "args preserved; current runtime sends trade_date for both offsets", s, 32))

    # S13 boot sequence ordered before the timed window event.
    s = at("S13")
    cases.append(mk("S13 boot before window", "S13", [
        _event("boot_alpha", now, trigger_type="startup", order=10),
        _event("boot_beta", now, trigger_type="startup", order=20, after=["boot_alpha"]),
        _event("boot_gamma", now, trigger_type="startup", order=30, after=["boot_beta"]),
        _event("window_after_boot", s, deadline=s + timedelta(seconds=18)),
    ], ["boot_alpha", "boot_beta", "boot_gamma", "window_after_boot"], "boot_alpha < beta < gamma < window", s, 34))

    # S14 is restarted in the monitor loop after one completed and one active
    # event are observable.
    s = at("S14")
    cases.append(mk("S14 restart recovery", "S14", [
        _event("already_completed", s, deadline=s + timedelta(seconds=32), behavior="ok"),
        _event("resume_after_kill", s, deadline=s + timedelta(seconds=32), behavior="sleep:3", retry={"max_attempts": 3, "backoff": "1s"}),
    ], ["already_completed", "resume_after_kill"], "completed event once; interrupted event resumes", s, 42, "restart"))

    # S15 overlay. Source window is exactly 08:30:00..09:45:30 (4530s).
    schedule = json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text(encoding="utf-8"))
    production = schedule.get("events", schedule)
    chosen: list[dict[str, Any]] = []
    active = False
    for item in production:
        if item.get("event_id") == "startup_0830":
            active = True
        if active:
            chosen.append(item)
        if item.get("event_id") == "open_report_0945":
            break
    windows = [item for item in chosen if item.get("trigger_type") == "window"]
    if not windows:
        raise RuntimeError("schedule overlay found no window events between startup_0830 and open_report_0945")
    windows.sort(key=lambda item: item.get("start", ""))
    src_starts = [datetime.strptime(item["start"], "%H:%M:%S") for item in windows]
    src_min = min(src_starts)
    src_max = max(src_starts)
    span = max(1.0, (src_max - src_min).total_seconds())
    overlay_span = 45.0 if fast else 360.0
    overlay_base = (now + timedelta(seconds=5 if fast else 300)).replace(microsecond=0)
    mapped: list[datetime] = []
    last_sec = -1
    for src in src_starts:
        desired = int(round((src - src_min).total_seconds() / span * overlay_span))
        desired = max(desired, last_sec + 1)
        last_sec = desired
        mapped.append(overlay_base + timedelta(seconds=desired))
    overlay_events: list[dict[str, Any]] = []
    overlay_ids: list[str] = []
    for item, start in zip(windows, mapped):
        eid = item["event_id"]
        overlay_ids.append(eid)
        # The source ordering is time-compressed; each generated window keeps a
        # 12s execution allowance so scheduler tick jitter does not collapse a
        # valid event. All event-specific dependency/retry/deadline settings
        # are carried across below.
        deadline = start + timedelta(seconds=12 if fast else 40)
        retry: dict[str, Any] = {"max_attempts": item.get("max_attempts") or 1}
        if item.get("retry_backoff"):
            retry["backoff"] = item["retry_backoff"]
        if item.get("retry_until_late"):
            retry["until_late"] = True
        if item.get("silent_until_last"):
            retry["silent_until_last"] = True
        dependencies = item.get("depends_on") or []
        dependencies = [({"id": d} if isinstance(d, str) else d) for d in dependencies]
        event = _event(
            eid,
            start,
            deadline=deadline,
            args={"behavior": "ok", "source_handler": item.get("handler"), "source_start": item.get("start")},
            calendar="every_day",
            phases=["any"],
            retry=retry,
            timeout=f"{max(1, int(item.get('timeout_seconds') or 10))}s",
            data_date_offset=item.get("data_date_offset", 0),
            late_until=_clock(deadline + timedelta(seconds=30)) if item.get("late_until") else "",
            on_deadline=item.get("on_deadline") or "",
            catchup=item.get("catchup_mode") or "",
            depends_on=dependencies,
            priority=item.get("priority") or 0,
            cost=item.get("cost") or "",
        )
        after = item.get("after") or []
        if after:
            event["trigger"]["after"] = after
        gate = item.get("gate")
        if gate and gate != "always":
            event["gate"] = {"ref": "always_ready", "poll": "1s", "timeout": "1s"}
        overlay_events.append(event)
    cases.append(mk("S15 production schedule overlay", "S15", overlay_events, overlay_ids,
                    f"{len(overlay_ids)} events once, in original time order, within {overlay_span:.0f}s mapping", overlay_base,
                    (mapped[-1] - now).total_seconds() + 22, "overlay"))

    for case in cases:
        case.binary = base / "guardiand-selftest"
        case.write_config(getattr(case, "_calendar_dates", None))
    return cases


def _evaluate(case: Scenario) -> list[str]:
    states = case.states()
    calls = case.calls()
    errors: list[str] = []
    status = lambda event_id: (states.get(event_id) or {}).get("status", "MISSING")
    if case.name.startswith("S01"):
        rows = case.calls_for("punctual")
        if status("punctual") != "SUCCEEDED" or len(rows) != 1:
            errors.append(f"punctual status/calls={status('punctual')}/{len(rows)}")
        elif case.scheduled:
            actual = datetime.fromisoformat(rows[0]["wall_clock"])
            delta = (actual - case.scheduled).total_seconds()
            if not 0 <= delta <= 2:
                errors.append(f"trigger deviation {delta:.3f}s outside [0,2]")
    elif case.name.startswith("S02"):
        if status("boot_after") != "SKIPPED_DEP":
            errors.append(f"after dependency status={status('boot_after')}")
        if status("window_dep") != "SKIPPED_DEP":
            errors.append(f"window depends_on status={status('window_dep')}")
        seq = {row.get("event"): _parse_call_time(row.get("wall_clock")) for row in calls}
        if seq.get("boot_fail") and seq.get("boot_after") and seq["boot_fail"] > seq["boot_after"]:
            errors.append("startup order/after was reversed")
    elif case.name.startswith("S03"):
        rows = case.calls_for("retry_twice")
        if len(rows) != 3 or status("retry_twice") != "SUCCEEDED":
            errors.append(f"calls/status={len(rows)}/{status('retry_twice')}")
        else:
            times = [_parse_call_time(row.get("wall_clock")) for row in rows]
            gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
            if any(abs(got - want) > 1.5 for got, want in zip(gaps, (2, 4))):
                errors.append(f"retry intervals={','.join(f'{x:.3f}' for x in gaps)}s, expected 2s,4s ±1.5s")
    elif case.name.startswith("S04"):
        rows = case.calls_for("always_fail")
        alerts = _read_jsonl(case.selftest_dir / "alerts.jsonl")
        if len(rows) != 3 or status("always_fail") not in {"CIRCUIT_OPEN", "FAILED_FINAL"}:
            errors.append(f"calls/status={len(rows)}/{status('always_fail')}")
        if len(alerts) != 1:
            errors.append(f"alert count={len(alerts)}, expected 1")
    elif case.name.startswith("S05"):
        rows = case.calls_for("late_retry")
        state = states.get("late_retry", {})
        if state.get("status") != "FAILED_FINAL" or state.get("last_error") != "RETRIES_EXHAUSTED_BY_LATE_UNTIL":
            errors.append(f"status/error={state.get('status')}/{state.get('last_error')}")
        if len(rows) < 3:
            errors.append(f"attempt count={len(rows)}, expected retries through late_until")
        if len(rows) > 1:
            times = [_parse_call_time(row.get("wall_clock")) for row in rows]
            gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
            if any(abs(got - want) > 1.5 for got, want in zip(gaps, (3, 6, 12))):
                errors.append(f"retry intervals={gaps}; expected exponential 3s base")
    elif case.name.startswith("S06"):
        if len(case.calls_for("catchup_run")) != 1:
            errors.append(f"run_in_window calls={len(case.calls_for('catchup_run'))}")
        if len(case.calls_for("catchup_skip")) != 0:
            errors.append(f"catchup skip calls={len(case.calls_for('catchup_skip'))}")
        if status("already_missed") != "MISSED" or case.calls_for("already_missed"):
            errors.append(f"expired status/calls={status('already_missed')}/{len(case.calls_for('already_missed'))}")
    elif case.name.startswith("S07"):
        rows = case.calls_for("deadline_degrade")
        if not rows or not rows[-1].get("degrade_requested") or status("deadline_degrade") != "SUCCEEDED_DEGRADED":
            errors.append(f"degrade calls/status={len(rows)}/{status('deadline_degrade')}; runtime may not set degrade_requested")
        if case.calls_for("deadline_missed") or status("deadline_missed") != "MISSED":
            errors.append(f"missed calls/status={len(case.calls_for('deadline_missed'))}/{status('deadline_missed')}")
    elif case.name.startswith("S08"):
        rows = case.calls_for("gate_mid_ready")
        if not rows or status("gate_mid_ready") != "SUCCEEDED":
            errors.append(f"gate ready calls/status={len(rows)}/{status('gate_mid_ready')}")
        else:
            # Compare using wall clock because call rows carry the timestamp.
            ready_file = next((path for _, path, _ in case.actions), None)
            try:
                ready_mtime = datetime.fromtimestamp(ready_file.stat().st_mtime, TZ) if ready_file else None
                actual = _parse_call_time(rows[0].get("wall_clock"))
                if ready_mtime and (actual - ready_mtime).total_seconds() > 2:
                    errors.append("handler started more than gate.poll+1s after gate became ready")
            except OSError:
                errors.append("delayed ready gate file missing")
    elif case.name.startswith("S09"):
        rows = case.calls_for("timed_handler")
        timeouts = [r for r in case.ledger() if r.get("type") == "ATTEMPT_END" and (r.get("data") or {}).get("status") == "HANDLER_TIMEOUT"]
        attempts = [int(row.get("attempt", 0)) for row in rows]
        if attempts != [1, 2] or len(timeouts) != 2 or status("timed_handler") != "CIRCUIT_OPEN":
            errors.append(f"attempts/timeouts/status={attempts}/{len(timeouts)}/{status('timed_handler')}")
    elif case.name.startswith("S10"):
        expected = {"map_ok": "SUCCEEDED", "map_degraded": "SUCCEEDED_DEGRADED", "map_na": "SKIPPED_NA", "map_permanent": "FAILED_FINAL"}
        for event_id, wanted in expected.items():
            if status(event_id) != wanted:
                errors.append(f"{event_id}={status(event_id)}, expected {wanted}")
        if len(case.calls_for("map_permanent")) != 1:
            errors.append(f"permanent failure attempts={len(case.calls_for('map_permanent'))}")
    elif case.name.startswith("S11"):
        if case.calls_for("trading_only") or status("trading_only") != "SKIPPED_NA":
            errors.append(f"trading_only calls/status={len(case.calls_for('trading_only'))}/{status('trading_only')}")
        if len(case.calls_for("every_day")) != 1 or status("every_day") != "SUCCEEDED":
            errors.append(f"every_day calls/status={len(case.calls_for('every_day'))}/{status('every_day')}")
    elif case.name.startswith("S12"):
        for event_id in ("offset_minus_one", "offset_zero"):
            rows = case.calls_for(event_id)
            if len(rows) != 1:
                errors.append(f"{event_id} calls={len(rows)}")
            elif rows[0].get("data_date") != rows[0].get("trade_date"):
                errors.append(f"{event_id} data_date={rows[0].get('data_date')} != trade_date={rows[0].get('trade_date')}")
        row = case.calls_for("offset_minus_one")
        if row and row[0].get("args", {}).get("nested", {}).get("x") != 1:
            errors.append("args were not preserved")
    elif case.name.startswith("S13"):
        order = [row.get("event") for row in calls]
        wanted = ["boot_alpha", "boot_beta", "boot_gamma", "window_after_boot"]
        positions = [order.index(name) for name in wanted if name in order]
        if len(positions) != 4 or positions != sorted(positions):
            errors.append(f"call order={order}")
    elif case.name.startswith("S14"):
        completed = case.calls_for("already_completed")
        resumed = case.calls_for("resume_after_kill")
        if len(completed) != 1:
            errors.append(f"completed call count={len(completed)}")
        if [int(row.get("attempt", 0)) for row in resumed] != [1, 2]:
            errors.append(f"resumed attempts={[row.get('attempt') for row in resumed]}")
        if status("resume_after_kill") != "SUCCEEDED":
            errors.append(f"resume status={status('resume_after_kill')}")
    elif case.name.startswith("S15"):
        rows_by_event = {event_id: case.calls_for(event_id) for event_id in case.ids}
        missing = [event_id for event_id, rows in rows_by_event.items() if len(rows) != 1]
        if missing:
            errors.append(f"missing/duplicate calls={missing}")
        call_order = [row.get("event") for row in sorted(calls, key=lambda r: _parse_call_time(r.get("wall_clock")))]
        expected_order = [event["id"] for event in case.events]
        if call_order != expected_order:
            errors.append("calls do not preserve source schedule order")
        if any(status(event_id) != "SUCCEEDED" for event_id in case.ids):
            errors.append("one or more overlay events did not succeed")
    return errors


def _parse_call_time(value: Any) -> datetime:
    if not isinstance(value, str):
        return datetime.fromtimestamp(0, TZ)
    return datetime.fromisoformat(value)


def _actual(case: Scenario) -> tuple[str, str, str]:
    states = case.states()
    status = ",".join(f"{event_id}:{(states.get(event_id) or {}).get('status', 'MISSING')}" for event_id in case.ids)
    calls = case.calls()
    window_ids = {event["id"] for event in case.events if event.get("trigger", {}).get("type") != "startup"}
    scheduled_calls = [row for row in calls if row.get("event") in window_ids]
    if scheduled_calls:
        calls = scheduled_calls
    actual_dt = min((_parse_call_time(row.get("wall_clock")) for row in calls), default=None)
    if actual_dt and case.scheduled:
        delta = (actual_dt - case.scheduled).total_seconds()
        return _fmt(actual_dt), f"{delta:+.3f}", status
    return _fmt(actual_dt), "-", status


# Scenarios whose expectations the live Go runtime does not meet. Keep empty
# unless a verified runtime gap is intentionally accepted for this suite.
KNOWN_GAPS: dict[str, str] = {}


def _apply_known_gaps(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        reason = KNOWN_GAPS.get(row["name"].split()[0])
        if not reason:
            continue
        if row["passed"]:
            row["passed"] = False
            row["known_gap"] = "closed"
            row.setdefault("reasons", []).append(f"known gap closed ({reason}); remove it from KNOWN_GAPS")
        else:
            row["passed"] = True
            row["known_gap"] = reason


def _table(rows: list[dict[str, Any]]) -> str:
    lines = ["场景 | 预定触发时刻 | 实际触发时刻 | 偏差秒 | 期望状态 | 实际状态 | 结果"]
    lines.append("---|---|---|---:|---|---|---")
    for row in rows:
        lines.append(f"{row['name']} | {row['scheduled']} | {row['actual']} | {row['deviation']} | {row['expected']} | {row['observed']} | {'KNOWN_GAP: ' + row['known_gap'] if row.get('known_gap') and row['passed'] else 'PASS' if row['passed'] else 'FAIL'}")
    return "\n".join(lines)


def _run_cases(cases: list[Scenario], binary: Path, deadline: float) -> list[dict[str, Any]]:
    for case in cases:
        case.binary = binary
        case.launch()
    launched = time.monotonic()
    while time.monotonic() - launched < deadline:
        now_mono = time.monotonic()
        for case in cases:
            if case.result is not None:
                continue
            age = now_mono - case.started_monotonic
            for offset, path, content in case.actions:
                if age >= offset and not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
            if case.special == "restart" and not case.restarted:
                if len(case.calls_for("already_completed")) == 1 and len(case.calls_for("resume_after_kill")) >= 1:
                    case.stop(signal.SIGKILL)
                    case.restarted = True
                    case.stopped = False
                    case.launch(append=True)
            if case.special == "early_backoff_probe" and len(case.calls_for("retry_twice")) >= 2:
                rows = case.calls_for("retry_twice")
                gap = (_parse_call_time(rows[1].get("wall_clock")) - _parse_call_time(rows[0].get("wall_clock"))).total_seconds()
                if gap > 3.5:
                    case.stop()
                    case.result = {"name": case.name, "scheduled": _fmt(case.scheduled), "actual": _actual(case)[0], "deviation": _actual(case)[1], "expected": case.expected, "observed": _actual(case)[2], "passed": False, "reasons": [f"injected 30s backoff observed {gap:.3f}s interval"]}
                    continue
            if case.terminal():
                # S04 alert delivery can complete after its terminal state write.
                if case.name.startswith("S04") and len(_read_jsonl(case.selftest_dir / "alerts.jsonl")) == 0:
                    continue
                if case.special == "restart" and not case.restarted:
                    continue
                errors = _evaluate(case)
                actual, deviation, observed = _actual(case)
                case.stop()
                case.result = {"name": case.name, "scheduled": _fmt(case.scheduled), "actual": actual, "deviation": deviation,
                               "expected": case.expected, "observed": observed, "passed": not errors, "reasons": errors}
            elif case.process and case.process.poll() is not None:
                errors = [f"guardiand exited early with {case.process.returncode}"]
                actual, deviation, observed = _actual(case)
                case.stop()
                case.result = {"name": case.name, "scheduled": _fmt(case.scheduled), "actual": actual, "deviation": deviation,
                               "expected": case.expected, "observed": observed, "passed": False, "reasons": errors}
        if all(case.result is not None for case in cases):
            break
        time.sleep(0.1)
    for case in cases:
        if case.result is None:
            actual, deviation, observed = _actual(case)
            case.stop()
            case.result = {"name": case.name, "scheduled": _fmt(case.scheduled), "actual": actual, "deviation": deviation,
                           "expected": case.expected, "observed": observed, "passed": False, "reasons": ["scenario deadline expired"]}
    return [case.result for case in cases if case.result is not None]


def _print_rows(rows: list[dict[str, Any]]) -> None:
    print(_table(rows), flush=True)
    for row in rows:
        for reason in row.get("reasons", []):
            print(f"{row['name']}: FAIL_DETAIL: {reason}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fast", "minutes"), default="fast")
    parser.add_argument("--minutes", type=int, default=3)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--fault", choices=("start", "backoff", "catchup"))
    args = parser.parse_args(argv)
    if args.mode == "minutes" and args.minutes < 3:
        parser.error("--mode minutes requires --minutes >= 3")
    if args.mode == "minutes" and args.minutes != 3:
        print("note: --minutes is accepted for CLI compatibility; C544 timing uses fixed +2m/+3m/+5m anchors", flush=True)
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="guardian-selftest-"))
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    run_dir = work / f"r{os.getpid()}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir()
    try:
        binary = _build_binary(run_dir)
        date = _iso_clock().date().isoformat()
        cases = _make_scenarios(args.mode, args.minutes, run_dir, date, args.fault or "")
        if args.fault:
            target = {"start": "S01", "backoff": "S03", "catchup": "S06"}[args.fault]
            cases = [case for case in cases if case.name.startswith(target)]
        _ACTIVE_CASES[:] = cases
        # A normal fast pass is limited to 110s, leaving margin under the card's
        # 120s target. Minutes mode allows the six-minute overlay plus margin.
        budget = 105.0 if args.mode == "fast" else 720.0
        rows = _run_cases(cases, binary, budget)
        _apply_known_gaps(rows)
        _print_rows(rows)
        passed = sum(bool(row["passed"]) for row in rows)
        max_deviation = max((abs(float(row["deviation"])) for row in rows if row["deviation"] != "-"), default=0.0)
        print(f"SELFTEST_SUMMARY passed={passed}/{len(rows)} max_abs_deviation_s={max_deviation:.3f} mode={args.mode} work_dir={run_dir}", flush=True)
        if args.fault == "catchup":
            print("FAULT_NOTE catchup=skip overrides catchup_run; S06 expects its configured run_in_window call", flush=True)
        return 0 if passed == len(rows) else 1
    except Exception as exc:
        print(f"SELFTEST_ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
