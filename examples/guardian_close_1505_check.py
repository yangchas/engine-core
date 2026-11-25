#!/usr/bin/env python3
"""Fast fake-clock acceptance check for the 15:05 daily-kline close event (C566).

Template: ``src/engine_core/guardian/schedule.json`` event ``daily_kline_close_1505`` with its profile
``daily_kline_close`` (rung order wencai -> q2_final, attempts/backoff/timeout/on_timeout/alert_on_fallback all
read from the template).  An isolated guardiand is built and run with ``--mode=fakeclock``; the handler is swapped
for this file's own fake (``--fake run|gate|alert``).  Only the real-time timeout of a hanging rung is shortened
(3s).  No network, Redis, TD, ~/core-data or the running guardiand is touched; fake time is moved by the driver.

Scenarios (clock start is the fake "guardian start"):
  A normal 15:05 / B started 15:30 / C started 16:50 (backfill) / D wencai TIER_NO_DATA -> q2_final /
  E wencai timeout -> q2_final with no same-rung retry / F wencai TIER_DATE_MISMATCH -> q2_final silently /
  G wencai deterministic failure -> q2_final and exactly one alert / H both rungs fail -> FAILED_FINAL, one alert /
  I a better-or-equal version exists -> SKIPPED_SATISFIED, nothing fetched / J ladder reordered in config ->
  q2_final first / K started after late_until -> MISSED, nothing fetched / L retryable partial retried in-rung
  (60s fake backoff) then q2_final.

Usage: python examples/guardian_close_1505_check.py [--only A,B,...] [--schedule-file PATH]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = ROOT / "src" / "engine_core" / "guardian" / "schedule.json"
DEFAULT_WORK = Path("/home/exedev/validation/s1/C566/runs")
sys.path.insert(0, str(ROOT / "examples"))
import guardian_baostock_trigger_check as bt  # noqa: E402  (fake-clock helpers; also loads guardian_selftest as gs)

gs = bt.gs
DATE = bt.DATE
EVENT = "daily_kline_close_1505"
HANG_REAL_TIMEOUT = "3s"
RUN_LIMIT_SECONDS = 60.0


@dataclass
class Scenario:
    key: str
    title: str
    start: str
    wencai: list[str]
    q2: list[str] = field(default_factory=list)
    satisfied: bool = False
    reorder: bool = False
    expect_status: str = ""
    expect_tier: str = ""
    expect_calls: list[str] = field(default_factory=list)
    expect_alerts: list[str] = field(default_factory=list)
    expect_failed_codes: list[str] = field(default_factory=list)


def S(key, title, start, wencai, q2=(), **kw):
    return Scenario(key, title, f"{DATE}T{start}+08:00", list(wencai), list(q2), **kw)


SCENARIOS = [
    S("A", "normal 15:05", "15:04:00", ["ok"], expect_status="SUCCEEDED_DEGRADED", expect_tier="wencai", expect_calls=["wencai"]),
    S("B", "guardian started 15:30 -> backfill", "15:30:00", ["ok"], expect_status="SUCCEEDED_DEGRADED", expect_tier="wencai", expect_calls=["wencai"]),
    S("C", "guardian started 16:50 -> backfill", "16:50:00", ["ok"], expect_status="SUCCEEDED_DEGRADED", expect_tier="wencai", expect_calls=["wencai"]),
    S("D", "wencai TIER_NO_DATA -> q2_final", "15:04:00", ["nodata40"], ["ok"], expect_status="SUCCEEDED_DEGRADED", expect_tier="q2_final",
      expect_calls=["wencai", "q2_final"], expect_failed_codes=["TIER_NO_DATA"]),
    S("E", "wencai timeout -> q2_final, no same-rung retry", "15:04:00", ["hang"], ["ok"], expect_status="SUCCEEDED_DEGRADED", expect_tier="q2_final",
      expect_calls=["wencai", "q2_final"], expect_failed_codes=["HANDLER_TIMEOUT"]),
    S("F", "wencai TIER_DATE_MISMATCH -> q2_final silently", "15:04:00", ["mismatch40"], ["ok"], expect_status="SUCCEEDED_DEGRADED", expect_tier="q2_final",
      expect_calls=["wencai", "q2_final"], expect_failed_codes=["TIER_DATE_MISMATCH"]),
    S("G", "wencai deterministic -> q2_final + one alert", "15:04:00", ["det40"], ["ok"], expect_status="SUCCEEDED_DEGRADED", expect_tier="q2_final",
      expect_calls=["wencai", "q2_final"], expect_alerts=["TIER_FALLBACK"], expect_failed_codes=["DETERMINISTIC_FAILURE"]),
    S("H", "both rungs fail -> FAILED_FINAL, one alert", "15:04:00", ["det40"], ["det40"], expect_status="FAILED_FINAL", expect_tier="q2_final",
      expect_calls=["wencai", "q2_final"], expect_alerts=["FAILED_FINAL"], expect_failed_codes=["DETERMINISTIC_FAILURE", "DETERMINISTIC_FAILURE"]),
    S("I", "equal-or-better version exists -> skipped", "15:04:00", ["ok"], satisfied=True, expect_status="SKIPPED_SATISFIED", expect_tier="wencai",
      expect_calls=["wencai:satisfied"]),
    S("J", "ladder reordered in config -> q2_final first", "15:04:00", ["ok"], ["ok"], reorder=True, expect_status="SUCCEEDED_DEGRADED", expect_tier="q2_final",
      expect_calls=["q2_final"]),
    S("K", "started after late_until -> MISSED", "17:35:30", ["ok"], expect_status="MISSED", expect_tier="", expect_calls=[], expect_alerts=["MISSED"]),
    S("L", "retryable partial retried in-rung then q2_final", "15:04:00", ["retry30"], ["ok"], expect_status="SUCCEEDED_DEGRADED", expect_tier="q2_final",
      expect_calls=["wencai", "wencai", "q2_final"], expect_failed_codes=["INTERNAL_ERROR", "INTERNAL_ERROR"]),
]

OUTCOMES = {
    "ok": (10, {"status": "degraded", "error_code": "", "retryable": False}),
    "retry30": (30, {"status": "failed", "error_code": "TIER_PARTIAL", "retryable": True}),
    "det40": (40, {"status": "failed", "error_code": "DETERMINISTIC_FAILURE", "retryable": False}),
    "nodata40": (40, {"status": "failed", "error_code": "TIER_NO_DATA", "retryable": False}),
    "mismatch40": (40, {"status": "failed", "error_code": "TIER_DATE_MISMATCH", "retryable": False}),
}


# ---- fake handler (same file, selected with --fake) -------------------------------------------------------------
def _fake_dir() -> Path:
    return Path(os.environ["CLOSE1505_FAKE_DIR"])


def _append(name: str, value: dict) -> None:
    path = _fake_dir() / name
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def fake_main(command: str) -> int:
    request = json.load(sys.stdin)
    if command == "alert":
        _append("alerts.jsonl", {"reason": request.get("reason"), "request": request.get("request")})
        print(json.dumps({"status": "sent"}))
        return 0
    if command == "gate":
        print(json.dumps({"status": "ready", "reason": "calendar_ready"}))
        return 0
    args = request.get("args") or {}
    provider = str(args.get("providers"))
    script = list(args.get("script") or ["ok"])
    seen = sum(1 for line in (_fake_dir() / "calls.jsonl").read_text().splitlines()
               if json.loads(line).get("provider") == provider) if (_fake_dir() / "calls.jsonl").exists() else 0
    outcome = script[min(seen, len(script) - 1)]
    if (_fake_dir() / "satisfied").exists():
        _append("calls.jsonl", {"provider": provider, "outcome": "satisfied", "now": request.get("now")})
        print(json.dumps({"contract": 1, "status": "not_applicable", "error_code": "ALREADY_SATISFIED", "retryable": False}))
        return 20
    _append("calls.jsonl", {"provider": provider, "outcome": outcome, "now": request.get("now"),
                            "timeout_seconds": request.get("timeout_seconds")})
    if outcome == "hang":
        time.sleep(3600)
    code, body = OUTCOMES[outcome]
    print(json.dumps({"contract": 1, **body, "artifacts": [], "detail": "fake " + provider}))
    return code


# ---- driver -------------------------------------------------------------------------------------------------------
def build_source(schedule: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    event = copy.deepcopy(next(e for e in schedule["events"] if e["event_id"] == EVENT))
    profile = copy.deepcopy(schedule["profiles"][event["profile"]])
    scripts = {"wencai": scenario.wencai, "q2_final": scenario.q2 or ["ok"]}
    for tier in profile["ladder"]:
        tier["args"] = {**tier["args"], "script": scripts[tier["name"]]}
        if "hang" in scripts[tier["name"]]:
            tier["timeout"] = HANG_REAL_TIMEOUT  # the only real-time shortening; everything else stays as configured
    if scenario.reorder:
        profile["ladder"].reverse()
    global_config = copy.deepcopy(schedule["global"])
    global_config["heavy_max_load5"] = 1000.0  # host load must not defer the fake attempts
    return {"schema_version": 3, "revision": schedule.get("revision", 1), "timezone": schedule["timezone"],
            "global": global_config, "profiles": {event["profile"]: profile}, "events": [event]}


def run_scenario(scenario: Scenario, binary: Path, work: Path, schedule: dict[str, Any]) -> dict[str, Any]:
    directory = work / f"run-{time.time_ns()}-scenario-{scenario.key}"
    state_root, fake = directory / "state-root", directory / "fake"
    fake.mkdir(parents=True)
    if scenario.satisfied:
        (fake / "satisfied").write_text("existing equal-or-better version\n", encoding="utf-8")
    source = build_source(schedule, scenario)
    (directory / "schedule-v3.json").write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config_path = directory / "config.json"
    migrate = subprocess.run([str(binary), "migrate", "--from", str(directory / "schedule-v3.json"), "--to", str(config_path), "--expand"],
                             cwd=directory, capture_output=True, text=True)
    if migrate.returncode:
        raise RuntimeError(migrate.stdout + migrate.stderr)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    (directory / "trade_dates.json").write_text(json.dumps([DATE]) + "\n", encoding="utf-8")
    config["calendars"] = {"trading": {"file": str(directory / "trade_dates.json")}}
    for name, command in ((config["events"][0]["handler"], "run"), ("alert", "alert")):
        entry = {"argv": [sys.executable, str(Path(__file__).resolve()), "--fake", command], "cwd": str(ROOT),
                 "env_allow": ["CLOSE1505_FAKE_DIR"], "max_output_bytes": 1048576, "supports_degrade": True}
        if command == "run":
            entry["gate_argv"] = [sys.executable, str(Path(__file__).resolve()), "--fake", "gate"]
        config["handlers"][name] = entry
    config["alerts"] = {"enabled": True}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "CLOSE1505_FAKE_DIR": str(fake),
           "GOTOOLCHAIN": "local", "GOPROXY": "off"}
    log = (directory / "guardiand.log").open("wb")
    proc = subprocess.Popen([str(binary), "run", "--config", str(config_path), "--state-root", "state-root",
                             "--mode=fakeclock", "--clock-start", scenario.start],
                            cwd=str(directory), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    sock = Path(os.path.relpath(state_root / "guardiand.sock", ROOT))
    errors: list[str] = []
    advanced = 0
    try:
        deadline = time.monotonic() + RUN_LIMIT_SECONDS
        last_size, last_change = -1, time.monotonic()
        while True:
            if time.monotonic() > deadline:
                errors.append("run limit reached before terminal state")
                break
            if proc.poll() is not None:
                errors.append(f"guardiand exited early with {proc.returncode}")
                break
            time.sleep(0.1)
            if not sock.exists():
                continue
            rows = bt.ledger_rows(state_root)
            if len(rows) != last_size:
                last_size, last_change = len(rows), time.monotonic()
            if bt.final_states(rows).get(EVENT, {}).get("status") in bt.TERMINAL:
                break
            starts = {(r["data"].get("instance"), r["data"].get("attempt")) for r in rows if r.get("type") == "ATTEMPT_START"}
            ends = {(r["data"].get("instance"), r["data"].get("attempt")) for r in rows if r.get("type") == "ATTEMPT_END"}
            if starts - ends:
                last_change = time.monotonic()
                continue
            if time.monotonic() - last_change >= bt.QUIET_SECONDS:
                if advanced >= 200:
                    errors.append("advanced 200 fake minutes without a terminal state")
                    break
                bt._advance(sock, "1m")
                advanced += 1
                last_change = time.monotonic()
        time.sleep(1.5)  # alerts are delivered after the terminal write
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
    return {"scenario": scenario, "state_root": state_root, "fake": fake, "errors": errors, "advanced": advanced}


def evaluate(result: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    scenario: Scenario = result["scenario"]
    errors = list(result["errors"])
    rows = bt.ledger_rows(result["state_root"])
    calls = gs._read_jsonl(result["fake"] / "calls.jsonl")
    alerts = gs._read_jsonl(result["fake"] / "alerts.jsonl")
    state = bt.final_states(rows).get(EVENT, {})
    got_calls = [c["provider"] + (":" + c["outcome"] if c["outcome"] == "satisfied" else "") for c in calls]
    got_alerts = [a.get("reason") for a in alerts]
    failed = [r["data"].get("error_code") for r in rows if r.get("type") == "TIER_FAILED"]
    if state.get("status") != scenario.expect_status:
        errors.append(f"status expected {scenario.expect_status} got {state.get('status')!r} ({state.get('last_error', '')})")
    if (state.get("tier") or "") != scenario.expect_tier:
        errors.append(f"tier expected {scenario.expect_tier!r} got {state.get('tier')!r}")
    if got_calls != scenario.expect_calls:
        errors.append(f"calls expected {scenario.expect_calls} got {got_calls}")
    if got_alerts != scenario.expect_alerts:
        errors.append(f"alerts expected {scenario.expect_alerts} got {got_alerts}")
    if failed != scenario.expect_failed_codes:
        errors.append(f"TIER_FAILED codes expected {scenario.expect_failed_codes} got {failed}")
    timeline = bt.attempt_timeline(rows)
    if scenario.key == "A" and not timeline[0]["time"].startswith(f"{DATE}T15:05:"):
        errors.append(f"A must fire at 15:05, got {timeline[0]['time']}")
    if scenario.key in ("B", "C") and timeline and not timeline[0]["time"].startswith(f"{DATE}T{scenario.start[11:13]}:{scenario.start[14:16]}"):
        errors.append(f"{scenario.key} must backfill at the guardian start, got {timeline[0]['time']}")
    if scenario.key == "E" and [t["status"] for t in timeline[:1]] != ["HANDLER_TIMEOUT"]:
        errors.append(f"E first attempt must be killed by its timeout, got {timeline[:1]}")
    if scenario.key == "G" and alerts:
        text = json.dumps(alerts[0].get("request"))
        if "wencai:DETERMINISTIC_FAILURE" not in text or "q2_final" not in text:
            errors.append(f"G alert lacks failed/succeeded tier: {text}")
    if scenario.key == "L":
        gaps = [bt._parse(b["time"]) - bt._parse(a["time"]) for a, b in zip(timeline, timeline[1:3])]
        if gaps[:1] != [timedelta(seconds=60)]:
            errors.append(f"L in-rung retry gap expected 60s got {gaps}")
    return errors, rows, {"state": state, "alerts": got_alerts, "calls": got_calls, "advanced": result["advanced"]}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--fake"]:
        return fake_main(argv[1])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--schedule-file", type=Path, default=SCHEDULE, help="schedule.json template (mutation checks)")
    args = parser.parse_args(argv)
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    wanted = {k.strip().upper() for k in args.only.split(",") if k.strip()}
    scenarios = [s for s in SCENARIOS if not wanted or s.key in wanted]
    schedule = json.loads(args.schedule_file.resolve().read_text(encoding="utf-8"))
    (work / "build").mkdir(parents=True, exist_ok=True)
    binary = gs._build_binary(work / "build")
    print(f"built isolated guardiand: {binary}")
    failed = 0
    for scenario in scenarios:
        print(f"\n== scenario {scenario.key}: {scenario.title} (guardian start {scenario.start[11:19]})")
        try:
            result = run_scenario(scenario, binary, work, schedule)
        except Exception as exc:
            failed += 1
            print(f"  FAIL {scenario.key}: {type(exc).__name__}: {exc}")
            continue
        errors, rows, extra = evaluate(result)
        bt.print_timeline(rows)
        print(f"  final: status={extra['state'].get('status')} tier={extra['state'].get('tier')} calls={extra['calls']} "
              f"alerts={extra['alerts']} fake_minutes_advanced={extra['advanced']}")
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
