#!/usr/bin/env python3
"""Real-environment acceptance run for C587: intraday checkpoint events and the startup_0830 dependency wait.

A temporary LIVE-mode guardiand (real clock, real handler bridge ``examples/guardian_handler.py``) runs ONE repository
event with only its trigger shifted to a few minutes ahead of now.  Nothing is faked: the gate and the handler read the
real Redis ``q2:*`` hashes (read-only) / real TD (read-only) / the real catalog's yesterday data.

  --event intraday_33   gate + capture of a Redis q2 checkpoint, read back and contract-checked
  --event startup_0830  prep_trade_day against a COPY of the live catalog (live manifests are only read)

Isolation: short state root, temporary data root (a copy of ``catalog.json`` + calendar), alerts dry-run.  The live
guardiand and ``~/core-data/guardian-native`` are never touched; the child is stopped by its own process group.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(ROOT / "src"))
import guardian_selftest as gs  # noqa: E402

SCHEDULE = ROOT / "src" / "engine_core" / "guardian" / "schedule.json"
LIVE_ROOT = Path("~/core-data").expanduser()
TERMINAL = {"SUCCEEDED", "SUCCEEDED_DEGRADED", "FAILED_FINAL", "SKIPPED_SATISFIED", "SKIPPED_NA", "MISSED"}
ENV_ALLOW = ["CORE_DATA_ROOT", "TD_CONFIG", "REDIS_URL", "PYTHONPATH", "HOME", "PYTHONHASHSEED"]
TZ = ZoneInfo("Asia/Shanghai")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event", default="intraday_33")
    parser.add_argument("--lead-minutes", type=int, default=2)
    parser.add_argument("--state-root", type=Path, default=Path("/tmp/c587st"))
    parser.add_argument("--data-root", type=Path, default=Path("/tmp/c587data"))
    parser.add_argument("--work", type=Path, default=Path("/tmp/c587real"))
    parser.add_argument("--wait-minutes", type=int, default=12)
    parser.add_argument("--span-minutes", type=int, default=6, help="deadline - trigger for the shifted event")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    os.environ.setdefault("TD_CONFIG", "/home/exedev/validation/operator-runs/taos-cfg")
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6380/0")
    for path in (args.state_root, args.data_root, args.work):
        shutil.rmtree(path, ignore_errors=True)
    args.work.mkdir(parents=True)
    args.data_root.mkdir(parents=True)
    now = datetime.now(TZ)
    today = now.date().isoformat()
    calendar = json.loads((LIVE_ROOT / "trade_dates.json").read_text(encoding="utf-8"))
    if today not in calendar:
        print(f"{today} is not a trading day")
        return 2
    (args.data_root / "catalog.json").write_text((LIVE_ROOT / "catalog.json").read_text(encoding="utf-8"), encoding="utf-8")
    shutil.copy(LIVE_ROOT / "trade_dates.json", args.work / "trade_dates.json")
    if (LIVE_ROOT / "trade_calendar").is_dir():
        shutil.copytree(LIVE_ROOT / "trade_calendar", args.data_root / "trade_calendar")
    at = (now + timedelta(minutes=args.lead_minutes)).replace(second=5, microsecond=0)
    end = at + timedelta(minutes=args.span_minutes)

    schedule = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    event = copy.deepcopy(next(e for e in schedule["events"] if e["event_id"] == args.event))
    profile = copy.deepcopy(schedule["profiles"][event["profile"]])
    event.update(at=at.strftime("%H:%M:%S"), deadline=end.strftime("%H:%M:%S"), phases=None)
    if event.get("late_until"):
        event["late_until"] = (end + timedelta(minutes=10)).strftime("%H:%M:%S")
    source = {"schema_version": 3, "revision": schedule.get("revision", 1), "timezone": schedule["timezone"],
              "global": schedule["global"], "profiles": {event["profile"]: profile}, "events": [event]}
    (args.work / "schedule-v3.json").write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    binary = gs._build_binary(args.work)
    config_path = args.work / "config.json"
    done = subprocess.run([str(binary), "migrate", "--from", str(args.work / "schedule-v3.json"), "--to", str(config_path),
                           "--expand"], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(done.stdout + done.stderr)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    bridge = ROOT / "examples" / "guardian_handler.py"
    for name, handler in config["handlers"].items():
        handler.update({"argv": [sys.executable, str(bridge), "run", "--handler", name.removeprefix("legacy_")],
                        "gate_argv": [sys.executable, str(bridge), "gate"], "cwd": str(ROOT), "env_allow": ENV_ALLOW})
    config["handlers"]["alert"] = {"argv": [sys.executable, str(bridge), "alert", "--dry-run"], "cwd": str(ROOT),
                                   "env_allow": ENV_ALLOW}
    config["calendars"] = {"trading": {"file": str(args.work / "trade_dates.json")}}
    config["alerts"] = {"enabled": True}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(Path.home()),
           "CORE_DATA_ROOT": str(args.data_root), "TD_CONFIG": os.environ["TD_CONFIG"],
           "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT), "PYTHONHASHSEED": "0"}
    if args.event.startswith("intraday"):
        env["REDIS_URL"] = os.environ["REDIS_URL"]      # read-only q2 reads; the startup run gets no Redis at all
    log = (args.work / "guardiand.log").open("wb")
    print(f"clock now {now.isoformat(timespec='seconds')}; event {args.event} trigger shifted {event['at']} "
          f"(real {next(e for e in schedule['events'] if e['event_id'] == args.event)['at']}), deadline {event['deadline']}; "
          f"data root {args.data_root}; state root {args.state_root}", flush=True)
    proc = subprocess.Popen([str(binary), "run", "--config", str(config_path), "--state-root", str(args.state_root),
                             "--mode", "live"], cwd=str(args.work), env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    ledger = args.state_root / "ledger" / f"{today}.jsonl"
    summary: dict = {"event": args.event, "today": today, "shifted_at": at.isoformat(), "deadline": end.isoformat(),
                     "data_root": str(args.data_root)}
    rc = 1
    try:
        stop = time.monotonic() + args.wait_minutes * 60
        state: dict = {}
        while time.monotonic() < stop:
            if proc.poll() is not None:
                summary["error"] = f"guardiand exited with {proc.returncode}"
                break
            rows = gs._read_jsonl(ledger) if ledger.exists() else []
            for row in rows:
                data = row.get("data") or {}
                if row.get("type") == "INSTANCE_STATE" and (data.get("instance") or data.get("event_id")) == args.event:
                    state = data.get("state") or {}
            if state.get("status") in TERMINAL:
                break
            time.sleep(3)
        time.sleep(2)
        rows = gs._read_jsonl(ledger) if ledger.exists() else []
        summary["final_state"] = state
        summary["gate_polls"] = [{"at": (r.get("data") or {}).get("polled_at"), "status": (r.get("data") or {}).get("status"),
                                  "reason": (r.get("data") or {}).get("reason"), "row_count": (r.get("data") or {}).get("row_count")}
                                 for r in rows if r.get("type") == "GATE_POLL"][-40:]
        summary["gate_poll_summary"] = [r.get("data") for r in rows if r.get("type") == "GATE_POLL_SUMMARY"]
        summary["ledger_types"] = sorted({r.get("type") for r in rows})
        summary["alerts"] = [(r.get("data") or {}).get("key") for r in rows if r.get("type") == "ALERT_OUT"]
        print("final state:", json.dumps(state, ensure_ascii=False))
        summary["verify"] = verify_checkpoint(args, today, event) if args.event.startswith("intraday") else verify_startup(args, today)
        rc = 0 if summary["verify"].get("ok") else 1
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)       # our own child's process group only
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        log.close()
    text = json.dumps(summary, ensure_ascii=False, indent=1, default=str)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return rc


def verify_checkpoint(args, today: str, event: dict) -> dict:
    from engine_core.data_pipeline import intraday_checkpoint as core
    from engine_core.data_pipeline.storage import ArtifactCatalog
    out: dict = {"ok": False}
    cutoff = event["at"][:5] + ":00"
    catalog = ArtifactCatalog(args.data_root)
    path = core.locate_checkpoint(catalog, today, cutoff)
    if path is None:
        out["error"] = "no checkpoint partition committed for " + cutoff
        return out
    manifest = json.loads(path.read_text(encoding="utf-8"))
    rows = catalog.read_rows(path)
    cut = core.cutoff_datetime(today, cutoff)
    upper = cut + timedelta(seconds=manifest["tolerance_seconds"])
    stamps = [datetime.fromisoformat(r["ts"]) for r in rows]
    out["manifest"] = {k: manifest.get(k) for k in ("source", "provider", "provider_chain", "provider_attempts", "checkpoint",
                                                    "window", "as_of", "captured_at", "available_at", "business_status",
                                                    "degraded", "row_count", "quality", "source_dates", "version")}
    out["readback"] = {"rows": len(rows), "all_ts_in_window": all(cut <= t <= upper for t in stamps),
                       "all_dates_equal_trade_date": all(r["date"] == today for r in rows),
                       "symbols_unique": len({r["symbol"] for r in rows}) == len(rows),
                       "sample": rows[:2]}
    # independent re-read of Redis right now: a symbol's later tick must never be mislabelled as this checkpoint
    import redis
    client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    sample = [r["symbol"] for r in rows[:200]]
    pipe = client.pipeline(transaction=False)
    for s in sample:
        pipe.hgetall("q2:" + s)
    live = dict(zip(sample, pipe.execute()))
    moved = sum(1 for r in rows[:200] if int(live[r["symbol"]].get("ts", 0)) > upper.timestamp() * 1000)
    out["independent_redis_now"] = {"sampled": len(sample), "symbols_whose_live_ts_is_already_past_window": moved,
                                    "note": "expected: they moved on; the partition kept the in-window value"}
    out["ok"] = bool(rows and out["readback"]["all_ts_in_window"] and out["readback"]["all_dates_equal_trade_date"]
                     and out["readback"]["symbols_unique"] and len(rows) >= 4000)
    return out


def verify_startup(args, today: str) -> dict:
    out: dict = {"ok": False}
    from engine_core.data_pipeline.storage import ArtifactCatalog
    catalog = ArtifactCatalog(args.data_root)
    entry = catalog.read().get("datasets", {})
    out["theme_snapshot_dates"] = sorted(entry.get("node__theme_snapshot", {}).get("dates", {}))[-3:]
    out["stock_theme_assignment_dates"] = sorted(entry.get("stock_theme_assignment", {}).get("dates", {}))[-3:]
    out["ok"] = today in entry.get("stock_theme_assignment", {}).get("dates", {})
    return out


if __name__ == "__main__":
    raise SystemExit(main())
