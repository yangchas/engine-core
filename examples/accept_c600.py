#!/usr/bin/env python3
"""Live acceptance for C600: Kaipan DDE -> existing Core TD, Redis read-back and Guardian ledger."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_TD_DATABASE = "core_dev_c590_632551160254"
_ACCEPT_DATE = ""
# Verified against the official BSE listing pages and live Kaipan DDE on 2026-10-29.
# The Core BaoStock universe has no BSE symbols, so keep two current 920-series probes here.
BSE_COVERAGE_SYMBOLS = ("920071", "920206")


def _universe_symbols(root: Path, trade_date: str) -> list[str]:
    from engine_core.data_pipeline.storage import ArtifactCatalog

    catalog = ArtifactCatalog(root)
    manifest = catalog.locate("symbol_universe", trade_date) or catalog.locate("symbol_universe")
    if manifest is None:
        raise RuntimeError("symbol_universe_missing")
    rows = catalog.read_rows(manifest)
    active = sorted({str(row.get("symbol", "")).zfill(6) for row in rows
                     if str(row.get("trade_status", "")).strip() == "1"
                     and str(row.get("symbol", "")).strip().isdigit()})
    groups = {
        "star": [symbol for symbol in active if symbol.startswith(("688", "689"))],
        "chinext": [symbol for symbol in active if symbol.startswith(("300", "301"))],
        "beijing": [symbol for symbol in active if symbol.startswith("920")],
    }
    groups["beijing"].extend(symbol for symbol in BSE_COVERAGE_SYMBOLS
                              if symbol not in groups["beijing"])
    chosen: list[str] = []
    for name in ("star", "chinext", "beijing"):
        if len(groups[name]) < 2:
            raise RuntimeError("universe_board_coverage_missing:" + name)
        chosen.extend(groups[name][:2])
    chosen.extend(symbol for symbol in active if symbol not in chosen and not symbol.startswith(("688", "689", "300", "301", "920")))
    if len(chosen) < 20:
        raise RuntimeError("universe_has_fewer_than_20_active_symbols")
    return chosen[:20]


def _rehearse(event_id: str, symbols: list[str], database: str, minutes: int) -> dict:
    args_json = json.dumps({"symbols": symbols, "skip_redis": True, "target_date": _ACCEPT_DATE},
                           separators=(",", ":"))
    command = [sys.executable, str(ROOT / "examples/rehearse_event.py"), "--event", event_id,
               "--minutes", str(minutes), "--td-database", database,
               "--with", "daily_kline_close_1505", "--event-args-json", args_json]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    result_line = next((line.split("=", 1)[1] for line in completed.stdout.splitlines()
                        if line.startswith("RESULT_JSON=")), None)
    report: dict = {"exit_code": completed.returncode, "stdout_tail": completed.stdout.splitlines()[-12:],
                    "stderr_tail": completed.stderr.splitlines()[-12:]}
    if result_line:
        report["result"] = json.loads(Path(result_line).read_text(encoding="utf-8"))
    if completed.returncode:
        raise RuntimeError("guardian_rehearsal_failed:" + json.dumps(report, ensure_ascii=False)[:2000])
    state = report.get("result", {}).get("final_state") or {}
    safety = report.get("result", {}).get("safety_checks") or []
    if state.get("status") != "SUCCEEDED":
        raise RuntimeError("guardian_ledger_not_succeeded:" + str(state.get("status")))
    rows = report.get("result", {}).get("event_ledger_rows") or []
    alerts = [row for row in rows if isinstance(row, dict) and row.get("type") == "ALERT_OUT"]
    if alerts:
        raise RuntimeError("guardian_event_alerts:" + str(len(alerts)))
    safety_failures = [item for item in safety if not isinstance(item, dict) or item.get("status") != "PASS"]
    if not safety or safety_failures:
        failed = [(item.get("name"), item.get("status")) for item in safety_failures if isinstance(item, dict)]
        raise RuntimeError("guardian_rehearsal_safety_check_failed:" + json.dumps(failed))
    return {"exit_code": completed.returncode, "status": state.get("status"),
            "ledger_rows": len(rows), "alerts": len(alerts),
            "safety_checks": safety,
            "result_json": result_line}


def _sync_summary(result: dict) -> dict:
    comparison = result.get("legacy_comparison") or {}
    return ({key: result.get(key) for key in ("status", "database", "requested_symbols", "request_count",
                                               "written_symbols", "rows_written", "readback_verified",
                                               "readback_mismatches", "watermark_counts",
                                               "redis_readback_verified")}
            | {"known_missing": (result.get("known_missing") or [])[:5],
               "legacy_comparison": {key: comparison.get(key)
                                     for key in ("pairs", "equal", "different", "unmatched", "reasons")}})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="DDE source date; default is the read-only legacy daily_dde waterline")
    parser.add_argument("--database", default=DEFAULT_TD_DATABASE)
    parser.add_argument("--core-database", default="core_data")
    parser.add_argument("--minutes", type=int, default=3)
    args = parser.parse_args(argv)
    result = {"card": "C600", "status": "FAIL"}
    try:
        os.environ.setdefault("TD_CONFIG", "/home/exedev/validation/operator-runs/taos-cfg")
        from engine_core.data_pipeline.daily_dde import TaosDdeStore, run_daily_dde_sync

        if args.date:
            target_date = args.date
        else:
            legacy = TaosDdeStore(args.database)
            try:
                target_date = legacy.legacy_latest_date()
            finally:
                legacy.close()
            if not target_date:
                raise RuntimeError("legacy_daily_dde_waterline_missing")
        result["trade_date"] = target_date
        global _ACCEPT_DATE
        _ACCEPT_DATE = target_date
        symbols = _universe_symbols(Path(os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser(), target_date)

        dev = run_daily_dde_sync(symbols, target_date, database=args.database, max_workers=3,
                                 redis_ttl_seconds=86400, write_redis=True)
        if (dev.get("status") != "ok" or dev.get("requested_symbols") != 20
                or dev.get("readback_mismatches") or not dev.get("readback_verified")):
            raise RuntimeError("core_dev_td_readback_failed:" + json.dumps(_sync_summary(dev), ensure_ascii=False, default=str))
        core = run_daily_dde_sync(symbols, target_date, database=args.core_database, max_workers=3,
                                  redis_ttl_seconds=86400, write_redis=False)
        if (core.get("status") != "ok" or core.get("requested_symbols") != 20
                or core.get("readback_mismatches") or not core.get("readback_verified")):
            raise RuntimeError("core_data_td_readback_failed:" + json.dumps(_sync_summary(core), ensure_ascii=False, default=str))
        if len(symbols) > 20 or dev.get("written_symbols", 0) > 20 or core.get("written_symbols", 0) > 20:
            raise RuntimeError("acceptance_write_exceeded_20_symbols")
        if not dev.get("redis_readback_verified"):
            raise RuntimeError("redis_core_key_ttl_readback_failed")
        result.update({"symbols": len(symbols), "board_coverage": {
            "star": sum(symbol.startswith(("688", "689")) for symbol in symbols),
            "chinext": sum(symbol.startswith(("300", "301")) for symbol in symbols),
            "beijing": sum(symbol.startswith("920") for symbol in symbols)},
            "core_dev": dev, "core_data": core})
        rehearsal = _rehearse("daily_dde_postclose", symbols, args.database, args.minutes)
        result.update({"status": "PASS", "guardian": rehearsal})
    except Exception as exc:  # acceptance must report machine-readable failure without credentials
        result["error"] = type(exc).__name__ + ":" + str(exc)[:2500]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    _ACCEPT_DATE = ""
    raise SystemExit(main())
