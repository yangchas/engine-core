#!/usr/bin/env python3
"""Run isolated, wall-clock guardian self-tests against a locally built binary.

Fast windows start 5--25 seconds ahead.  Retry delays retain the runtime's
eventRetryDelay formula (backoff * 2**(attempt-1), capped at five minutes),
except that the suite runs independent scenarios concurrently.  The minutes
mode moves the S01/S08/S15 start points to +2m/+3m/+5m and maps the production
schedule's 75m30s span onto six minutes. Target-date cases use the captured
BaoStock trading-calendar fixture and fail closed outside its coverage.

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
    "SKIPPED_DEP", "SKIPPED_SATISFIED",
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


def _effective_schedule_instances(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand each v3 event's configured trigger time into runtime instances."""
    instances: list[dict[str, Any]] = []
    for order, event in enumerate(schedule.get("events", [])):
        event_id = event.get("event_id") or event.get("id")
        if not event_id:
            continue
        configured_times = event.get("at")
        is_multiple = isinstance(configured_times, list)
        if isinstance(configured_times, str):
            times = [configured_times]
        elif is_multiple:
            times = configured_times
        elif event.get("start"):
            # The migrated config form uses start after Go expands a schedule.
            times = [event["start"]]
        else:
            continue
        for configured_time in times:
            if not isinstance(configured_time, str):
                continue
            parsed = None
            for time_format in ("%H:%M:%S", "%H:%M"):
                try:
                    parsed = datetime.strptime(configured_time, time_format)
                    break
                except ValueError:
                    pass
            if parsed is None:
                continue
            start = parsed.strftime("%H:%M:%S")
            instance = dict(event)
            instance["source_event_id"] = event_id
            instance["instance_id"] = f"{event_id}@{start[:5]}" if is_multiple else event_id
            instance["start"] = start
            instance["_source_order"] = order
            instances.append(instance)
    return instances


def _compare_legacy_schedule(schedule: dict[str, Any]) -> tuple[int, int]:
    legacy = json.loads((ROOT / "native/guardiand/testdata/schedule_v2.json").read_text(encoding="utf-8"))
    legacy_events = legacy.get("events", legacy)
    current_events = schedule.get("events", schedule)
    current_ids = {event.get("event_id") or event.get("id") for event in current_events}
    missing = sorted(event.get("event_id") for event in legacy_events if event.get("event_id") not in current_ids)
    effective = _effective_schedule_instances(schedule)
    times_by_id: dict[str, set[str]] = {}
    for instance in effective:
        times_by_id.setdefault(instance["source_event_id"], set()).add(instance["start"])
    timed_legacy = [event for event in legacy_events if event.get("start")]
    mismatched = []
    for event in timed_legacy:
        expected = event["start"]
        try:
            parsed = next(datetime.strptime(expected, fmt) for fmt in ("%H:%M:%S", "%H:%M")
                          if _matches_time_format(expected, fmt))
        except StopIteration:
            mismatched.append(event["event_id"])
            continue
        if parsed.strftime("%H:%M:%S") not in times_by_id.get(event["event_id"], set()):
            mismatched.append(event["event_id"])
    if len(legacy_events) != 62 or missing or mismatched:
        raise RuntimeError(
            f"legacy schedule comparison failed: events={len(legacy_events)}/62 "
            f"missing={missing[:5]} start_mismatches={mismatched[:5]}"
        )
    return len(legacy_events), len(timed_legacy)


def _matches_time_format(value: str, time_format: str) -> bool:
    try:
        datetime.strptime(value, time_format)
    except ValueError:
        return False
    return True


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


def _write_captured_trade_dates(path: Path) -> None:
    fixture = ROOT / "tests" / "fixtures" / "trade_calendar" / "baostock-2026-01-01_2026-12-31.json"
    source = json.loads(fixture.read_text(encoding="utf-8"))
    dates = [row["calendar_date"] for row in source["rows"] if row["is_trading_day"] == "1"]
    path.write_text(json.dumps(dates, separators=(",", ":")) + "\n", encoding="utf-8")


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


