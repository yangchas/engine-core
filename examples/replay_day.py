#!/usr/bin/env python3
"""Replay one whole trading day through a real guardiand on a fake clock (L4b, C665).

    replay_day.py --date 2026-10-23 [--from HH:MM:SS] [--until HH:MM:SS] [--work-dir DIR] [--report FILE]
                  [--handler PATH] [--strict-data]

A temporary guardiand (fakeclock, temporary state root, alerts dry-run) runs the day's whole schedule. The daemon itself
moves its clock: ``clock.run`` steps it one second at a time and waits after every step until all the work that step
released has finished or is asleep, exactly like the 1-second ticks of a real day. Handlers are the real
examples/guardian_handler.py behind examples/replay_handler.py, which answers upstream requests only from recorded
responses (tests/fixtures/real); every other source (Kaipan requests without a recording, Baostock, WenCai, Redis, TD)
is a recorded gap. Nothing touches production data, Redis, TD or mail.

Two verdicts. ``verdict`` (exit code) is about orchestration and the contract: every instance reached a terminal state,
every reply obeys the contract matrix, no reply is an internal/unmapped error, the daemon settled after each step.
``data_verdict`` is about data: every failed instance should be attributable to a recorded gap; failures with no gap
are listed as unattributed (they need a recording of the day's starting state, or they are defects). --strict-data
makes unattributed failures fail the run as well.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(ROOT / "src"))
import guardian_selftest as gs  # noqa: E402

# the same set as guardiand's isTerminal (native/guardiand/runtime.go)
TERMINAL = {"SUCCEEDED", "SUCCEEDED_DEGRADED", "FAILED_FINAL", "CIRCUIT_OPEN", "MISSED", "GATE_BLOCKED", "SKIPPED",
            "SKIPPED_NA", "SKIPPED_SATISFIED", "SKIPPED_PHASE", "SKIPPED_DEP"}
FAILED = ("FAILED_FINAL", "MISSED", "CIRCUIT_OPEN")

DEFAULT_WORK = Path("/home/exedev/validation/s1/C665/runs")
HANDLER = ROOT / "examples" / "replay_handler.py"
SCHEDULE = ROOT / "src" / "engine_core" / "guardian" / "schedule.json"
UNMAPPED = ("INTERNAL_ERROR", "NOT_IMPLEMENTED", "UNKNOWN_EVENT")


def control(sock_path: Path, op: str, args: dict[str, Any] | None = None, timeout: float = 60.0) -> dict[str, Any]:
    """One request on guardiand's control socket."""
    request = {"t": "req", "id": f"{op}-{time.monotonic_ns()}", "op": op, "args": args or {}}
    # unix socket paths are limited to ~107 bytes: connect by file name from inside the socket's directory
    previous = os.getcwd()
    os.chdir(sock_path.parent)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(sock_path.name)
    finally:
        os.chdir(previous)
    with client:
        client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = client.recv(65536)
            if not chunk:
                raise RuntimeError(f"control socket closed during {op}")
            buffer.extend(chunk)
    response = json.loads(bytes(buffer).splitlines()[0])
    if response.get("ok") is not True:
        raise RuntimeError(f"{op} failed: {response}")
    return response.get("result") or {}


