#!/usr/bin/env python3
"""Fast fake-clock acceptance check for the D-1 limit-up pool event ``limit_pool_d1`` (C580, acceptance C).

Template: ``src/engine_core/guardian/schedule.json`` event ``limit_pool_d1`` with profile ``limit_pool_d1`` (rung order
kaipan -> wencai -> td_derived, attempts / backoff / timeout / on_timeout / alert_on_fallback / retry_until all read from
the template).  An isolated guardiand is built and run with ``--mode=fakeclock``.  The handler is this file's ``--fake``
mode, which runs the REAL ``run_pool_tier`` (catalog commit, ordering, ``retry_until``, exit codes) with the three
network/TD sources replaced by recorded real responses or scripted failures.  Only the real-time timeout of a hanging
rung is shortened (3s).  No network, Redis, TD, ~/core-data or the live guardiand is touched; time is moved by the driver.

Scenarios (the fake clock start is the guardian start; D = 2026-10-27, the pool described is 2026-10-26):
  A started 00:30, Kaipan not published for three polls -> 00:40, 00:55, 01:10 not ready, 01:25 ok (15 min polling)
  B started 09:30 (after every poll window) -> immediate backfill, Kaipan ok
  C started 08:30, Kaipan never published -> polls 08:30/08:45, at 09:00 retry_until passed -> advance -> WenCai ok
  D Kaipan blocked (deterministic) -> WenCai ok, exactly one TIER_FALLBACK alert
  E Kaipan no data, WenCai truncated twice (30 min apart) -> TD derived ok
  F WenCai date mismatch -> TD derived silently (no alert)
  G every rung fails -> FAILED_FINAL and exactly one alert
  H started 15:00:30 (after late_until) -> MISSED explicitly, nothing fetched, never fabricated
  I an equal-or-better version exists -> SKIPPED_SATISFIED, no fetch
  J ladder reordered in the config (td first) -> td_derived first, nothing else runs
  K Kaipan hangs -> killed by timeout -> advance to WenCai

Usage: python examples/guardian_limit_pool_check.py [--only A,B,...] [--schedule-file PATH]
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
DEFAULT_WORK = Path("/home/exedev/validation/s1/C580/runs")
FIXTURES = ROOT / "tests" / "fixtures" / "limit_pool"
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(ROOT / "src"))
import guardian_baostock_trigger_check as bt  # noqa: E402  (fake-clock helpers; also loads guardian_selftest as gs)

gs = bt.gs
DATE = bt.DATE                 # 2026-10-27 (the guardian's day)
POOL_DAY = "2026-10-26"        # data_date_offset -1
EVENT = "limit_pool_d1"
HANG_REAL_TIMEOUT = "3s"
RUN_LIMIT_SECONDS = 150.0
STEP = "5m"                    # every start/backoff in the template is a multiple of 5 minutes


@dataclass
class Scenario:
    key: str
    title: str
    start: str
    kaipan: list[str]
    wencai: list[str] = field(default_factory=lambda: ["ok"])
    td: list[str] = field(default_factory=lambda: ["ok"])
    satisfied: bool = False
    reorder: bool = False
    expect_status: str = ""
    expect_tier: str = ""
    expect_calls: list[str] = field(default_factory=list)
    expect_alerts: list[str] = field(default_factory=list)
    expect_failed_codes: list[str] = field(default_factory=list)
    expect_times: list[str] = field(default_factory=list)       # HH:MM:SS of the handler calls, in order


def S(key, title, start, kaipan, **kw):
    return Scenario(key, title, f"{DATE}T{start}+08:00", list(kaipan), **kw)


SCENARIOS = [
    S("A", "started 00:30, Kaipan unpublished for 3 polls then ok", "00:30:00",
      ["notready", "notready", "notready", "ok"], expect_status="SUCCEEDED", expect_tier="kaipan",
      expect_calls=["kaipan"] * 4, expect_failed_codes=["INTERNAL_ERROR"] * 3,
      expect_times=["00:40:00", "00:55:00", "01:10:00", "01:25:00"]),
    S("B", "guardian started 09:30 -> backfill now", "09:30:00", ["ok"], expect_status="SUCCEEDED",
      expect_tier="kaipan", expect_calls=["kaipan"], expect_times=["09:30:00"]),
    S("C", "started 08:30, Kaipan never published -> retry_until passes -> WenCai", "08:30:00", ["notready"],
      expect_status="SUCCEEDED_DEGRADED", expect_tier="wencai", expect_calls=["kaipan", "kaipan", "kaipan", "wencai"],
      expect_failed_codes=["INTERNAL_ERROR", "INTERNAL_ERROR", "TIER_NO_DATA"],
      expect_times=["08:30:00", "08:45:00", "09:00:00", "09:00:00"]),
    S("D", "Kaipan blocked (deterministic) -> WenCai + one alert", "09:30:00", ["blocked40"],
      expect_status="SUCCEEDED_DEGRADED", expect_tier="wencai", expect_calls=["kaipan", "wencai"],
      expect_alerts=["TIER_FALLBACK"], expect_failed_codes=["PROVIDER_BLOCKED"]),
    S("E", "Kaipan no data, WenCai truncated twice -> TD derived", "09:30:00", ["nodata40"], wencai=["partial30"],
      expect_status="SUCCEEDED_DEGRADED", expect_tier="td_derived",
      expect_calls=["kaipan", "wencai", "wencai", "td"], expect_failed_codes=["TIER_NO_DATA", "INTERNAL_ERROR", "INTERNAL_ERROR"],
      expect_times=["09:30:00", "09:30:00", "10:00:00", "10:00:00"]),
    S("F", "WenCai date mismatch -> TD derived silently", "09:30:00", ["nodata40"], wencai=["mismatch40"],
      expect_status="SUCCEEDED_DEGRADED", expect_tier="td_derived", expect_calls=["kaipan", "wencai", "td"],
      expect_failed_codes=["TIER_NO_DATA", "TIER_DATE_MISMATCH"]),
    S("G", "every rung fails -> FAILED_FINAL + one alert", "09:30:00", ["nodata40"], wencai=["det40"], td=["nodata40"],
      expect_status="FAILED_FINAL", expect_tier="td_derived", expect_calls=["kaipan", "wencai", "td"],
      expect_alerts=["FAILED_FINAL"], expect_failed_codes=["TIER_NO_DATA", "DETERMINISTIC_FAILURE", "TIER_NO_DATA"]),
    S("H", "started after late_until -> MISSED, nothing fetched", "15:00:30", ["ok"], expect_status="MISSED",
      expect_tier="", expect_calls=[], expect_alerts=["MISSED"]),
    S("I", "equal-or-better version exists -> skipped", "09:30:00", ["ok"], satisfied=True,
      expect_status="SKIPPED_SATISFIED", expect_tier="kaipan", expect_calls=["kaipan:satisfied"]),
    S("J", "ladder reordered in config (td first)", "09:30:00", ["ok"], reorder=True,
      expect_status="SUCCEEDED_DEGRADED", expect_tier="td_derived", expect_calls=["td"]),
    S("K", "Kaipan hangs -> killed by timeout -> WenCai", "09:30:00", ["hang"], expect_status="SUCCEEDED_DEGRADED",
      expect_tier="wencai", expect_calls=["kaipan", "wencai"], expect_failed_codes=["HANDLER_TIMEOUT"]),
]


# ---- fake handler: the real tier runner with scripted sources ------------------------------------------------------
def _fake_dir() -> Path:
    return Path(os.environ["POOL_FAKE_DIR"])


def _append(name: str, value: dict) -> None:
    with (_fake_dir() / name).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def scripted_deps(outcome: str):
    from engine_core.data_pipeline.limit_pool import PoolSourceError
    from engine_core.data_pipeline.limit_pool_td import MemoryPoolStore
    from engine_core.guardian.limit_pool_d1 import PoolDeps

    def gate(kind: str):
        errors = {
            "notready": PoolSourceError("KAIPAN_NOT_READY", "level_1_empty", exit_code=30, retryable=True),
            "blocked40": PoolSourceError("PROVIDER_BLOCKED", "kaipan_provider_blocked:403", exit_code=40),
            "nodata40": PoolSourceError("TIER_NO_DATA", "empty_response", exit_code=40),
            "partial30": PoolSourceError("TIER_PARTIAL", "pages_truncated", exit_code=30, retryable=True),
            "mismatch40": PoolSourceError("TIER_DATE_MISMATCH", "fake_other_day", exit_code=40)}
        if kind in errors:
            raise errors[kind]
        if kind == "det40":
            raise RuntimeError("fake_deterministic_bug")

    pages = {int(k): v for k, v in _fixture("kaipan_pool_20261026.json")["pages"].items()}
    wencai = _fixture("wencai_pool_20261026.json")["response"]
    td = _fixture("td_bars_20261026.json")

    def kaipan(day, args):
        gate(outcome)
        return {"pages": pages, "request_count": 6}

    def wencai_fn(day, **kwargs):
        gate(outcome)
        return {"records": wencai["data"], "reported_total": wencai["row_count"], "request_count": 1,
                "cookie_status": "used"}

    def bars(day):
        gate(outcome)
        return {s: [tuple(b) for b in series] for s, series in td["bars"].items()}

    return PoolDeps(kaipan=kaipan, wencai=wencai_fn, td_bars=bars, universe=lambda root, day: (td["names"], "fixture"),
                    td_store=lambda: MemoryPoolStore())


def fake_main(command: str) -> int:
    request = json.load(sys.stdin)
    if command == "alert":
        _append("alerts.jsonl", {"reason": request.get("reason"), "request": request.get("request")})
        print(json.dumps({"status": "sent"}))
        return 0
    if command == "gate":
        print(json.dumps({"status": "ready", "reason": "calendar_ready"}))
        return 0
    from engine_core.guardian.limit_pool_d1 import run_pool_tier

    args = request.get("args") or {}
    source = str(args.get("source"))
    script = list(args.get("script") or ["ok"])
    calls = _fake_dir() / "calls.jsonl"
    seen = sum(1 for line in calls.read_text().splitlines() if json.loads(line).get("provider") == source) \
        if calls.exists() else 0
    outcome = script[min(seen, len(script) - 1)]
    if outcome == "hang":
        _append("calls.jsonl", {"provider": source, "outcome": outcome, "now": request.get("now")})
        time.sleep(3600)
    root = _fake_dir() / "core-data"
    code, body = run_pool_tier(request, root, deps=scripted_deps(outcome))
    label = "satisfied" if body.get("error_code") == "ALREADY_SATISFIED" else outcome
    _append("calls.jsonl", {"provider": source, "outcome": label, "now": request.get("now"), "exit": code})
    print(json.dumps(body, ensure_ascii=False))
    return code


# ---- driver ---------------------------------------------------------------------------------------------------------
def build_source(schedule: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    event = copy.deepcopy(next(e for e in schedule["events"] if e["event_id"] == EVENT))
    profile = copy.deepcopy(schedule["profiles"][event["profile"]])
    scripts = {"kaipan": scenario.kaipan, "wencai": scenario.wencai, "td": scenario.td}
    for tier in profile["ladder"]:
        script = scripts[tier["args"]["source"]]
        tier["args"] = {**tier["args"], "script": script}
        if "hang" in script:
            tier["timeout"] = HANG_REAL_TIMEOUT  # the only real-time shortening; everything else stays as configured
    if scenario.reorder:
        profile["ladder"].reverse()
    global_config = copy.deepcopy(schedule["global"])
    global_config["heavy_max_load5"] = 1000.0
    return {"schema_version": 3, "revision": schedule.get("revision", 1), "timezone": schedule["timezone"],
            "global": global_config, "profiles": {event["profile"]: profile}, "events": [event]}


def run_scenario(scenario: Scenario, binary: Path, work: Path, schedule: dict[str, Any]) -> dict[str, Any]:
    directory = work / f"run-{time.time_ns()}-scenario-{scenario.key}"
    state_root, fake = directory / "state-root", directory / "fake"
    fake.mkdir(parents=True)
    if scenario.satisfied:   # a real FORMAL Kaipan version committed beforehand by the real runner
        from engine_core.guardian.limit_pool_d1 import run_pool_tier
        code, _body = run_pool_tier({"data_date": POOL_DAY, "args": {"source": "kaipan", "dataset": "limit_up_pool"}},
                                    fake / "core-data", deps=scripted_deps("ok"))
        assert code == 0
    source = build_source(schedule, scenario)
    (directory / "schedule-v3.json").write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config_path = directory / "config.json"
    migrate = subprocess.run([str(binary), "migrate", "--from", str(directory / "schedule-v3.json"), "--to",
                              str(config_path), "--expand"], cwd=directory, capture_output=True, text=True)
    if migrate.returncode:
        raise RuntimeError(migrate.stdout + migrate.stderr)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    (directory / "trade_dates.json").write_text(json.dumps([POOL_DAY, DATE]) + "\n", encoding="utf-8")
    config["calendars"] = {"trading": {"file": str(directory / "trade_dates.json")}}
    for name, command in ((config["events"][0]["handler"], "run"), ("alert", "alert")):
        entry = {"argv": [sys.executable, str(Path(__file__).resolve()), "--fake", command], "cwd": str(ROOT),
                 "env_allow": ["POOL_FAKE_DIR", "PYTHONPATH"], "max_output_bytes": 1048576, "supports_degrade": True}
        if command == "run":
            entry["gate_argv"] = [sys.executable, str(Path(__file__).resolve()), "--fake", "gate"]
        config["handlers"][name] = entry
    config["alerts"] = {"enabled": True}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "POOL_FAKE_DIR": str(fake),
           "PYTHONPATH": str(ROOT / "src"), "GOTOOLCHAIN": "local", "GOPROXY": "off"}
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
                    errors.append("advanced 200 steps without a terminal state")
                    break
                bt._advance(sock, STEP)
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


def evaluate(result: dict[str, Any]):
    scenario: Scenario = result["scenario"]
    errors = list(result["errors"])
    rows = bt.ledger_rows(result["state_root"])
    calls = gs._read_jsonl(result["fake"] / "calls.jsonl")
    alerts = gs._read_jsonl(result["fake"] / "alerts.jsonl")
    state = bt.final_states(rows).get(EVENT, {})
    got_calls = [c["provider"] + (":" + c["outcome"] if c["outcome"] == "satisfied" else "") for c in calls]
    got_alerts = [a.get("reason") for a in alerts]
    failed = [r["data"].get("error_code") for r in rows if r.get("type") == "TIER_FAILED"]
    got_times = [str(c.get("now") or "")[11:19] for c in calls]
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
    if scenario.expect_times and got_times != scenario.expect_times:
        errors.append(f"handler call times expected {scenario.expect_times} got {got_times}")
    # the data the handler wrote (real runner, real catalog in the scenario directory)
    committed = _committed_providers(result["fake"] / "core-data")
    expect_provider = {"kaipan": "kaipan_getHisBans", "wencai": "wencai", "td_derived": "td_derived"}.get(scenario.expect_tier)
    if scenario.expect_status.startswith("SUCCEEDED") and expect_provider and expect_provider not in committed:
        errors.append(f"expected a committed {expect_provider} partition for {POOL_DAY}, found {committed}")
    if scenario.expect_status in {"MISSED", "FAILED_FINAL"} and committed:
        errors.append(f"nothing may be written when the pool is missing, found {committed}")
    if scenario.key == "D" and alerts and "kaipan:PROVIDER_BLOCKED" not in json.dumps(alerts[0].get("request")):
        errors.append(f"D alert lacks the failed tier: {alerts[0].get('request')}")
    return errors, rows, {"state": state, "alerts": got_alerts, "calls": got_calls, "times": got_times,
                          "committed": committed, "advanced": result["advanced"]}


def _committed_providers(root: Path) -> list[str]:
    from engine_core.data_pipeline.storage import ArtifactCatalog
    versions = ArtifactCatalog(root).read().get("datasets", {}).get("limit_up_pool", {}).get("dates", {}).get(POOL_DAY, [])
    return [str(v.get("source")) for v in versions]


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
              f"times={extra['times']} alerts={extra['alerts']} committed={extra['committed']} steps={extra['advanced']}")
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