def _migration_event_differences(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    def execution_fields(event: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(json.dumps(event))
        for key in ("id", "profile", "catchup_source", "ladder_source", "window_source"):
            value.pop(key, None)
        # V2 migration materializes the start as at_times=[start]. This is the
        # same single window trigger already represented by start/deadline.
        trigger = value.get("trigger")
        if (isinstance(trigger, dict) and trigger.get("type") == "window"
                and trigger.get("at_times") == [trigger.get("start")]):
            trigger.pop("at_times")
        for rung in value.get("ladder", []):
            rung.pop("source", None)
        return value

    left, right = execution_fields(before), execution_fields(after)
    if left == right:
        return []
    return [f"{key}: before={left.get(key)!r}, after={right.get(key)!r}"
            for key in sorted(set(left) | set(right)) if left.get(key) != right.get(key)]


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
    schedule_source: dict[str, Any] | None = None
    schedule_source_before: dict[str, Any] | None = None
    migration_differences: list[str] = field(default_factory=list)

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
        needs_captured_calendar = any(
            event.get("data_date_offset") == -1
            or (isinstance(event.get("args"), dict) and event["args"].get("target") == "prev_trade_day")
            for event in self.events
        )
        if dates is not None:
            self.calendar_path.write_text(json.dumps(dates) + "\n", encoding="utf-8")
        elif needs_captured_calendar:
            _write_captured_trade_dates(self.calendar_path)
        else:
            self.calendar_path.write_text(json.dumps([self.date]) + "\n", encoding="utf-8")
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
        if self.fault == "disable-budget" and self.name.startswith("S19"):
            self.events[0]["ladder"][0]["budget_enabled"] = False
        if self.schedule_source is not None:
            source_path = self.work / "schedule-v3.json"
            source_path.write_text(json.dumps(self.schedule_source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result = subprocess.run(
                [str(self.binary), "migrate", "--from", str(source_path), "--to", str(self.config_path), "--expand"],
                cwd=self.work,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise RuntimeError(f"S24 v3 migrate failed ({result.returncode}): {result.stdout}{result.stderr}")
            migrated = json.loads(self.config_path.read_text(encoding="utf-8"))
            if self.schedule_source_before is not None:
                before_source_path = self.work / "schedule-before.json"
                before_config_path = self.work / "before-expanded.json"
                before_source_path.write_text(json.dumps(self.schedule_source_before, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                before_result = subprocess.run(
                    [str(self.binary), "migrate", "--from", str(before_source_path), "--to", str(before_config_path), "--expand"],
                    cwd=self.work,
                    capture_output=True,
                    text=True,
                )
                if before_result.returncode:
                    raise RuntimeError(f"S28 before migrate failed ({before_result.returncode}): {before_result.stdout}{before_result.stderr}")
                before = json.loads(before_config_path.read_text(encoding="utf-8"))
                before_event = next((event for event in before.get("events", []) if event.get("id") == "profile_form"), None)
                after_event = next((event for event in migrated.get("events", []) if event.get("id") == "profile_form"), None)
                if before_event is None or after_event is None:
                    self.migration_differences.append("profile_form missing from before/after migrated config")
                else:
                    self.migration_differences = _migration_event_differences(before_event, after_event)
            migrated["calendars"]["trading"]["file"] = str(self.calendar_path)
            handlers = _handler_config()
            for event in migrated["events"]:
                migrated["handlers"][event["handler"]] = handlers["selftest"]
            migrated["alerts"] = {"enabled": True}
            self.config_path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if self.special == "target_unresolved":
            self.calendar_path.unlink(missing_ok=True)
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
            instance = data.get("instance")
            if event_id:
                result[str(event_id)] = state
            if instance:
                result[str(instance)] = state
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


def _build_binary(work: Path, fault: str = "") -> Path:
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
    command = [str(GO), "build", "-o", str(binary)]
    if fault == "retry-no-data":
        runtime_source = GO_ROOT / "runtime.go"
        injected = work / "runtime-fault-retry-no-data.go"
        source = runtime_source.read_text(encoding="utf-8")
        old_decode = 'return "failed", "TIER_NO_DATA", false, detailText'
        old_advance = 'if code == "TIER_NO_DATA" {'
        if source.count(old_decode) != 1 or source.count(old_advance) != 1:
            raise RuntimeError("retry-no-data fault injection anchors changed")
        source = source.replace(old_decode, 'return "failed", "TIER_NO_DATA", true, detailText', 1)
        source = source.replace(old_advance, 'if code == "TIER_NO_DATA" && false {', 1)
        injected.write_text(source, encoding="utf-8")
        overlay = work / "runtime-fault-overlay.json"
        overlay.write_text(json.dumps({"Replace": {str(runtime_source): str(injected)}}) + "\n", encoding="utf-8")
        command.extend(["-overlay", str(overlay)])
    elif fault == "heavy-gate-lock":
        runtime_source = GO_ROOT / "runtime.go"
        injected = work / "runtime-fault-heavy-gate-lock.go"
        source = runtime_source.read_text(encoding="utf-8")
        entry = "\tr.running[x.ID] = true\n"
        release = "\t\tdelete(r.running, x.ID)\n\t\tr.mu.Unlock()\n"
        if source.count(entry) != 1 or source.count(release) != 1:
            raise RuntimeError("heavy-gate-lock fault injection anchors changed")
        source = source.replace(
            entry,
            entry + "\tif cost == \"heavy\" && event.Gate.Ref != \"\" {\n\t\tr.heavyRunning = true\n\t}\n",
            1,
        )
        source = source.replace(
            release,
            "\t\tdelete(r.running, x.ID)\n"
            "\t\tif cost == \"heavy\" && event.Gate.Ref != \"\" {\n"
            "\t\t\tr.heavyRunning = false\n"
            "\t\t\tr.signalHeavyChangedLocked()\n"
            "\t\t}\n\t\tr.mu.Unlock()\n",
            1,
        )
        injected.write_text(source, encoding="utf-8")
        overlay = work / "runtime-fault-overlay.json"
        overlay.write_text(json.dumps({"Replace": {str(runtime_source): str(injected)}}) + "\n", encoding="utf-8")
        command.extend(["-overlay", str(overlay)])
    command.append(".")
    result = subprocess.run(command, cwd=GO_ROOT, env=env, capture_output=True, text=True)
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
        "S16": 55 if fast else 330,
        "S17": 56 if fast else 345,
        "S18": 57 if fast else 360,
        "S20": 58 if fast else 375,
        "S21": 59 if fast else 390,
        "S22": 60 if fast else 405,
        "S23": 61 if fast else 420,
        "S28": 62 if fast else 435,
        "S29": 70 if fast else 450,
        "S30": 72 if fast else 465,
        "S31": 74 if fast else 480,
        "S32": 76 if fast else 495,
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

    # S12 preserves args and resolves data_date_offset through the captured calendar.
    s = at("S12")
    cases.append(mk("S12 args and target date", "S12", [
        _event("offset_minus_one", s, deadline=s + timedelta(seconds=18), data_date_offset=-1, args={"marker": "passthrough", "nested": {"x": 1}}),
        _event("offset_zero", s, deadline=s + timedelta(seconds=18), data_date_offset=0),
    ], ["offset_minus_one", "offset_zero"], "args preserved; -1 resolves the previous trade date", s, 32))

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
    production = _effective_schedule_instances(schedule)
    legacy_event_count, legacy_start_count = _compare_legacy_schedule(schedule)
    source_start = next((item["start"] for item in production if item["source_event_id"] == "startup_0830"), None)
    source_end = next((item["start"] for item in production if item["source_event_id"] == "open_report_0945"), None)
    if source_start is None or source_end is None:
        raise RuntimeError("schedule overlay is missing startup_0830 or open_report_0945 trigger time")
    start_bound = datetime.strptime(source_start, "%H:%M:%S")
    end_bound = datetime.strptime(source_end, "%H:%M:%S")
    windows = [
        item for item in production
        if start_bound <= datetime.strptime(item["start"], "%H:%M:%S") <= end_bound
    ]
    windows.sort(key=lambda item: (item["start"], item["_source_order"]))
    if not windows:
        raise RuntimeError("schedule overlay found no window events between startup_0830 and open_report_0945")
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
        eid = item["instance_id"]
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
                    f"{legacy_event_count}/62 legacy events and {legacy_start_count} start times preserved; "
                    f"{len(overlay_ids)} timed instances once, in original time order, within {overlay_span:.0f}s mapping", overlay_base,
                    (mapped[-1] - now).total_seconds() + 22, "overlay"))

    # S16: a no-data result advances immediately to the second configured tier.
    s = at("S16")
    cases.append(mk("S16 no-data tier fallback", "S16", [
        _event("fallback_success", s, deadline=s + timedelta(seconds=24), ladder=[
            {"name": "redis", "args": {"behavior": "no_data", "source": "redis"}, "attempts": 4, "backoff": "1s*2", "timeout": "2s"},
            {"name": "td", "args": {"behavior": "ok", "source": "td"}, "attempts": 1, "timeout": "2s"},
            {"name": "wencai", "args": {"behavior": "ok", "source": "wencai"}, "attempts": 1, "timeout": "2s"},
        ]),
    ], ["fallback_success"], "redis TIER_NO_DATA once; td succeeds as degraded", s, 32))

    # S17 all tiers return no data; one terminal alert and a failure row per tier.
    s = at("S17")
    cases.append(mk("S17 all tiers exhausted", "S17", [
        _event("all_no_data", s, deadline=s + timedelta(seconds=24), ladder=[
            {"name": "redis", "args": {"behavior": "no_data"}, "attempts": 3, "backoff": "1s*2", "timeout": "2s"},
            {"name": "td", "args": {"behavior": "no_data"}, "attempts": 3, "backoff": "1s*2", "timeout": "2s"},
            {"name": "wencai", "args": {"behavior": "no_data"}, "attempts": 1, "timeout": "2s"},
        ]),
    ], ["all_no_data"], "FAILED_FINAL; one alert; three TIER_NO_DATA ledger rows", s, 32))

    # S18 checks that exit 40/TIER_NO_DATA does not consume same-tier retries.
    s = at("S18")
    cases.append(mk("S18 no-data does not retry", "S18", [
        _event("no_data_once", s, deadline=s + timedelta(seconds=24), ladder=[
            {"name": "redis", "args": {"behavior": "no_data"}, "attempts": 3, "backoff": "1s*2", "timeout": "2s"},
            {"name": "td", "args": {"behavior": "ok"}, "attempts": 1, "timeout": "2s"},
        ]),
    ], ["no_data_once"], "one redis call before td fallback", s, 28))

    # S19 starts late with only the final tier's timeout budget remaining.
    s = (now - timedelta(seconds=3)).replace(microsecond=0)
    cases.append(mk("S19 time budget skips rung", "S19", [
        _event("budget_skip", s, deadline=now + timedelta(seconds=8), catchup="run_in_window", ladder=[
            {"name": "too_slow", "args": {"behavior": "ok"}, "attempts": 1, "timeout": "10s"},
            {"name": "fits", "args": {"behavior": "ok"}, "attempts": 1, "timeout": "2s"},
        ]),
    ], ["budget_skip"], "first tier skipped; final tier succeeds", s, 20))

    # S20 starts two seconds before its deadline and completes a five-second handler.
    s = at("S20")
    cases.append(mk("S20 deadline preserves running attempt", "S20", [
        _event("finish_after_deadline", s, deadline=s + timedelta(seconds=2), timeout="7s", ladder=[
            {"name": "primary", "args": {"behavior": "sleep:5"}, "attempts": 1, "timeout": "7s", "budget_enabled": False},
        ]),
    ], ["finish_after_deadline"], "handler finishes after deadline and succeeds", s, 22))

    # S21 kills the daemon during tier one and checks that restart resumes that tier.
    s = at("S21")
    cases.append(mk("S21 restart resumes current tier", "S21", [
        _event("resume_tier", s, deadline=s + timedelta(seconds=32), ladder=[
            {"name": "primary", "args": {"behavior": "sleep_once:9"}, "attempts": 3, "backoff": "1s*2", "timeout": "12s"},
            {"name": "fallback", "args": {"behavior": "ok"}, "attempts": 1, "timeout": "2s"},
        ]),
    ], ["resume_tier"], "attempt 2 resumes primary tier after daemon restart", s, 38, "restart_ladder"))

    # S22 exposes two at-times for one event; its handler says the second target is satisfied.
    s = ((now + timedelta(seconds=10)).replace(microsecond=0) if fast else at("S22"))
    satisfied = _event("satisfied_twice", s, deadline=s + timedelta(seconds=80), behavior="already_satisfied")
    satisfied["ladder"] = [
        {"name": "primary", "args": {"behavior": "already_satisfied"}, "attempts": 1, "timeout": "3s"},
    ]
    satisfied_second = s + timedelta(minutes=1)
    satisfied["trigger"]["at_times"] = [_clock(s), _clock(satisfied_second)]
    cases.append(mk("S22 satisfied later trigger", "S22", [satisfied],
                    [f"satisfied_twice@{_clock(s)[:5]}", f"satisfied_twice@{_clock(satisfied_second)[:5]}"],
                    "first succeeds; second records SKIPPED_SATISFIED", s, 88))

    # S23 retains a single rung when no fallback was configured.
    s = at("S23")
    cases.append(mk("S23 no implicit fallback", "S23", [
        _event("single_rung", s, deadline=s + timedelta(seconds=24), ladder=[
            {"name": "primary", "args": {"behavior": "fail_always"}, "attempts": 2, "backoff": "1s*2", "timeout": "3s"},
        ]),
    ], ["single_rung"], "two attempts on one rung; FAILED_FINAL without fallback", s, 32))

    # S24 compares a profile ladder with old fields, both migrated from schema v3.
    first_at = ((now + timedelta(seconds=10)).replace(microsecond=0) if fast
                else now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    second_at = first_at + timedelta(minutes=1)
    final_deadline = second_at + timedelta(minutes=2)
    s = first_at
    explicit = {
        "id": "profile_form", "enabled": True, "calendar": "every_day", "handler": "auction_report",
        "at": [_clock(first_at), _clock(second_at)], "deadline": _clock(final_deadline), "profile": "retry_profile",
        "args": {"behavior": "flaky_then_ok"}, "phases": ["any"],
    }
    legacy = {
        "id": "legacy_form", "enabled": True, "calendar": "every_day", "handler": "auction_report",
        "at": [_clock(first_at), _clock(second_at)], "deadline": _clock(final_deadline), "timeout_seconds": 3,
        "max_attempts": 2, "retry_backoff": "1s", "args": {"behavior": "flaky_then_ok"}, "phases": ["any"],
    }
    migration = mk("S24 v3 profile and legacy equivalence", "S24", [],
                   [f"profile_form@{_clock(first_at)[:5]}", f"profile_form@{_clock(second_at)[:5]}",
                    f"legacy_form@{_clock(first_at)[:5]}", f"legacy_form@{_clock(second_at)[:5]}"],
                   "four expanded instances; identical retry sequences", first_at, (final_deadline - now).total_seconds() + 20,
                   "migration_equivalence")
    migration.schedule_source = {
        "schema_version": 3, "revision": 1, "timezone": "Asia/Shanghai", "profiles": {
            "retry_profile": {"catchup": "run_in_window", "window": {"deadline": _clock(final_deadline)},
                              "ladder": [{"name": "legacy", "args": {"behavior": "flaky_then_ok"},
                                          "attempts": 2, "backoff": "1s*2", "timeout": "3s"}]}
        }, "events": [explicit, legacy],
    }
    migration.events = [
        {"id": "profile_form", "trigger": {"type": "window"}},
        {"id": "legacy_form", "trigger": {"type": "window"}},
    ]
    cases.append(migration)

    # S25 exercises both daily-backfill trigger outcomes: satisfied after an
    # earlier success, and a failed first window repaired by the later window.
    first_backfill = (now + timedelta(seconds=12)).replace(microsecond=0)
    second_backfill = first_backfill + timedelta(minutes=1)
    common_at = [_clock(first_backfill), _clock(second_backfill)]
    backfill_deadline = second_backfill + timedelta(seconds=10)
    data_available_at = first_backfill + timedelta(seconds=30)
    satisfied = _event("backfill_satisfied", first_backfill, deadline=backfill_deadline,
                       behavior="already_satisfied", args={"target": "prev_trade_day"})
    satisfied["trigger"]["at_times"] = list(common_at)
    retry_backfill = _event("backfill_retry", first_backfill, deadline=backfill_deadline,
                            behavior="available_after",
                            args={"target": "prev_trade_day", "available_after": data_available_at.isoformat(timespec="seconds")})
    retry_backfill["trigger"]["at_times"] = list(common_at)
    s25 = mk("S25 daily backfill trigger recovery", "S25", [satisfied, retry_backfill],
                    [f"backfill_satisfied@{_clock(first_backfill)[:5]}",
                     f"backfill_satisfied@{_clock(second_backfill)[:5]}",
                     f"backfill_retry@{_clock(first_backfill)[:5]}",
                     f"backfill_retry@{_clock(second_backfill)[:5]}"],
                    "success→already satisfied; data arrives after first trigger, second backfill succeeds",
                    first_backfill, (backfill_deadline - now).total_seconds() + 20, "daily_backfill")
    s25.schedule_source = {
        "schema_version": 3,
        "revision": 1,
        "timezone": "Asia/Shanghai",
        "profiles": {"kline": {"catchup": "run_in_window", "ladder": [
            {"name": "primary", "attempts": 3, "backoff": "1s*2", "timeout": "3s"},
        ]}},
        "events": [
            {"id": event["id"], "enabled": True, "calendar": "every_day", "at": common_at,
             "deadline": _clock(backfill_deadline), "handler": "selftest", "profile": "kline",
             "args": event["args"]}
            for event in (satisfied, retry_backfill)
        ],
    }
    cases.append(s25)

    # S27 loses its only calendar after startup and must retry target resolution
    # using exit code 30 without ever calling the handler.
    unresolved_start = (now + timedelta(seconds=30 if fast else 90)).replace(microsecond=0)
    unresolved = _event("target_unresolved", unresolved_start,
                        deadline=unresolved_start + timedelta(seconds=18),
                        args={"target": "prev_trade_day"},
                        retry={"max_attempts": 2, "backoff": "1s"})
    cases.append(mk("S27 unavailable calendar retries target resolution", "S27", [unresolved],
                    ["target_unresolved"],
                    "CIRCUIT_OPEN; two exit-30 attempts; no handler call", unresolved_start, 28,
                    "target_unresolved"))

    # S28 compiles the same retry contract from legacy v2 fields and a v3
    # profile, compares all execution fields, then executes both expanded
    # events and checks their recorded call sequences.
    s = at("S28")
    migration_deadline = s + timedelta(seconds=24)
    flaky_args = {"behavior": "flaky_then_ok"}
    profile_form = {
        "id": "profile_form", "enabled": True, "calendar": "every_day", "handler": "selftest",
        "at": [_clock(s)], "deadline": _clock(migration_deadline), "profile": "retry_profile",
        "args": flaky_args, "phases": ["any"], "on_deadline": "missed",
    }
    legacy_form = {
        "id": "legacy_form", "enabled": True, "calendar": "every_day", "handler": "selftest",
        "at": [_clock(s)], "deadline": _clock(migration_deadline), "timeout_seconds": 3,
        "max_attempts": 2, "retry_backoff": "1s", "catchup": "run_in_window",
        "args": flaky_args, "phases": ["any"],
    }
    s28 = mk("S28 profile migration and call sequence equivalence", "S28", [
        {"id": "profile_form", "trigger": {"type": "window"}},
        {"id": "legacy_form", "trigger": {"type": "window"}},
    ], ["profile_form", "legacy_form"],
       "migrated event fields match; both call attempt 1 then attempt 2", s, 35)
    s28.schedule_source_before = {
        "schema_version": 2, "revision": 1, "timezone": "Asia/Shanghai", "events": [{
            "event_id": "profile_form", "enabled": True, "calendar": "every_day", "trigger_type": "window",
            "start": _clock(s), "deadline": _clock(migration_deadline), "handler": "selftest",
            "timeout_seconds": 3, "max_attempts": 2, "retry_backoff": "1s",
            "catchup_mode": "run_in_window", "on_deadline": "missed", "phases": ["any"], "args": flaky_args,
        }],
    }
    s28.schedule_source = {
        "schema_version": 3, "revision": 1, "timezone": "Asia/Shanghai", "profiles": {
            "retry_profile": {"catchup": "run_in_window", "retry_backoff": "1s", "ladder": [
                {"name": "legacy", "attempts": 2, "backoff": "1s*2", "backoff_cap": "5m",
                 "timeout": "3s", "budget_enabled": False}
            ]}
        }, "events": [profile_form, legacy_form],
    }
    cases.append(s28)

    # S29 reproduces the gate/producer deadlock: B makes A's gate ready, so B
    # must run while A is polling rather than wait behind A's heavy reservation.
    s = at("S29")
    gate_name = "s29_input_ready"
    cases.append(mk("S29 heavy gate does not block its producer", "S29", [
        _event("gate_wait", s, deadline=s + timedelta(seconds=14), cost="heavy", on_deadline="missed",
               gate={"ref": gate_name, "poll": "1s", "timeout": "1s"}),
        _event("gate_producer", s + timedelta(seconds=2), deadline=s + timedelta(seconds=8), cost="heavy",
               behavior="ready_gate", args={"gate_ref": gate_name}, on_deadline="missed"),
    ], ["gate_wait", "gate_producer"], "producer succeeds during gate polling; gated event then succeeds", s, 23))

    # S30 verifies the global heavy mutex covers the complete handler process.
    s = at("S30")
    cases.append(mk("S30 heavy attempts remain mutually exclusive", "S30", [
        _event("heavy_a", s, deadline=s + timedelta(seconds=24), cost="heavy", behavior="sleep:2",
               args={"record_interval": True}),
        _event("heavy_b", s, deadline=s + timedelta(seconds=24), cost="heavy", behavior="sleep:2",
               args={"record_interval": True}),
    ], ["heavy_a", "heavy_b"], "both succeed; handler process intervals do not overlap", s, 32))

    # S31 distinguishes a queued heavy event admitted before its deadline from
    # one whose on_deadline=missed deadline expires while waiting.
    s = at("S31")
    cases.append(mk("S31 heavy queue honors each deadline", "S31", [
        _event("heavy_holder", s, deadline=s + timedelta(seconds=20), cost="heavy", behavior="sleep:5"),
        _event("queued_before_deadline", s + timedelta(seconds=1), deadline=s + timedelta(seconds=18),
               cost="heavy"),
        _event("queued_after_deadline", s + timedelta(seconds=2), deadline=s + timedelta(seconds=4),
               cost="heavy", on_deadline="missed"),
    ], ["heavy_holder", "queued_before_deadline", "queued_after_deadline"],
       "holder and pre-deadline waiter succeed; expired waiter is MISSED", s, 29))

    # S32 keeps a heavy gate poll outside the slot while another heavy handler
    # runs, then reserves the slot only after the gate has opened.
    s = at("S32")
    ready_gate = "s32_delayed_ready"
    gate_path = base / "S32" / "selftest" / "gates" / ready_gate
    cases.append(mk("S32 gate readiness precedes heavy admission", "S32", [
        _event("delayed_gate", s, deadline=s + timedelta(seconds=20), cost="heavy",
               args={"record_interval": True}, gate={"ref": ready_gate, "poll": "1s", "timeout": "1s"}),
        _event("gate_poll_probe", s + timedelta(seconds=1), deadline=s + timedelta(seconds=20),
               cost="heavy", behavior="sleep:2", args={"record_interval": True}),
    ], ["delayed_gate", "gate_poll_probe"], "probe ends before gate opens; gated attempt starts after readiness", s, 28,
       actions=[((s - now).total_seconds() + 5, gate_path, "ready\n")]))

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
            else:
                expected_date = "2026-10-23" if event_id == "offset_minus_one" else rows[0].get("trade_date")
                if rows[0].get("data_date") != expected_date:
                    errors.append(f"{event_id} data_date={rows[0].get('data_date')} expected={expected_date}")
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
    elif case.name.startswith("S16"):
        rows = case.calls_for("fallback_success")
        ledger_failures = [r.get("data", {}) for r in case.ledger() if r.get("type") == "TIER_FAILED"]
        if len(rows) != 2 or [row.get("tier") for row in rows] != ["redis", "td"]:
            errors.append(f"calls/tier sequence={[(row.get('tier'), row.get('attempt')) for row in rows]}")
        if not rows or not rows[-1].get("degraded") or status("fallback_success") != "SUCCEEDED_DEGRADED":
            errors.append(f"fallback tier metadata/status={rows[-1] if rows else None}/{status('fallback_success')}")
        if len(ledger_failures) != 1 or ledger_failures[0].get("error_code") != "TIER_NO_DATA":
            errors.append(f"first tier failure ledger={ledger_failures}")
    elif case.name.startswith("S17"):
        failures = [r.get("data", {}) for r in case.ledger() if r.get("type") == "TIER_FAILED"]
        alerts = _read_jsonl(case.selftest_dir / "alerts.jsonl")
        if status("all_no_data") != "FAILED_FINAL" or len(alerts) != 1:
            errors.append(f"status/alert count={status('all_no_data')}/{len(alerts)}")
        if [row.get("tier") for row in failures] != ["redis", "td", "wencai"] or any(row.get("error_code") != "TIER_NO_DATA" for row in failures):
            errors.append(f"tier failure rows={failures}")
        if [row.get("tier") for row in case.calls_for("all_no_data")] != ["redis", "td", "wencai"]:
            errors.append("tier call order does not reach all three sources")
    elif case.name.startswith("S18"):
        rows = case.calls_for("no_data_once")
        tiers = [r.get("tier") for r in rows]
        if tiers.count("redis") != 1 or tiers != ["redis", "td"]:
            errors.append(f"TIER_NO_DATA retry/tier sequence={tiers}")
    elif case.name.startswith("S19"):
        rows = case.calls_for("budget_skip")
        states = case.states().get("budget_skip", {})
        if [r.get("tier") for r in rows] != ["fits"] or states.get("status") != "SUCCEEDED_DEGRADED":
            errors.append(f"budget call/status={[(r.get('tier')) for r in rows]}/{states.get('status')}")
        if states.get("skipped_tiers") != ["too_slow"] or not rows or rows[0].get("skipped_tiers") != ["too_slow"]:
            errors.append(f"skipped tiers state/request={states.get('skipped_tiers')}/{rows[0].get('skipped_tiers') if rows else None}")
    elif case.name.startswith("S20"):
        rows = case.calls_for("finish_after_deadline")
        if len(rows) != 1 or status("finish_after_deadline") != "SUCCEEDED":
            errors.append(f"calls/status={len(rows)}/{status('finish_after_deadline')}")
        else:
            completed_text = case.states().get("finish_after_deadline", {}).get("completed_at")
            try:
                completed = datetime.fromisoformat(completed_text)
                deadline = datetime.strptime(case.date + " " + case.events[0]["trigger"]["deadline"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                if completed <= deadline:
                    errors.append(f"completed at {completed.isoformat()} before deadline {deadline.isoformat()}")
                if (completed - _parse_call_time(rows[0].get("wall_clock"))).total_seconds() < 4.5:
                    errors.append("handler did not remain alive for its configured five seconds")
            except (TypeError, ValueError):
                errors.append(f"invalid completion/deadline timestamps={completed_text}")
        if rows and rows[0].get("tier") != "primary":
            errors.append(f"tier={rows[0].get('tier')}")
    elif case.name.startswith("S21"):
        rows = case.calls_for("resume_tier")
        if [row.get("attempt") for row in rows] != [1, 2] or [row.get("tier") for row in rows] != ["primary", "primary"]:
            errors.append(f"restart attempts/tiers={[(row.get('attempt'), row.get('tier')) for row in rows]}")
        state = case.states().get("resume_tier", {})
        if state.get("status") != "SUCCEEDED" or state.get("rung") != 1:
            errors.append(f"restart terminal state={state}")
        if not case.restarted:
            errors.append("daemon was not restarted while rung one was active")
    elif case.name.startswith("S22"):
        first, second = case.ids
        if status(first) != "SUCCEEDED" or status(second) != "SKIPPED_SATISFIED":
            errors.append(f"trigger statuses={status(first)}/{status(second)}")
        rows = case.calls_for("satisfied_twice")
        if len(rows) != 2 or rows[1].get("attempt") != 1:
            errors.append(f"trigger calls={[(row.get('instance'), row.get('attempt')) for row in rows]}")
        ledger = [r.get("data", {}) for r in case.ledger() if r.get("type") == "ALREADY_SATISFIED"]
        if len(ledger) != 1 or ledger[0].get("instance") != second:
            errors.append(f"satisfied ledger={ledger}")
    elif case.name.startswith("S23"):
        rows = case.calls_for("single_rung")
        if len(rows) != 2 or [row.get("tier") for row in rows] != ["primary", "primary"]:
            errors.append(f"single rung calls={[(row.get('tier'), row.get('attempt')) for row in rows]}")
        if status("single_rung") != "FAILED_FINAL":
            errors.append(f"single rung status={status('single_rung')}")
        alerts = _read_jsonl(case.selftest_dir / "alerts.jsonl")
        if len(alerts) != 1:
            errors.append(f"single rung terminal alert count={len(alerts)}")
    elif case.name.startswith("S24"):
        calls = case.calls()
        expected_ids = set(case.ids)
        by_instance = {key: [row for row in calls if row.get("instance") == key] for key in expected_ids}
        missing = [key for key, rows in by_instance.items() if len(rows) != 2]
        if missing:
            errors.append(f"migration expanded call counts={{{', '.join(f'{k}:{len(v)}' for k, v in by_instance.items())}}}")
        normalized = {key: [(row.get("attempt"), row.get("tier"), row.get("degraded"), row.get("args", {}).get("behavior")) for row in rows]
                      for key, rows in by_instance.items()}
        profile_seq = normalized.get(next((key for key in case.ids if key.startswith("profile_form")), ""), [])
        legacy_seq = normalized.get(next((key for key in case.ids if key.startswith("legacy_form")), ""), [])
        if not profile_seq or profile_seq != legacy_seq:
            # Compare equivalent per-instance sequences; both at-times expand identically.
            event_sequences = {}
            for prefix in ("profile_form", "legacy_form"):
                event_sequences[prefix] = sorted((key.split("@", 1)[1], value) for key, value in normalized.items() if key.startswith(prefix + "@"))
            if event_sequences.get("profile_form") != event_sequences.get("legacy_form"):
                errors.append(f"profile/legacy sequences differ={event_sequences}")
        if any(status(event_id) != "SUCCEEDED" for event_id in case.ids):
            errors.append("one or more migrated instances did not succeed")
    elif case.name.startswith("S25"):
        satisfied_first, satisfied_second, failed_first, recovered_second = case.ids
        observed = [status(key) for key in case.ids]
        if observed != ["SUCCEEDED", "SKIPPED_SATISFIED", "FAILED_FINAL", "SUCCEEDED"]:
            errors.append(f"backfill trigger statuses={observed}")
        satisfied_calls = case.calls_for("backfill_satisfied")
        retry_calls = case.calls_for("backfill_retry")
        if len(satisfied_calls) != 2 or len(retry_calls) != 2:
            errors.append(f"backfill call counts={len(satisfied_calls)}/{len(retry_calls)}")
        elif ([row.get("instance") for row in satisfied_calls] != [satisfied_first, satisfied_second]
              or [row.get("instance") for row in retry_calls] != [failed_first, recovered_second]):
            errors.append("backfill trigger call order differs from configured trigger points")
        satisfied_rows = [r.get("data", {}) for r in case.ledger() if r.get("type") == "ALREADY_SATISFIED"]
        if len(satisfied_rows) != 1 or satisfied_rows[0].get("instance") != satisfied_second:
            errors.append(f"backfill satisfied ledger={satisfied_rows}")
        failed_attempts = [r.get("data", {}) for r in case.ledger()
                           if r.get("type") == "ATTEMPT_END" and (r.get("data") or {}).get("instance") == failed_first]
        available_after = datetime.fromisoformat(str(retry_calls[0].get("args", {}).get("available_after"))) if retry_calls else None
        observed_calls = [_parse_call_time(row.get("wall_clock")) for row in retry_calls]
        if len(failed_attempts) != 1 or [row.get("exit_code") for row in failed_attempts] != [40]:
            errors.append(f"first backfill failure ledger={failed_attempts}")
        if available_after is None or len(observed_calls) != 2 or not (
                observed_calls[0] < available_after <= observed_calls[1]):
            errors.append(f"data availability did not fall between trigger calls: {observed_calls}/{available_after}")
    elif case.name.startswith("S27"):
        calls = case.calls_for("target_unresolved")
        ledger = case.ledger()
        attempts = [r.get("data", {}) for r in ledger
                    if r.get("type") == "ATTEMPT_END" and (r.get("data") or {}).get("instance") == "target_unresolved"]
        failures = [r.get("data", {}) for r in ledger
                    if r.get("type") == "FAILURE_OBSERVED" and (r.get("data") or {}).get("event") == "target_unresolved"]
        if status("target_unresolved") != "CIRCUIT_OPEN" or calls:
            errors.append(f"unresolved target status/calls={status('target_unresolved')}/{len(calls)}")
        if [row.get("exit_code") for row in attempts] != [30, 30]:
            errors.append(f"unresolved target retry ledger={[row.get('exit_code') for row in attempts]}")
        if [row.get("error_code") for row in failures] != ["TARGET_DATE_UNRESOLVED", "TARGET_DATE_UNRESOLVED"]:
            errors.append(f"unresolved target error codes={[row.get('error_code') for row in failures]}")
    elif case.name.startswith("S28"):
        if case.migration_differences:
            errors.append(f"migrate before/after fields differ={case.migration_differences}")
        sequences = {}
        for event_id in case.ids:
            rows = case.calls_for(event_id)
            sequences[event_id] = [(row.get("attempt"), row.get("tier"), row.get("args", {}).get("behavior"))
                                   for row in rows]
            if status(event_id) != "SUCCEEDED" or len(rows) != 2:
                errors.append(f"{event_id} status/calls={status(event_id)}/{len(rows)}")
        if sequences["profile_form"] != sequences["legacy_form"]:
            errors.append(f"profile/legacy call sequences differ={sequences}")
        elif sequences["profile_form"] != [(1, "legacy", "flaky_then_ok"), (2, "legacy", "flaky_then_ok")]:
            errors.append(f"unexpected migrated call sequence={sequences['profile_form']}")
    elif case.name.startswith("S29"):
        if status("gate_wait") != "SUCCEEDED" or status("gate_producer") != "SUCCEEDED":
            errors.append(f"producer/gated statuses={status('gate_producer')}/{status('gate_wait')}")
        producer = case.calls_for("gate_producer")
        gated = case.calls_for("gate_wait")
        ready_polls = [row.get("data", {}) for row in case.ledger()
                       if row.get("type") == "GATE_POLL" and (row.get("data") or {}).get("event") == "gate_wait"
                       and (row.get("data") or {}).get("status") == "ready"]
        if len(producer) != 1 or len(gated) != 1 or not ready_polls:
            errors.append(f"calls/ready polls={len(producer)}/{len(gated)}/{len(ready_polls)}")
        elif not (_parse_call_time(producer[0].get("wall_clock"))
                  <= datetime.fromisoformat(str(ready_polls[0].get("polled_at")))
                  <= _parse_call_time(gated[0].get("wall_clock"))):
            errors.append("producer did not make the gate ready before the gated handler started")
    elif case.name.startswith("S30"):
        if any(status(event_id) != "SUCCEEDED" or len(case.calls_for(event_id)) != 1 for event_id in case.ids):
            errors.append(f"heavy statuses/calls={[(event_id, status(event_id), len(case.calls_for(event_id))) for event_id in case.ids]}")
        intervals = _read_jsonl(case.selftest_dir / "attempt_intervals.jsonl")
        edges = sorted((datetime.fromisoformat(str(row["wall_clock"])), row.get("edge")) for row in intervals)
        active = maximum = 0
        for _, edge in edges:
            active += 1 if edge == "start" else -1
            maximum = max(maximum, active)
        if len(edges) != 4 or active != 0 or maximum != 1:
            errors.append(f"handler intervals edges={len(edges)} max_concurrent={maximum} final_active={active}")
    elif case.name.startswith("S31"):
        expected = {"heavy_holder": "SUCCEEDED", "queued_before_deadline": "SUCCEEDED", "queued_after_deadline": "MISSED"}
        observed = {event_id: status(event_id) for event_id in expected}
        if observed != expected:
            errors.append(f"queue deadline statuses={observed}")
        if len(case.calls_for("queued_before_deadline")) != 1 or case.calls_for("queued_after_deadline"):
            errors.append("pre-deadline queue call / post-deadline call count is incorrect")
    elif case.name.startswith("S32"):
        if status("delayed_gate") != "SUCCEEDED" or status("gate_poll_probe") != "SUCCEEDED":
            errors.append(f"gate/probe statuses={status('delayed_gate')}/{status('gate_poll_probe')}")
        ready_polls = [row.get("data", {}) for row in case.ledger()
                       if row.get("type") == "GATE_POLL" and (row.get("data") or {}).get("event") == "delayed_gate"
                       and (row.get("data") or {}).get("status") == "ready"]
        intervals = _read_jsonl(case.selftest_dir / "attempt_intervals.jsonl")
        probe_end = next((datetime.fromisoformat(str(row["wall_clock"])) for row in intervals
                          if row.get("event") == "gate_poll_probe" and row.get("edge") == "end"), None)
        gated_start = next((datetime.fromisoformat(str(row["wall_clock"])) for row in intervals
                            if row.get("event") == "delayed_gate" and row.get("edge") == "start"), None)
        if len(ready_polls) != 1 or probe_end is None or gated_start is None:
            errors.append(f"probe/gate timing evidence={len(ready_polls)}/{probe_end}/{gated_start}")
        elif not (probe_end <= datetime.fromisoformat(str(ready_polls[0].get("polled_at"))) <= gated_start):
            errors.append("probe did not finish before gated attempt admission")
    return errors


def _target_date_selftest(binary: Path, work: Path) -> dict[str, Any]:
    case_dir = work / "S26"
    case_dir.mkdir(parents=True, exist_ok=True)
    calendar = case_dir / "trade_dates.json"
    _write_captured_trade_dates(calendar)
    config_path = case_dir / "config.json"
    config = {
        "schema_version": 1,
        "config_version": 1,
        "name": "guardian-selftest-S26-target-date",
        "timezone": "Asia/Shanghai",
        "calendars": {"trading": {"file": str(calendar)}},
        "handlers": _handler_config(),
        "events": [{
            "id": "daily_kline_backfill", "enabled": True, "calendar": "every_day",
            "handler": "selftest", "args": {"target": "prev_trade_day"},
            "trigger": {"type": "window", "start": "01:00:00", "deadline": "01:05:00"},
            "timeout": "3s", "retry": {"max_attempts": 1}, "on_deadline": "missed",
        }],
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    expected = {
        "2026-10-26": "2026-10-23",
        "2026-10-24": "2026-10-23",
        "2026-10-25": "2026-10-23",
        "2026-10-08": "2026-09-30",
        "2026-09-28": "2026-09-24",
        "2027-01-04": "TARGET_DATE_UNRESOLVED",
    }
    observed: dict[str, str] = {}
    errors: list[str] = []
    for event_date, want in expected.items():
        result = subprocess.run([str(binary), "explain", str(config_path), "--date", event_date, "--json"],
                                cwd=case_dir, capture_output=True, text=True)
        if result.returncode:
            errors.append(f"{event_date}: explain exited {result.returncode}: {result.stderr.strip()}")
            continue
        try:
            rows = json.loads(result.stdout)
            row = rows[0]
            got = row.get("data_date") or "TARGET_DATE_UNRESOLVED"
            if row.get("target_date_error"):
                got = "TARGET_DATE_UNRESOLVED"
        except (json.JSONDecodeError, IndexError, TypeError, KeyError) as exc:
            errors.append(f"{event_date}: invalid explain output: {exc}")
            continue
        observed[event_date] = str(got)
        if got != want:
            errors.append(f"{event_date}: got {got}, want {want}")
    return {
        "name": "S26 previous trade date resolution",
        "scheduled": "-", "actual": "-", "deviation": "-",
        "expected": "Monday/weekend/holiday returns previous session; uncovered date unresolved",
        "observed": "; ".join(f"{day}={value}" for day, value in observed.items()),
        "passed": not errors, "reasons": errors,
    }


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
            if case.special == "restart_ladder" and not case.restarted:
                rows = case.calls_for("resume_tier")
                state = case.states().get("resume_tier", {})
                if len(rows) >= 1 and state.get("status") in {"RUNNING", "LATE_RUN"} and state.get("rung") == 1:
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
                if case.name.startswith(("S04", "S17", "S23")) and len(_read_jsonl(case.selftest_dir / "alerts.jsonl")) == 0:
                    continue
                if case.special in {"restart", "restart_ladder"} and not case.restarted:
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
    parser.add_argument("--fault", choices=("start", "backoff", "catchup", "disable-budget", "retry-no-data", "heavy-gate-lock"))
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
        binary = _build_binary(run_dir, args.fault or "")
        date = _iso_clock().date().isoformat()
        cases = _make_scenarios(args.mode, args.minutes, run_dir, date, args.fault or "")
        if args.fault:
            target = {"start": "S01", "backoff": "S03", "catchup": "S06", "disable-budget": "S19", "retry-no-data": "S18", "heavy-gate-lock": "S29"}[args.fault]
            cases = [case for case in cases if case.name.startswith(target)]
        _ACTIVE_CASES[:] = cases
        # The at-list equivalence case may start just before the next minute and
        # needs one more minute for its second trigger point.
        budget = 135.0 if args.mode == "fast" else 720.0
        rows = _run_cases(cases, binary, budget)
        if not args.fault:
            rows.append(_target_date_selftest(binary, run_dir))
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