def _seed_calendar(root: Path) -> list[str]:
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

    fixture = ROOT / "tests" / "fixtures" / "trade_calendar"
    payload = json.loads((fixture / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    dates = [row["calendar_date"] for row in payload["rows"] if row["is_trading_day"] == "1"]
    TradeCalendarStore(root).publish_confirmed(
        dates, source="C325 BaoStock+SZSE", fetched_at="2026-10-20T00:00:00+00:00",
        coverage_start="2026-01-01", coverage_end="2026-12-31",
        raw_responses={path.name: path.read_bytes() for path in fixture.glob("*.json")})
    return dates


def _entry(command: str, handler: Path, *extra: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "argv": [sys.executable, str(handler), command, *extra], "cwd": str(ROOT),
        "env_allow": ["CORE_DATA_ROOT", "CORE_REPLAY_DIR", "CORE_PROVIDER_STATE", "REDIS_URL", "TD_CONFIG", "HOME",
                      "PYTHONPATH"],
        "max_output_bytes": 1048576, "supports_degrade": True}
    if command == "run":
        entry["gate_argv"] = [sys.executable, str(handler), "gate"]
    return entry


def build_config(binary: Path, schedule: dict[str, Any], work: Path, handler: Path, dates: list[str]) -> Path:
    source = work / "schedule-v3.json"
    source.write_text(json.dumps(schedule, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config_path = work / "config.json"
    migrate = subprocess.run([str(binary), "migrate", "--from", str(source), "--to", str(config_path), "--expand"],
                             cwd=work, capture_output=True, text=True)
    if migrate.returncode:
        raise RuntimeError(f"migrate failed: {migrate.stdout}{migrate.stderr}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    calendar = work / "trade_dates.json"
    calendar.write_text(json.dumps(dates) + "\n", encoding="utf-8")
    config["calendars"] = {"trading": {"file": str(calendar)}}
    for event in config["events"]:
        config["handlers"][event["handler"]] = _entry("run", handler)
    config["handlers"]["alert"] = _entry("alert", handler, "--dry-run")
    config["alerts"] = {"enabled": True}
    # the replay must not depend on how busy this host is: never defer heavy events on load
    config["heavy_max_load1"] = 1000000
    config["heavy_max_load5"] = 1000000
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config_path


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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


def run_day(day: str, work: Path, *, binary: Path, handler: Path = HANDLER, schedule_file: Path = SCHEDULE,
            wall_limit: float = 7200.0, start: str = "00:00:00", until: str = "23:59:59") -> dict[str, Any]:
    work.mkdir(parents=True, exist_ok=True)
    data_root, replay_dir, home = work / "core-data", work / "replay", work / "home"
    for path in (data_root, replay_dir, home):
        path.mkdir(parents=True, exist_ok=True)
    dates = _seed_calendar(data_root)
    if day not in dates:
        raise RuntimeError(f"{day} is not a trading day in the recorded calendar")
    schedule = json.loads(schedule_file.read_text(encoding="utf-8"))
    config_path = build_config(binary, schedule, work, handler, dates)
    state_root = work / "state-root"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "GOTOOLCHAIN": "local",
           "GOPROXY": "off", "HOME": str(home), "CORE_DATA_ROOT": str(data_root),
           "CORE_REPLAY_DIR": str(replay_dir), "CORE_PROVIDER_STATE": "memory",
           "REDIS_URL": "redis://replay.invalid:6379/0", "TD_CONFIG": "replay-no-td",
           "PYTHONPATH": os.pathsep.join([str(ROOT / "examples" / "replay_site"), str(ROOT / "src"), str(ROOT)])}
    log = (work / "guardiand.log").open("wb")
    proc = subprocess.Popen([str(binary), "run", "--config", str(config_path), "--state-root", "state-root",
                             "--mode=fakeclock", "--clock-start", f"{day}T{start}+08:00"],
                            cwd=str(work), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    sock = state_root / "guardiand.sock"
    started = time.monotonic()
    errors: list[str] = []
    instances: list[str] = []
    outside: list[str] = []
    end_state: dict[str, Any] = {}
    try:
        while not sock.exists():
            if proc.poll() is not None:
                raise RuntimeError(f"guardiand exited early with {proc.returncode}")
            if time.monotonic() - started > 60:
                raise RuntimeError("guardiand control socket did not appear")
            time.sleep(0.05)
        control(sock, "clock.settle", {"idle_timeout": "10m"}, timeout=700)            # the boot chain
        # only instances that start and end inside the replayed slice can be judged
        listed = control(sock, "list", {"date": day}) or []
        instances = [str(x["id"]) for x in listed
                     if x.get("trigger_type") == "startup" or (start <= x["start"] and x["deadline"] <= until)]
        outside = [str(x["id"]) for x in listed if str(x["id"]) not in instances]
        end_state = control(sock, "clock.run",
                            {"until": f"{day}T{until}+08:00", "step": "1s", "idle_timeout": "10m"},
                            timeout=wall_limit)
        if not end_state.get("settled"):
            errors.append(f"daemon did not settle after a clock step ({end_state})")
    except Exception as exc:          # noqa: BLE001 - reported, never swallowed
        errors.append(f"{type(exc).__name__}: {exc}")
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
    return {"day": day, "work": str(work), "state_root": state_root, "replay_dir": replay_dir, "errors": errors,
            "expected": sorted(instances), "outside": sorted(outside), "end_state": end_state,
            "wall_seconds": round(time.monotonic() - started, 1)}


def evaluate(run: dict[str, Any], *, strict_data: bool = False) -> dict[str, Any]:
    rows = gs._read_jsonl(run["state_root"] / "ledger" / f"{run['day']}.jsonl")
    states = final_states(rows)
    replies = [r for r in _jsonl(run["replay_dir"] / "replies.jsonl") if r.get("command") == "run"]
    gaps = _jsonl(run["replay_dir"] / "unrecorded.jsonl")
    problems: list[str] = list(run["errors"])
    histogram = Counter(str(states.get(i, {}).get("status", "NO_STATE")) for i in run["expected"])

    def root_failure(instance: str, seen: frozenset = frozenset()) -> str | None:
        state = states.get(instance, {})
        if state.get("status") in FAILED:
            return instance
        for upstream in state.get("wait_for") or []:
            if upstream not in seen and (found := root_failure(upstream, seen | {instance})):
                return found
        return None

    blocked: dict[str, str] = {}
    judged = sorted((set(run["expected"]) | set(states)) - set(run.get("outside", [])))
    for instance in judged:
        status = states.get(instance, {}).get("status", "NO_STATE")
        if status in TERMINAL:
            continue
        if status == "DEFERRED" and (root := root_failure(instance)):
            blocked[instance] = root      # queued behind an upstream that failed for good: pending by design
        else:
            problems.append(f"NOT_TERMINAL {instance}: {status}")
    violations, unmapped = [], []
    for item in replies:
        reply = item.get("reply") or {}
        if reply.get("contract_violations"):
            violations.append({"instance": item.get("instance"), "violations": reply["contract_violations"]})
        if reply.get("error_code") in UNMAPPED:
            unmapped.append({"instance": item.get("instance"), "error_code": reply.get("error_code"),
                             "detail": str(reply.get("detail"))[:200]})
    problems += [f"CONTRACT_VIOLATION {v['instance']}: {v['violations']}" for v in violations]
    problems += [f"UNMAPPED_ERROR {u['instance']}: {u['error_code']} {u['detail']}" for u in unmapped]
    # data: a failed instance is attributable when its own attempts hit a recorded gap
    gap_sources: dict[str, Counter] = {}
    for gap in gaps:
        gap_sources.setdefault(str(gap.get("instance")), Counter())[str(gap.get("source"))] += 1
    attributed: Counter = Counter()
    unattributed = []
    for instance in judged:
        state = states.get(instance, {})
        if state.get("status") not in FAILED:
            continue
        sources = gap_sources.get(instance)
        if sources:
            attributed[sources.most_common(1)[0][0]] += 1
        else:
            unattributed.append({"instance": instance, "status": state.get("status"),
                                 "error": str(state.get("last_error") or state.get("LastError") or "")[:160]})
    data_verdict = "PASS" if not unattributed else "UNATTRIBUTED_FAILURES"
    if strict_data:
        problems += [f"UNATTRIBUTED_FAILURE {u['instance']}: {u['status']} {u['error']}" for u in unattributed]
    return {"day": run["day"], "verdict": "PASS" if not problems else "FAIL", "problems": problems,
            "data_verdict": data_verdict, "instances": len(run["expected"]), "states": dict(histogram),
            "replies": len(replies), "failures_attributed_to_gap": dict(attributed),
            "unattributed_failures": unattributed, "blocked_dependents": blocked,
            "recorded_gaps": dict(Counter(f"{g.get('source')}.{g.get('call')}" for g in gaps)),
            "wall_seconds": run["wall_seconds"],
            "note": "handlers run on virtual time (sleep advances it at once; datetime.now is real): design/25 2.3 in-function waits"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--handler", type=Path, default=HANDLER)
    parser.add_argument("--schedule-file", type=Path, default=SCHEDULE)
    parser.add_argument("--wall-limit", type=float, default=7200.0)
    parser.add_argument("--from", dest="start", default="00:00:00", help="replay from this time of day (HH:MM:SS)")
    parser.add_argument("--until", default="23:59:59", help="replay up to this time of day (HH:MM:SS)")
    parser.add_argument("--strict-data", action="store_true")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    work = args.work_dir.resolve() / f"replay-{args.date}-{time.strftime('%H%M%S')}"
    build_dir = args.work_dir.resolve() / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    binary = gs._build_binary(build_dir)
    run = run_day(args.date, work, binary=binary, handler=args.handler.resolve(),
                  schedule_file=args.schedule_file.resolve(), wall_limit=args.wall_limit,
                  start=args.start, until=args.until)
    report = evaluate(run, strict_data=args.strict_data)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
