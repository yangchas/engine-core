#!/usr/bin/env python3
"""Real-environment acceptance run of the D-1 limit-up pool ladder (C580, acceptance gate E).

A temporary LIVE-mode guardiand (real clock, real handler bridge ``examples/guardian_handler.py``) is started with the
repository's ``limit_pool_d1`` event and profile; only the trigger time is shifted to a few minutes ahead of now (and the
window end to one hour later).  Nothing is faked: the rung talks to the real Kaipan service / real read-only TD.

Isolation: short state root (``/tmp/c580st`` by default), a temporary data root (a copy of the live ``catalog.json`` plus
the calendar; the live manifests are only READ through their absolute paths), a throw-away TD database
``core_dev_c580_*`` (dropped at the end unless ``--keep-td``), alerts in dry-run (no mail).  The live guardiand and
``~/core-data/guardian-native`` are never touched; the child is stopped by its own pid.

Verification after the terminal state: ledger timeline, contract checker, TD read-back of every cell, idempotent
re-sync, independent cross-check against a pool derived from read-only TD bars.

Usage: python examples/guardian_limit_pool_real_run.py [--ladder kaipan_first|td_first] [--lead-minutes 2]
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
import guardian_selftest as gs  # noqa: E402  (isolated Go build)

SCHEDULE = ROOT / "src" / "engine_core" / "guardian" / "schedule.json"
LIVE_ROOT = Path("~/core-data").expanduser()
EVENT = "limit_pool_d1"
TERMINAL = {"SUCCEEDED", "SUCCEEDED_DEGRADED", "FAILED_FINAL", "SKIPPED_SATISFIED", "SKIPPED_NA", "MISSED"}
ENV_ALLOW = ["CORE_DATA_ROOT", "TD_CONFIG", "CORE_TD_DATABASE", "PYTHONPATH", "HOME", "PYTHONHASHSEED"]
TZ = ZoneInfo("Asia/Shanghai")


def previous_trading_day(calendar: list[str], today: str) -> str:
    earlier = [day for day in calendar if day < today]
    return earlier[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ladder", choices=("kaipan_first", "td_first"), default="kaipan_first")
    parser.add_argument("--lead-minutes", type=int, default=2)
    parser.add_argument("--state-root", type=Path, default=Path("/tmp/c580st"))
    parser.add_argument("--data-root", type=Path, default=Path("/tmp/c580data"))
    parser.add_argument("--work", type=Path, default=Path("/tmp/c580real"))
    parser.add_argument("--td-database", default="core_dev_c580_real")
    parser.add_argument("--keep-td", action="store_true")
    parser.add_argument("--fresh-pool", action="store_true",
                        help="drop the live limit_up_pool entries from the catalog COPY (default keeps the pre-C580 "
                             "4-column partitions so the new version is shown replacing them)")
    parser.add_argument("--wait-minutes", type=int, default=20)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    if not args.td_database.startswith("core_dev_c580_"):
        parser.error("--td-database must start with core_dev_c580_ (temporary databases only)")
    os.environ.setdefault("TD_CONFIG", "/home/exedev/validation/operator-runs/taos-cfg")
    for path in (args.state_root, args.data_root, args.work):
        shutil.rmtree(path, ignore_errors=True)
    args.work.mkdir(parents=True)
    args.data_root.mkdir(parents=True)
    now = datetime.now(TZ)
    today = now.date().isoformat()
    calendar = json.loads((LIVE_ROOT / "trade_dates.json").read_text(encoding="utf-8"))
    if today not in calendar:
        print(f"{today} is not a trading day in the calendar; nothing to run")
        return 2
    pool_day = previous_trading_day(calendar, today)
    catalog_copy = json.loads((LIVE_ROOT / "catalog.json").read_text(encoding="utf-8"))
    if args.fresh_pool:
        catalog_copy.get("datasets", {}).pop("limit_up_pool", None)
    (args.data_root / "catalog.json").write_text(json.dumps(catalog_copy), encoding="utf-8")   # live manifests are only read
    shutil.copy(LIVE_ROOT / "trade_dates.json", args.work / "trade_dates.json")
    at = (now + timedelta(minutes=args.lead_minutes)).replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = at + timedelta(minutes=60)
    if end.date() != at.date():
        print("too close to midnight for a one-hour window")
        return 2

    schedule = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    event = copy.deepcopy(next(e for e in schedule["events"] if e["event_id"] == EVENT))
    profile = copy.deepcopy(schedule["profiles"][event["profile"]])
    event.update(at=at.strftime("%H:%M:%S"), deadline=end.strftime("%H:%M:%S"), late_until=end.strftime("%H:%M:%S"))
    if args.ladder == "td_first":
        profile["ladder"].reverse()
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
           "CORE_TD_DATABASE": args.td_database, "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT),
           "PYTHONHASHSEED": "0"}
    log = (args.work / "guardiand.log").open("wb")
    print(f"clock now {now.isoformat(timespec='seconds')}; trigger shifted to {at.strftime('%H:%M:%S')} "
          f"(real event: 00:40:00); pool day {pool_day}; ladder {args.ladder}; data root {args.data_root}; "
          f"state root {args.state_root}; td database {args.td_database}", flush=True)
    proc = subprocess.Popen([str(binary), "run", "--config", str(config_path), "--state-root", str(args.state_root),
                             "--mode", "live"], cwd=str(args.work), env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    ledger = args.state_root / "ledger" / f"{today}.jsonl"
    summary: dict = {"pool_day": pool_day, "today": today, "ladder": args.ladder, "shifted_at": at.isoformat(),
                     "td_database": args.td_database, "data_root": str(args.data_root)}
    rc = 1
    try:
        deadline = time.monotonic() + args.wait_minutes * 60
        state: dict = {}
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                summary["error"] = f"guardiand exited with {proc.returncode}"
                break
            rows = gs._read_jsonl(ledger) if ledger.exists() else []
            for row in rows:
                data = row.get("data") or {}
                if row.get("type") == "INSTANCE_STATE" and (data.get("instance") or data.get("event_id")) == EVENT:
                    state = data.get("state") or {}
            if state.get("status") in TERMINAL:
                break
            time.sleep(3)
        time.sleep(2)
        rows = gs._read_jsonl(ledger) if ledger.exists() else []
        timeline = []
        for row in rows:
            data = row.get("data") or {}
            if row.get("type") in {"ATTEMPT_START", "ATTEMPT_END", "TIER_FAILED", "TIER_ATTEMPT"}:
                timeline.append({"type": row["type"], "at": row.get("ts") or data.get("starttime"), **{
                    k: data[k] for k in ("tier", "rung", "attempt", "exit_code", "error_code", "status") if k in data}})
        summary.update(final_state=state, timeline=timeline)
        print("final state:", json.dumps(state, ensure_ascii=False))
        summary["verify"] = verify(args, pool_day, state)
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
        if not args.keep_td:
            try:
                from engine_core.data_pipeline.limit_pool_td import PoolTaosStore
                store = PoolTaosStore(args.td_database)
                store.drop_database()
                summary["td_database_dropped"] = PoolTaosStore(args.td_database).schema_exists() is False
            except Exception as exc:  # noqa: BLE001
                summary["td_drop_error"] = f"{type(exc).__name__}:{exc}"
    text = json.dumps(summary, ensure_ascii=False, indent=1, default=str)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return rc


def verify(args, pool_day: str, state: dict) -> dict:
    from engine_core.data_pipeline.dataset_contract import check_partition
    from engine_core.data_pipeline import limit_pool as lp
    from engine_core.data_pipeline.limit_pool_td import DATA_COLUMNS, LimitPoolTdSink, PoolTaosStore, map_rows
    from engine_core.data_pipeline.providers.limit_pool_sources import read_td_bars, universe_names
    from engine_core.data_pipeline.storage import ArtifactCatalog

    out: dict = {"ok": False}
    expect_first = "kaipan" if args.ladder == "kaipan_first" else "td_derived"
    out["status_ok"] = state.get("status") in {"SUCCEEDED", "SUCCEEDED_DEGRADED"} and state.get("tier") == expect_first
    catalog = ArtifactCatalog(args.data_root)
    path = catalog.locate("limit_up_pool", pool_day)
    if path is None:
        out["error"] = "no partition committed"
        return out
    manifest = json.loads(path.read_text(encoding="utf-8"))
    rows = catalog.read_rows(path)
    out["manifest"] = {k: manifest.get(k) for k in ("provider", "source", "business_status", "degraded", "row_count", "version",
                                                    "provider_chain", "fetched_at", "available_at", "as_of", "source_dates")}
    out["manifest"]["quality"] = manifest.get("quality")
    out["manifest"]["path"] = str(path)
    report = check_partition(args.data_root, "limit_up_pool", pool_day)
    out["contract_checker"] = {"passed": report.passed, "counts": report.to_dict()["counts"],
                               "violations": [c["id"] + ":" + c["message"][:100] for c in report.to_dict()["violations"]]}
    store = PoolTaosStore(args.td_database)
    try:
        mapped, _dropped = map_rows(rows)
        back = store.read_day_rows(pool_day)
        mismatches = [(s, c) for s, want in mapped.items() for c in DATA_COLUMNS
                      if not ((back.get(s, {}).get(c) == want[c]) or (isinstance(want[c], float) and
                              back.get(s, {}).get(c) is not None and abs(back[s][c] - want[c]) < 1e-9))]
        versions = store.read_versions(pool_day)
        again = LimitPoolTdSink(store, args.data_root).sync_dates([pool_day])[0]
        out["td"] = {"rows_in_td": len(back), "rows_in_catalog": len(rows), "cell_mismatches": len(mismatches),
                     "versions": [{k: v.get(k) for k in ("version", "business_status", "provider", "row_count")} for v in versions],
                     "resync_status": again.status}
    finally:
        store.close()
    # independent cross-check: pool derived from read-only TD bars (different code path and different source)
    bars = read_td_bars(pool_day)
    names, st_source = universe_names(LIVE_ROOT, pool_day)
    derived = lp.build_td_derived(pool_day, bars, names=names, st_source=st_source)
    mine, theirs = {r["symbol"]: r["lb_days"] for r in rows}, {r["symbol"]: r["lb_days"] for r in derived.rows}
    out["cross_check_td_derived"] = {"rows": len(mine), "td_derived_rows": len(theirs),
                                     "only_in_run": sorted(set(mine) - set(theirs)),
                                     "only_in_td_derived": sorted(set(theirs) - set(mine)),
                                     "lb_days_differs": sorted(s for s in set(mine) & set(theirs) if mine[s] != theirs[s])}
    out["ok"] = bool(out["status_ok"] and report.passed and out["td"]["cell_mismatches"] == 0
                     and out["td"]["rows_in_td"] == len(rows) and out["td"]["resync_status"] == "unchanged")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
