#!/usr/bin/env python3
"""Live C615 acceptance: resumable Kaipan DDE sync into an existing core_dev TD database."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DATABASE = "core_dev_c590_632551160254"
DEFAULT_TRADE_DATE = "2026-10-29"
MIN_BATCH = 300
BOARD_MINIMUM = 20
BOARD_MINIMUMS = {"main": 20, "chinext": 20, "star": 20, "beijing": 2}
BSE_COVERAGE_SYMBOLS = ("920071", "920206")
HISTORY_TRADING_DAYS = 250
MAX_WORKERS = 2
KILL_AFTER_WRITTEN = 25
FULL_MARKET_SYMBOLS = 5200


def _board(symbol: str) -> str:
    if symbol.startswith(("688", "689")):
        return "star"
    if symbol.startswith(("300", "301")):
        return "chinext"
    if symbol.startswith(("4", "8", "9", "43", "83", "87", "88")):
        return "beijing"
    return "main"


def _active_symbols(root: Path, trade_date: str) -> list[str]:
    from engine_core.data_pipeline.daily_dde import _active_symbols as load_symbols

    return load_symbols(root, trade_date)


def _select_batch(active: list[str], watermarks: dict[str, str], trade_date: str) -> tuple[list[str], dict[str, int]]:
    active = sorted(set(active) | set(BSE_COVERAGE_SYMBOLS))
    # Keep the two live-verified Beijing symbols in the repeatable cohort even
    # after a previous acceptance run has already written their target rows.
    candidates = [symbol for symbol in active if watermarks.get(symbol, "") < trade_date
                  or symbol in BSE_COVERAGE_SYMBOLS]
    groups = {name: [symbol for symbol in candidates if _board(symbol) == name]
              for name in ("main", "chinext", "star", "beijing")}
    for name, members in groups.items():
        minimum = BOARD_MINIMUMS[name]
        if len(members) < minimum:
            raise RuntimeError(f"active_board_coverage_too_small:{name}:{len(members)}")
    selected = [symbol for name in ("main", "chinext", "star", "beijing")
                for symbol in groups[name][:BOARD_MINIMUMS[name]]]
    selected_set = set(selected)
    remaining = [symbol for symbol in candidates if symbol not in selected_set]
    selected.extend(remaining[:max(0, MIN_BATCH - len(selected))])
    if len(selected) < MIN_BATCH:
        raise RuntimeError(f"fresh_active_symbols_below_300:{len(selected)}")
    selected = selected[:MIN_BATCH]
    return selected, {name: sum(_board(symbol) == name for symbol in selected)
                      for name in ("main", "chinext", "star", "beijing")}


def _live_fetcher(timings: list[float]):
    from pykaipan import pykaipan
    from engine_core.data_pipeline.daily_dde import _configure_kaipan_request_timeout

    _configure_kaipan_request_timeout(pykaipan)
    original = pykaipan.getHisStockDDE

    def fetch(symbol: str, trade_date: str):
        started = time.monotonic()
        try:
            return original(symbol, trade_date)
        finally:
            timings.append(time.monotonic() - started)

    return fetch


def _sync(database: str, trade_date: str, symbols: list[str], root: Path) -> tuple[dict[str, Any], list[float]]:
    from engine_core.data_pipeline.daily_dde import run_daily_dde_sync
    from engine_core.data_pipeline.daily_dde import DdeSyncRetryableError

    timings: list[float] = []
    try:
        result = run_daily_dde_sync(
            symbols, trade_date, database=database, root=root, max_workers=MAX_WORKERS,
            attempts=3, history_trading_days=HISTORY_TRADING_DAYS, write_redis=False,
            skip_redis=True, fetcher=_live_fetcher(timings))
        return {"status": "ok", "summary": result}, timings
    except DdeSyncRetryableError as exc:
        return {"status": "retryable_error", "retryable": True, "summary": exc.summary}, timings


def _worker(args: argparse.Namespace) -> int:
    symbols = json.loads(args.symbols_json)
    outcome, _timings = _sync(args.database, args.date, symbols, Path(args.root))
    print("C615_WORKER_RESULT=" + json.dumps(outcome, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if outcome["status"] == "ok" else 75


def _interrupt_midrun(database: str, trade_date: str, symbols: list[str], root: Path) -> dict[str, Any]:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--database", database,
               "--date", trade_date, "--root", str(root), "--symbols-json",
               json.dumps(symbols, separators=(",", ":"))]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, bufsize=1)
    lines: list[str] = []
    progress = 0
    killed = False
    assert process.stdout is not None
    try:
        while True:
            line = process.stdout.readline()
            if line:
                lines.append(line.rstrip())
                match = re.search(r"DDE_PROGRESS .*written=(\d+)", line)
                if match:
                    progress = max(progress, int(match.group(1)))
                    if progress >= KILL_AFTER_WRITTEN and process.poll() is None:
                        process.send_signal(signal.SIGTERM)
                        killed = True
                        break
            elif process.poll() is not None:
                break
            elif time.monotonic() - started > 3 * 60 * 60:
                process.send_signal(signal.SIGTERM)
                raise TimeoutError("c615_initial_run_did_not_reach_progress")
        tail, _ = process.communicate(timeout=60)
        if tail:
            lines.extend(tail.splitlines())
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("c615_sigterm_process_did_not_exit")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    elapsed = time.monotonic() - started
    result = {"killed_by_sigterm": killed, "return_code": process.returncode,
              "progress_written": progress, "elapsed_seconds": round(elapsed, 3),
              "log_tail": lines[-12:]}
    if not killed or process.returncode != -signal.SIGTERM or progress < KILL_AFTER_WRITTEN:
        raise RuntimeError("c615_midrun_sigterm_not_observed:" + json.dumps(result, ensure_ascii=False))
    return result


def _target_rows(store: Any, database: str, trade_date: str, symbols: list[str]) -> int:
    stamp = int(__import__("engine_core.data_pipeline.daily_dde", fromlist=["_ts_us"])._ts_us(trade_date))
    total = 0
    for offset in range(0, len(symbols), 500):
        chunk = symbols[offset:offset + 500]
        quoted = ",".join("'" + symbol + "'" for symbol in chunk)
        rows = store._run(f"SELECT COUNT(*) FROM {database}.daily_dde_v1 WHERE ts={stamp} AND symbol IN ({quoted})")
        total += int(rows[0][0]) if rows else 0
    return total


def _rehearse(database: str, trade_date: str, symbol: str) -> dict[str, Any]:
    event_args = json.dumps({"symbols": [symbol], "target_date": trade_date, "database": database,
                             "history_trading_days": HISTORY_TRADING_DAYS, "max_workers": MAX_WORKERS,
                             "write_redis": False, "skip_redis": True}, separators=(",", ":"))
    command = [sys.executable, str(ROOT / "examples/rehearse_event.py"), "--event", "daily_dde_postclose",
               "--with", "daily_kline_close_1505", "baostock_probe_1730", "close_recap", "postmarket_publish",
               "--minutes", "1", "--td-database", database, "--timeout-minutes", "180",
               "--isolation-heavy-max-load5", "100",
               "--event-args-json", event_args]
    completed = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=181 * 60, check=False)
    lines = completed.stdout.splitlines()
    result_path = next((line.split("=", 1)[1] for line in lines if line.startswith("RESULT_JSON=")), None)
    result = json.loads(Path(result_path).read_text(encoding="utf-8")) if result_path else {}
    state = result.get("final_state") or {}
    ledger = result.get("event_ledger_rows") or []
    alerts = [item for item in ledger if isinstance(item, dict) and item.get("type") == "ALERT_OUT"]
    safety = result.get("safety_checks") or []
    failed_safety = [item for item in safety if not isinstance(item, dict) or item.get("status") != "PASS"]
    check = {"return_code": completed.returncode, "status": state.get("status"),
             "ledger_rows": len(ledger), "alerts": len(alerts), "failed_safety_checks": len(failed_safety),
             "isolation_heavy_max_load5": result.get("isolation_heavy_max_load5"),
             "result_json": result_path, "output_tail": lines[-12:]}
    if completed.returncode or state.get("status") != "SUCCEEDED" or alerts or not safety or failed_safety:
        raise RuntimeError("c615_rehearsal_failed:" + json.dumps(check, ensure_ascii=False))
    return check


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=DEFAULT_TRADE_DATE)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--root", default=os.environ.get("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--symbols-json", default="[]", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return _worker(args)

    report: dict[str, Any] = {"card": "C615", "trade_date": args.date, "database": args.database,
                              "started_at": datetime.now(TZ).isoformat(), "status": "FAIL", "checks": {}}
    store = None
    try:
        from engine_core.data_pipeline.daily_dde import TaosDdeStore

        root = Path(args.root).expanduser()
        store = TaosDdeStore(args.database)
        if not store.database_exists():
            raise RuntimeError("validation_database_must_already_exist:" + args.database)
        active = _active_symbols(root, args.date)
        initial_watermarks = store.latest_watermarks()
        symbols, board_counts = _select_batch(active, initial_watermarks, args.date)
        report["checks"]["batch"] = {"symbols": len(symbols), "board_counts": board_counts,
                                       "source": "Core active symbol_universe + live Kaipan DDE"}

        first = _interrupt_midrun(args.database, args.date, symbols, root)
        after_crash = store.latest_watermarks()
        completed_after_crash = [symbol for symbol in symbols
                                 if after_crash.get(symbol, "") >= args.date]
        if len(completed_after_crash) < KILL_AFTER_WRITTEN:
            raise RuntimeError("td_watermark_below_sigterm_progress:" + str(len(completed_after_crash)))
        report["checks"]["sigterm_resume"] = {**first, "td_completed_after_kill": len(completed_after_crash)}

        resumed_started = time.monotonic()
        resumed, timings = _sync(args.database, args.date, symbols, root)
        resumed_elapsed = time.monotonic() - resumed_started
        if resumed.get("status") != "ok":
            raise RuntimeError("c615_resume_not_complete:" + json.dumps(resumed, ensure_ascii=False)[:3000])
        result = resumed["summary"]
        final_watermarks = store.latest_watermarks()
        missing = [symbol for symbol in symbols if final_watermarks.get(symbol, "") != args.date]
        target_rows = _target_rows(store, args.database, args.date, symbols)
        skipped_on_resume = int(result.get("skipped_completed_symbols", 0))
        if missing or target_rows != len(symbols):
            raise RuntimeError("c615_td_latest_date_incomplete:" + json.dumps(
                {"missing": missing[:20], "missing_count": len(missing), "target_rows": target_rows},
                ensure_ascii=False))
        if skipped_on_resume < len(completed_after_crash):
            raise RuntimeError("c615_resume_did_not_skip_crash_watermarks")
        if result.get("readback_verified") is not True or result.get("status") != "ok":
            raise RuntimeError("c615_resume_readback_failed")
        report["checks"]["td_complete"] = {"symbols": len(symbols), "target_date_rows": target_rows,
                                            "readback_verified": result.get("readback_verified"),
                                            "skipped_on_resume": skipped_on_resume,
                                            "written_on_resume": result.get("written_symbols"),
                                            "requested_on_resume": result.get("candidate_symbols")}

        total_wall = first["elapsed_seconds"] + resumed_elapsed
        measured_fetch_symbols = sum(initial_watermarks.get(symbol, "") < args.date for symbol in symbols)
        if measured_fetch_symbols <= 0:
            raise RuntimeError("c615_no_unwatermarked_symbols_for_throughput_measurement")
        average = total_wall / measured_fetch_symbols
        extrapolation = average * FULL_MARKET_SYMBOLS
        report["checks"]["throughput"] = {"measured_batch_symbols": len(symbols),
                                          "measured_source_fetch_symbols": measured_fetch_symbols,
                                          "run_wall_seconds": round(total_wall, 3),
                                          "average_seconds_per_stock": round(average, 4),
                                          "full_market_symbols": FULL_MARKET_SYMBOLS,
                                          "full_market_extrapolation_seconds": round(extrapolation, 1),
                                          "schedule_timeout_safety_factor": 2,
                                          "timeout_seconds_recommended": int(extrapolation * 2)}

        bad_code = "999999"
        bad_result, _bad_timings = _sync(args.database, args.date, [bad_code], root)
        if bad_result.get("status") == "retryable_error":
            failures = bad_result.get("summary", {}).get("failed", [])
            if not bad_result.get("retryable") or not any(item.get("symbol") == bad_code for item in failures):
                raise RuntimeError("c615_invalid_symbol_failure_not_retryable")
            report["checks"]["real_invalid_symbol"] = {"symbol": bad_code, "status": "retryable_error",
                                                       "failure_count": len(failures), "retryable": True,
                                                       "error_code": "DDE_SYNC_INCOMPLETE_RETRYABLE"}
        else:
            raise RuntimeError("c615_invalid_symbol_did_not_fail_from_live_source")

        sample_symbol = next((symbol for symbol in active
                              if symbol not in symbols and initial_watermarks.get(symbol, "") < args.date), None)
        if sample_symbol is None:
            raise RuntimeError("c615_no_unwatermarked_symbol_for_rehearsal")
        report["checks"]["rehearsal"] = _rehearse(args.database, args.date, sample_symbol)
        report["status"] = "PASS"
    except Exception as exc:
        report["error"] = type(exc).__name__ + ":" + str(exc)[:4000]
    finally:
        if store is not None:
            store.close()
    report["finished_at"] = datetime.now(TZ).isoformat()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
