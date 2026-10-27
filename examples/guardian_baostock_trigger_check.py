#!/usr/bin/env python3
"""Fast fake-clock acceptance check for the configured Baostock ladder.

Template: ``src/engine_core/guardian/schedule.json`` events ``baostock_probe_1730``
(and ``baostock_retry_1740`` for scenario D) with their profile ``policy_11``.
The check keeps the template's profile/event structure, moves the trigger to
the fake clock start plus a few minutes, replaces the ladder with three tiers,
and swaps the handler for ``examples/guardian_baostock_fake_handler.py``.

Ladder used here:
- L1 baostock: attempts/backoff/timeout read from the template's legacy tier
  (expected 3 / "5m*2" / "20m").
- L2 wencai: attempts 2, backoff "2m*1" (the validator rejects a bare "2m"; *1 is a constant
  2-minute gap), timeout "10m".
- L3 q2_final: attempts 1, timeout "5m".

A-F use a synthetic 60-minute event window to fit the full fallback sequence;
G/H retain the schedule's original event windows, L1 retry values, and cost.

Expected behavior follows the ledger/runtime contract: A retries L1 three times
at 5m/10m then uses Wencai; B treats C559 exit 40 as permanent and advances;
C retries the timed-out L1 three times before advancing (the handler gets a 3s
real timeout); D's later 17:40 event is satisfied by the first event's marker;
E exhausts L1 and L2 before L3 succeeds; F alerts once after FAILED_FINAL; G
starts before the real 17:30 event deadline, releases a retryable failure after
that deadline, then succeeds within L1's real ``until``; H starts after the real
20:00 retry event deadline without an attempt and must become MISSED.

Only C uses a real handler timeout. All scheduling and backoff use fake time.
G uses a release file to move fake time across the deadline while its handler is
still running. The isolated daemon and handlers do not touch network, Redis,
TaosDB, ~/core-data, or the running guardiand. Temporary files default to
``/home/exedev/validation/s1/C560``.

Usage: python examples/guardian_baostock_trigger_check.py [--only A,B,...]
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = ROOT / "src" / "engine_core" / "guardian" / "schedule.json"
HANDLER = ROOT / "examples" / "guardian_baostock_fake_handler.py"
DEFAULT_WORK = Path("/home/exedev/validation/s1/C560/runs")

sys.path.insert(0, str(ROOT / "examples"))
import guardian_selftest as gs  # noqa: E402  (reuses the isolated Go build and jsonl reader)

BASE_CLOCK_START = "2026-10-27T18:00:00+08:00"
DATE = "2026-10-27"
TRIGGER_OFFSET = timedelta(minutes=2)       # 1730 template -> start + 2m
RETRY_OFFSET = timedelta(minutes=12)        # 1740 template -> start + 12m
WINDOW = timedelta(minutes=60)
LATE_UNTIL = "23:30:00"

L1_TEMPLATE_EVENT = "baostock_probe_1730"
L1_TEMPLATE_RETRY = "baostock_retry_1740"
L2 = {"name": "wencai", "attempts": 2, "backoff": "2m*1", "timeout": "10m"}
L3 = {"name": "q2_final", "attempts": 1, "timeout": "5m"}
HANG_REAL_TIMEOUT = "3s"

EXPECT_L1_ATTEMPTS = 3
EXPECT_L1_INTERVALS = [timedelta(minutes=5), timedelta(minutes=10)]
QUIET_SECONDS = 0.5
RUN_LIMIT_SECONDS = 45.0
TOTAL_RUN_LIMIT_SECONDS = 165.0
TERMINAL = {"SUCCEEDED", "SUCCEEDED_DEGRADED", "FAILED_FINAL", "SKIPPED_SATISFIED",
            "SKIPPED_NA", "SKIPPED_DEP", "MISSED", "CIRCUIT_OPEN"}


@dataclass
class Scenario:
    key: str
    title: str
    l1: list[str]
    l2: list[str]
    l3: list[str]
    template_event: str = L1_TEMPLATE_EVENT
    clock_start: str = BASE_CLOCK_START
    l1_timeout: str | None = None
    retry_event: bool = False
    use_template_window: bool = False
    advance_running_past_deadline: bool = False
    expect_status: str = ""
    expect_tier: str = ""
    expect_alert_reasons: list[str] = field(default_factory=list)
    expect_total_attempts: int | None = None


SCENARIOS = [
    Scenario("A", "L1 retryable x3 (5m,10m) then L2 ok", ["retry30"] * 3, ["ok"], [],
             expect_status="SUCCEEDED_DEGRADED", expect_tier="wencai", expect_total_attempts=4),
    Scenario("B", "L1 exit 40 is not retried, L2 ok", ["det40"], ["ok"], [],
             expect_status="SUCCEEDED_DEGRADED", expect_tier="wencai", expect_total_attempts=2),
    Scenario("C", "L1 hangs past its timeout, killed, then L2", ["hang"] * 3, ["ok"], [],
             l1_timeout=HANG_REAL_TIMEOUT, expect_status="SUCCEEDED_DEGRADED", expect_tier="wencai",
             expect_total_attempts=4),
    Scenario("D", "L1 ok, later 17:40-style trigger is already satisfied", ["ok"], ["ok"], [],
             retry_event=True, expect_status="SUCCEEDED", expect_tier="baostock", expect_total_attempts=2),
    Scenario("E", "L1 and L2 retryable exhausted, L3 ok", ["retry30"] * 3, ["retry30", "retry75"], ["ok"],
             expect_status="SUCCEEDED_DEGRADED", expect_tier="q2_final", expect_total_attempts=6),
    Scenario("F", "all three tiers fail", ["retry30"] * 3, ["retry30", "retry75"], ["retry30"],
             expect_status="FAILED_FINAL", expect_tier="q2_final", expect_alert_reasons=["FAILED_FINAL"],
             expect_total_attempts=6),
    Scenario("G", "L1 attempt crosses deadline, retry stays on L1 and succeeds", ["wait_retry30", "ok"], [], [],
             clock_start="2026-10-27T17:39:58+08:00", use_template_window=True,
             advance_running_past_deadline=True, expect_status="SUCCEEDED", expect_tier="baostock",
             expect_total_attempts=2),
    Scenario("H", "baostock_retry_2000 never starts after deadline", [], [], [],
             template_event="baostock_retry_2000", clock_start="2026-10-27T20:30:01+08:00",
             use_template_window=True, expect_status="MISSED", expect_alert_reasons=["MISSED"],
             expect_total_attempts=0),
]


def _clock_of(value: datetime) -> str:
    return value.strftime("%H:%M:%S")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _template(schedule: dict[str, Any], event_id: str) -> tuple[dict, dict, dict, dict]:
    events = {e.get("event_id"): e for e in schedule["events"]}
    try:
        trigger = copy.deepcopy(events[event_id])
        retry = copy.deepcopy(events[L1_TEMPLATE_RETRY])
    except KeyError as exc:
        raise SystemExit(f"template event missing from schedule.json: {exc}")
    if trigger.get("handler") != "baostock_probe" or retry.get("handler") != "baostock_probe":
        raise SystemExit("template events are not baostock_probe")
    profile = copy.deepcopy(schedule["profiles"][trigger["profile"]])
    legacy = profile["ladder"][0]
    return trigger, retry, profile, legacy


def build_source(schedule: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    trigger, retry, profile, legacy = _template(schedule, scenario.template_event)
    start = _parse(scenario.clock_start)
    l1_timeout = scenario.l1_timeout or legacy["timeout"]
    l1 = copy.deepcopy(legacy)
    l1.update({"name": "baostock", "args": {"provider": "baostock", "script": scenario.l1},
               "timeout": l1_timeout})
    profile["ladder"] = [
        l1,
        {"name": L2["name"], "args": {"provider": "wencai", "script": scenario.l2},
         "attempts": L2["attempts"], "backoff": L2["backoff"], "timeout": L2["timeout"]},
        {"name": L3["name"], "args": {"provider": "q2_final", "script": scenario.l3},
         "attempts": L3["attempts"], "timeout": L3["timeout"]},
    ]
    events = []
    templates = [(trigger, TRIGGER_OFFSET)]
    if scenario.retry_event:
        templates.append((retry, RETRY_OFFSET))
    for template, offset in templates:
        event = copy.deepcopy(template)
        if not scenario.use_template_window:
            at = start + offset
            event["at"] = _clock_of(at)
            event["available_after"] = _clock_of(at)
            event["deadline"] = _clock_of(at + WINDOW)
            event["late_until"] = LATE_UNTIL
        if not scenario.use_template_window:
            event["cost"] = "light"
        event["profile"] = trigger["profile"]
        events.append(event)
    return {
        "schema_version": 3,
        "revision": schedule.get("revision", 1),
        "timezone": schedule["timezone"],
        "global": copy.deepcopy(schedule["global"]),
        "profiles": {trigger["profile"]: profile},
        "events": events,
    }


def _handler_entry(command: str) -> dict[str, Any]:
    """Handler table entry; the fake handler answers run, gate and alert."""
    entry: dict[str, Any] = {"argv": [sys.executable, str(HANDLER), command], "cwd": str(ROOT),
                             "env_allow": ["BAOSTOCK_FAKE_DIR"], "max_output_bytes": 1048576,
                             "supports_degrade": True}
    if command == "run":
        entry["gate_argv"] = [sys.executable, str(HANDLER), "gate"]
    return entry


def patch_config(config: dict[str, Any], scenario_dir: Path) -> None:
    """Point the migrated config at the fake handler and a scenario-local calendar.

    The v3 migration names handlers ``legacy_<name>``, so the patch keys on each
    event's own handler instead of the template name.
    """
    calendar = scenario_dir / "trade_dates.json"
    calendar.write_text(json.dumps([DATE]) + "\n", encoding="utf-8")
    config["calendars"] = {"trading": {"file": str(calendar)}}
    for event in config["events"]:
        config["handlers"][event["handler"]] = _handler_entry("run")
    config["handlers"]["alert"] = _handler_entry("alert")
    config["alerts"] = {"enabled": True}


def ledger_rows(state_root: Path) -> list[dict[str, Any]]:
    return gs._read_jsonl(state_root / "ledger" / f"{DATE}.jsonl")


def final_states(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("type") != "INSTANCE_STATE":
            continue
        data = row.get("data") or {}
        instance = data.get("instance") or data.get("event_id")
        if instance:
            result[str(instance)] = data.get("state") or {}
    return result


def attempt_timeline(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    starts: dict[tuple[str, int], dict[str, Any]] = {}
    timeline = []
    for row in rows:
        data = row.get("data") or {}
        if row.get("type") == "ATTEMPT_START":
            key = (str(data.get("instance")), int(data.get("attempt", 0)))
            starts[key] = {"time": data.get("starttime", ""), "instance": key[0], "attempt": key[1],
                           "rung": data.get("rung"), "tier": data.get("tier", ""), "exit": "", "status": ""}
            timeline.append(starts[key])
        elif row.get("type") == "ATTEMPT_END":
            key = (str(data.get("instance")), int(data.get("attempt", 0)))
            entry = starts.get(key)
            if entry is not None:
                entry["exit"] = data.get("exit_code", "")
                if data.get("status"):
                    entry["status"] = str(data["status"])
                elif data.get("exit_code") not in (0, None):
                    entry["status"] = str(data.get("exit_error", ""))
                else:
                    entry["status"] = "ok"
    return timeline


def print_timeline(rows: list[dict[str, Any]]) -> None:
    print("  假时间               | 实例                 | 档位 rung | 档位名   | 尝试序号 | 退出码 | 状态")
    for item in attempt_timeline(rows):
        when = item["time"][11:19] if len(item["time"]) >= 19 else item["time"]
        print(f"  {when:<20} | {item['instance']:<20} | {str(item['rung']):<9} | {item['tier']:<8} | "
              f"{item['attempt']:<8} | {str(item['exit']):<6} | {item['status']}")


def _advance(sock_path: Path, by: str) -> None:
    request = {"t": "req", "id": f"advance-{time.monotonic_ns()}", "op": "clock.advance", "args": {"by": by}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5.0)
        client.connect(str(sock_path))
        client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
        response = bytearray()
        while b"\n" not in response:
            chunk = client.recv(4096)
            if not chunk:
                raise RuntimeError("fakeclock socket closed without a response")
            response.extend(chunk)
    payload = json.loads(bytes(response).splitlines()[0])
    if payload.get("ok") is not True:
        raise RuntimeError(f"clock.advance failed: {payload}")


def run_scenario(scenario: Scenario, binary: Path, work: Path, schedule: dict[str, Any],
                 total_deadline: float) -> dict[str, Any]:
    scenario_dir = work / f"run-{time.time_ns()}-scenario-{scenario.key}"
    state_root = scenario_dir / "state-root"
    fake_dir = scenario_dir / "fake"
    (fake_dir / "gates").mkdir(parents=True)
    (fake_dir / "gates" / "calendar_ready").write_text("ready\n", encoding="utf-8")
    scenario_dir.mkdir(parents=True, exist_ok=True)

    source = build_source(schedule, scenario)
    source_path = scenario_dir / "schedule-v3.json"
    source_path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config_path = scenario_dir / "config.json"
    migrate = subprocess.run([str(binary), "migrate", "--from", str(source_path), "--to", str(config_path), "--expand"],
                             cwd=scenario_dir, capture_output=True, text=True)
    if migrate.returncode:
        raise RuntimeError(f"migrate failed: {migrate.stdout}{migrate.stderr}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    patch_config(config, scenario_dir)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "BAOSTOCK_FAKE_DIR": str(fake_dir), "GOTOOLCHAIN": "local", "GOPROXY": "off"}
    log = (scenario_dir / "guardiand.log").open("wb")
    # Unix socket paths are limited to ~107 bytes, so the daemon uses a relative
    # state root inside its scenario directory and the driver connects through
    # a path relative to the repo root (see main()).
    proc = subprocess.Popen([str(binary), "run", "--config", str(config_path), "--state-root", "state-root",
                             "--mode=fakeclock", "--clock-start", scenario.clock_start],
                            cwd=str(scenario_dir), env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    sock_path = Path(os.path.relpath(state_root / "guardiand.sock", ROOT))
    instances = [e["event_id"] for e in source["events"]]
    advanced = 0
    errors: list[str] = []
    deadline_advanced = False
    template_deadline = None
    if scenario.advance_running_past_deadline:
        source_deadline = source["events"][0]["deadline"]
        template_deadline = datetime.strptime(f"{DATE} {source_deadline}", "%Y-%m-%d %H:%M:%S")
        template_deadline = template_deadline.replace(tzinfo=_parse(scenario.clock_start).tzinfo)
    try:
        deadline = min(time.monotonic() + RUN_LIMIT_SECONDS, total_deadline)
        last_size = -1
        last_change = time.monotonic()
        while True:
            if time.monotonic() > deadline:
                errors.append("scenario run limit reached before all instances were terminal")
                break
            if proc.poll() is not None:
                errors.append(f"guardiand exited early with {proc.returncode}")
                break
            time.sleep(0.1)
            if not sock_path.exists():
                continue
            rows = ledger_rows(state_root)
            size = len(rows)
            if size != last_size:
                last_size = size
                last_change = time.monotonic()
            states = final_states(rows)
            if all(states.get(i, {}).get("status") in TERMINAL for i in instances):
                break
            starts = {(str(r["data"].get("instance")), r["data"].get("attempt"))
                      for r in rows if r.get("type") == "ATTEMPT_START"}
            ends = {(str(r["data"].get("instance")), r["data"].get("attempt"))
                    for r in rows if r.get("type") == "ATTEMPT_END"}
            if starts - ends:
                if scenario.advance_running_past_deadline and not deadline_advanced:
                    first_start = next((r["data"].get("starttime") for r in rows
                                        if r.get("type") == "ATTEMPT_START"
                                        and r["data"].get("instance") == instances[0]), "")
                    if not first_start:
                        errors.append("G running attempt has no ledger starttime")
                        target = _parse(scenario.clock_start) + timedelta(seconds=1)
                    else:
                        attempt_started = _parse(first_start)
                        if attempt_started >= template_deadline:
                            errors.append(f"G attempt began at/after deadline {template_deadline.isoformat()}")
                        target = max(attempt_started, template_deadline) + timedelta(seconds=1)
                    delta_seconds = max(1, int((target - _parse(scenario.clock_start)).total_seconds()))
                    _advance(sock_path, f"{delta_seconds}s")
                    (fake_dir / "release.json").write_text(
                        json.dumps({"now": target.isoformat(), "deadline": template_deadline.isoformat()}) + "\n",
                        encoding="utf-8")
                    deadline_advanced = True
                last_change = time.monotonic()  # a handler is running: real time, not fake time
                continue
            if time.monotonic() - last_change >= QUIET_SECONDS:
                if advanced >= 180:
                    errors.append("fake clock advanced 180 minutes without terminal state")
                    break
                _advance(sock_path, "1m")
                advanced += 1
                last_change = time.monotonic()
        # Alerts are delivered after the terminal state write; allow a short grace.
        grace = time.monotonic() + 2.0
        while time.monotonic() < grace:
            time.sleep(0.1)
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        log.close()
    return {"scenario": scenario, "dir": scenario_dir, "state_root": state_root, "fake_dir": fake_dir,
            "errors": errors, "advanced_minutes": advanced, "instances": instances,
            "deadline_advanced": deadline_advanced, "template_deadline": template_deadline}


def evaluate(result: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    scenario: Scenario = result["scenario"]
    errors: list[str] = list(result["errors"])
    rows = ledger_rows(result["state_root"])
    calls = gs._read_jsonl(result["fake_dir"] / "calls.jsonl")
    alerts = gs._read_jsonl(result["fake_dir"] / "alerts.jsonl")
    instance = scenario.template_event
    states = final_states(rows)
    state = states.get(instance, {})
    timeline = attempt_timeline(rows)
    l1 = [t for t in timeline if t["instance"] == instance and t["rung"] == 1]
    l1_calls = [c for c in calls if c.get("provider") == "baostock" and c.get("event") == instance]
    l2_calls = [c for c in calls if c.get("provider") == "wencai" and c.get("event") == instance and c.get("outcome") != "satisfied"]
    l3_calls = [c for c in calls if c.get("provider") == "q2_final" and c.get("event") == instance and c.get("outcome") != "satisfied"]

    if state.get("status") != scenario.expect_status:
        errors.append(f"status expected {scenario.expect_status} got {state.get('status')!r} ({state.get('last_error', '')})")
    if (state.get("tier") or "") != scenario.expect_tier:
        errors.append(f"final tier expected {scenario.expect_tier} got {state.get('tier')!r}")
    alert_reasons = [a.get("reason") for a in alerts]
    if alert_reasons != scenario.expect_alert_reasons:
        errors.append(f"alerts expected {scenario.expect_alert_reasons} got {alert_reasons}")
    if scenario.expect_total_attempts is not None and len(timeline) != scenario.expect_total_attempts:
        errors.append(f"total attempts expected {scenario.expect_total_attempts} got {len(timeline)}")

    if scenario.key in ("A", "C", "E", "F"):
        l1_times = [_parse(t["time"]) for t in l1]
        if len(l1) != EXPECT_L1_ATTEMPTS:
            errors.append(f"L1 attempts expected {EXPECT_L1_ATTEMPTS} got {len(l1)}")
        else:
            gaps = [b - a for a, b in zip(l1_times, l1_times[1:])]
            if gaps != EXPECT_L1_INTERVALS:
                errors.append(f"L1 retry intervals expected {[str(g) for g in EXPECT_L1_INTERVALS]} got {[str(g) for g in gaps]}")
    if scenario.key == "A":
        if l2_calls[:1] and l2_calls[0]["index"] != 1:
            errors.append("L2 first call index not 1")
        if not l2_calls:
            errors.append("L2 wencai was never called")
        if l3_calls:
            errors.append("L3 q2_final must not be called when L2 succeeds")
    if scenario.key == "B":
        if len(l1_calls) != 1:
            errors.append(f"L1 must be called exactly once on exit 40, got {len(l1_calls)}")
        if len(l2_calls) != 1:
            errors.append(f"L2 wencai must be called once, got {len(l2_calls)}")
        failures = [r.get("data", {}) for r in rows if r.get("type") == "TIER_FAILED"
                    and r.get("data", {}).get("instance") == instance]
        if len(failures) != 1 or failures[0].get("error_code") != "CONTRACT_VIOLATION":
            errors.append(f"C559 stdout error_code was not retained in TIER_FAILED: {failures}")
        elif failures[0].get("retryable") is not False or failures[0].get("advance") is not True:
            errors.append(f"exit 40 must be permanent and advance exactly once: {failures[0]}")
    if scenario.key == "C":
        killed = [t for t in l1 if t["status"] == "HANDLER_TIMEOUT"]
        if len(killed) != EXPECT_L1_ATTEMPTS:
            errors.append(f"L1 timeouts killed expected {EXPECT_L1_ATTEMPTS} got {len(killed)}")
        if not l2_calls:
            errors.append("L2 was not reached after L1 timeouts")
    if scenario.key == "D":
        if len(l1_calls) != 1:
            errors.append(f"L1 provider calls expected 1 got {len(l1_calls)}")
        late = [c for c in calls if c.get("event") == "baostock_retry_1740"]
        fetched = [c for c in late if c.get("outcome") != "satisfied"]
        if not late:
            errors.append("17:40-style trigger did not run (no handler call)")
        elif fetched:
            errors.append(f"17:40-style trigger fetched again: {fetched}")
        late_state = states.get("baostock_retry_1740", {})
        if late_state.get("status") != "SKIPPED_SATISFIED":
            errors.append(f"17:40-style trigger status expected SKIPPED_SATISFIED got {late_state.get('status')!r}")
    if scenario.key == "E":
        if len(l2_calls) != 2:
            errors.append(f"L2 calls expected 2 got {len(l2_calls)}")
        if len(l3_calls) != 1:
            errors.append(f"L3 calls expected 1 got {len(l3_calls)}")
    if scenario.key == "F":
        if len(l3_calls) != 1:
            errors.append(f"L3 calls expected 1 got {len(l3_calls)}")
    if scenario.key == "G":
        if not result["deadline_advanced"]:
            errors.append("fake time was not advanced while the first L1 handler was running")
        deadline = result["template_deadline"]
        if not l1 or _parse(l1[0]["time"]) >= deadline:
            errors.append(f"first L1 attempt did not start before event deadline {deadline}")
        first_call = next((c for c in l1_calls if c.get("index") == 1), {})
        released_at = first_call.get("released_at")
        if not released_at or _parse(released_at) <= deadline:
            errors.append(f"retryable L1 failure was not released after event deadline: {first_call}")
        if [c.get("outcome") for c in l1_calls] != ["retry30", "ok"]:
            errors.append(f"G L1 calls expected retry30 then ok, got {l1_calls}")
        retries = [r.get("data", {}) for r in rows if r.get("type") == "TIER_RETRY_SCHEDULED"
                   and r.get("data", {}).get("instance") == instance]
        if not retries or retries[0].get("rung") != 1 or not str(retries[0].get("until", "")).endswith("23:30:00+08:00"):
            errors.append(f"G retry was not scheduled on L1 through its tier until: {retries}")
        if l2_calls or l3_calls:
            errors.append("G must succeed on L1 without advancing to a fallback tier")
    if scenario.key == "H":
        if l1_calls or l2_calls or l3_calls:
            errors.append(f"H must remain unstarted after the event deadline, got calls={calls}")
        if state.get("attempts", 0) != 0 or l1:
            errors.append(f"H must be MISSED without an attempt, state={state} timeline={l1}")

    extra = {"state": state, "alerts": alert_reasons, "advanced_minutes": result["advanced_minutes"]}
    return errors, rows, extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="", help="comma separated scenario keys, e.g. A,C")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--schedule-file", type=Path, default=SCHEDULE,
                        help="schedule.json template (used for isolated mutation checks)")
    args = parser.parse_args(argv)
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)  # driver-relative socket paths must stay short
    wanted = {k.strip().upper() for k in args.only.split(",") if k.strip()}
    scenarios = [s for s in SCENARIOS if not wanted or s.key in wanted]
    if not scenarios:
        parser.error("no scenario selected")

    schedule = json.loads(args.schedule_file.resolve().read_text(encoding="utf-8"))
    build_dir = work / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    binary = gs._build_binary(build_dir)
    print(f"built isolated guardiand: {binary}")

    failed = 0
    total_deadline = time.monotonic() + TOTAL_RUN_LIMIT_SECONDS
    for index, scenario in enumerate(scenarios):
        if time.monotonic() >= total_deadline:
            remaining = len(scenarios) - index
            failed += remaining
            print(f"FAIL: total runtime budget reached; {remaining} scenario(s) not run")
            break
        print(f"\n== scenario {scenario.key}: {scenario.title}")
        try:
            result = run_scenario(scenario, binary, work, schedule, total_deadline)
        except Exception as exc:
            failed += 1
            print(f"  FAIL {scenario.key}: {type(exc).__name__}: {exc}")
            continue
        errors, rows, extra = evaluate(result)
        print_timeline(rows)
        print(f"  final: status={extra['state'].get('status')} tier={extra['state'].get('tier')} "
              f"rung={extra['state'].get('rung')} alerts={extra['alerts']} "
              f"fake_minutes_advanced={extra['advanced_minutes']}")
        if errors:
            failed += 1
            for error in errors:
                print(f"  FAIL {scenario.key}: {error}")
        else:
            print(f"  PASS {scenario.key}")
    print(f"\npassed={len(scenarios) - failed}/{len(scenarios)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
