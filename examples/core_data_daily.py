#!/usr/bin/env python3
"""Run one Core data phase. Cron examples are printed, never installed."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date
from pathlib import Path

from engine_core.data_pipeline.ops import run_daily_phase
from engine_core.data_pipeline.job_contract import JobResult as ContractJobResult, emit as emit_job_result
from engine_core.data_pipeline.jobs import JobRunner, RequestBudget


def _sample_probe(args, root: Path, adapters) -> int:
    started = time.monotonic()
    try:
        fetch = adapters.get("daily_kline")
        if fetch is None:
            raise RuntimeError("daily_kline_adapter_unavailable")
        payload = fetch(args.date)
        payload = dict(payload) if isinstance(payload, dict) else {"rows": payload}
        timing = payload.get("baostock_timing") if isinstance(payload.get("baostock_timing"), dict) else {}
        timing_result = str(timing.get("result") or "ok")
        failure_code = None
        retryable = True
        if timing_result in {"budget_exhausted", "timeout", "error"}:
            failure_code = str(timing.get("error_code") or "BAOSTOCK_RETRYABLE_FAILURE")
        else:
            job = JobRunner(artifact_root=root,
                            budget=RequestBudget(max_requests=args.max_requests,
                                                 min_interval_seconds=0)).run(
                "daily_kline", args.date, fetch=lambda _day: payload, dry_run=True)
            coverage = job.manifest.get("quality", {}).get("coverage")
            if coverage is None or coverage < 0.98:
                failure_code = "BAOSTOCK_COVERAGE_LOW"
            else:
                return emit_job_result(ContractJobResult(
                    contract=1, job="daily_kline", trade_date=args.date, status="degraded",
                    tier=0, source="baostock", dataset="daily_kline", version=None,
                    row_count=len(job.rows), coverage=coverage, error_code=None, retryable=False,
                    attempts=1, elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                    detail={"business_status": "PROVISIONAL", "sample_scope": "symbols_file",
                            "quality": job.manifest.get("quality"),
                            "provider_attempts": job.manifest.get("provider_attempts", [])}))
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        permanent = lowered.startswith(("quality_invalid:", "schema_invalid:", "symbols_file_",
                                        "symbol_universe_artifact_required:"))
        retryable = not permanent
        if lowered.startswith("quality_invalid:") or lowered.startswith("schema_invalid:"):
            failure_code = message.split(":", 1)[1].upper()
        elif "timeout" in lowered:
            failure_code = "BAOSTOCK_TIMEOUT"
        else:
            failure_code = type(exc).__name__.upper()
    return emit_job_result(ContractJobResult(
        contract=1, job="daily_kline", trade_date=args.date, status="failed", tier=0,
        source="baostock", dataset="daily_kline", version=None, row_count=None,
        coverage=None, error_code=failure_code, retryable=retryable, attempts=1,
        elapsed_ms=round((time.monotonic() - started) * 1000, 3),
        detail={"sample_scope": "symbols_file"}))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=["premarket", "intraday", "postmarket"])
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--td-write", action="store_true")
    parser.add_argument("--adapter", help="module:function returning adapter mapping")
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--baostock-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--baostock-total-timeout-seconds", type=float,
                        help="optional wall-clock budget for the daily_kline task")
    parser.add_argument("--symbols-file", help="debug-only one-symbol-per-line Baostock cohort")
    parser.add_argument("--artifact-root", help="artifact root (required with --symbols-file)")
    parser.add_argument("--no-lock", action="store_true")
    args = parser.parse_args(argv)
    artifact_root_explicit = args.artifact_root is not None
    args.artifact_root = args.artifact_root or os.getenv("CORE_DATA_ROOT", "~/core-data")
    if args.td_write and not args.write:
        parser.error("--td-write requires --write")
    if args.symbols_file and args.phase != "postmarket":
        parser.error("--symbols-file is only valid with --phase postmarket")
    if args.symbols_file and (args.write or args.td_write):
        parser.error("--symbols-file cannot publish to Redis or TD")
    if args.write and not os.getenv("CORE_REDIS_URL"):
        parser.error("--write requires CORE_REDIS_URL")
    if args.symbols_file and not artifact_root_explicit:
        parser.error("--symbols-file requires an explicit --artifact-root")
    root = Path(args.artifact_root).expanduser()
    from examples.run_data_jobs import _default_adapters, _require_sample_artifact_root
    if args.symbols_file:
        try:
            root = Path(_require_sample_artifact_root(root))
        except ValueError as exc:
            parser.error(str(exc))
    adapters = _default_adapters(str(root), os.getenv("ENGINE_LEGACY_ROOT", "/home/exedev/repos/stock-situation-runtime"),
                                  args.max_requests, args.write,
                                  baostock_timeout_seconds=args.baostock_timeout_seconds,
                                  baostock_total_timeout_seconds=args.baostock_total_timeout_seconds,
                                  baostock_symbols_file=args.symbols_file)
    if args.adapter:
        if args.symbols_file:
            parser.error("--symbols-file cannot be combined with --adapter")
        module_name, sep, function = args.adapter.partition(":")
        if not sep: parser.error("--adapter must be module:function")
        adapters = getattr(__import__(module_name, fromlist=[function]), function)()
    if args.symbols_file:
        return _sample_probe(args, root, adapters)
    redis = None
    if args.write and os.getenv("CORE_REDIS_URL"):
        import redis as redis_package
        redis = redis_package.Redis.from_url(os.environ["CORE_REDIS_URL"], decode_responses=True)
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
    trade_dates = TradeCalendarStore(root).all_trading_dates() or None
    result = run_daily_phase(args.phase, args.date, root=root, adapters=adapters,
        trading_dates=trade_dates, write=args.write, redis=redis,
        td_adapter=adapters.get("td_writer"), td_write=args.td_write, lock=not args.no_lock)
    result["recommended_cron"] = [
        "50 8 * * 1-5 core_data_daily.py --phase premarket --write",
        "*/5 9-15 * * 1-5 core_data_daily.py --phase intraday --write",
        "40 17 * * 1-5 core_data_daily.py --phase postmarket --write",
        "30 18 * * 1-5 core_data_daily.py --phase postmarket --write",
        "0 20 * * 1-5 core_data_daily.py --phase postmarket --write",
    ]
    if args.symbols_file:
        result["sample_scope"] = "symbols_file"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["status"] not in {"READY", "SKIPPED_NON_TRADING_DAY"}:
        return 2
    return 10 if args.symbols_file else 0


if __name__ == "__main__":
    raise SystemExit(main())
