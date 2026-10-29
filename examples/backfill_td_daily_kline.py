#!/usr/bin/env python3
"""Repair recent Core TD daily bars from Baostock, committing each date to catalog and TD together."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from engine_core.data_pipeline.job_contract import JobResult, emit
from engine_core.data_pipeline.td_sink import TdSourceRejected, TdUnavailable, TdVerifyError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--td-database")
    parser.add_argument("--baostock-no-progress-seconds", type=float, default=300.0,
                        help="fail retryably when Baostock produces no range data or checkpoint progress")
    args = parser.parse_args(argv)
    if args.baostock_no_progress_seconds <= 0:
        parser.error("--baostock-no-progress-seconds must be positive")
    try:
        date.fromisoformat(args.date)
        from engine_core.data_pipeline.daily_kline_gapfill import run_td_daily_kline_gapfill
        from engine_core.guardian.daily_kline_coverage import make_store, td_settings
        settings = td_settings(database=args.td_database)
        if settings is None or not os.environ.get("TD_CONFIG"):
            raise TdUnavailable("td_not_configured")
        store = make_store(settings)
        try:
            result = run_td_daily_kline_gapfill(args.artifact_root, args.date, store=store,
                                                history_min_trading_days=int(
                                                    settings.get("history_min_trading_days", 10)),
                                                total_timeout_seconds=float(
                                                    settings.get("baostock_total_timeout_seconds", 6900)),
                                                no_progress_timeout_seconds=args.baostock_no_progress_seconds)
        finally:
            store.close()
        outcomes = result.get("partitions", [])
        target = next((row for row in outcomes if row.get("trade_date") == args.date), {})
        return emit(JobResult(
            contract=1, job="daily_kline", trade_date=args.date, status="ok", tier=0,
            source="baostock", dataset="daily_kline", version=target.get("version"),
            row_count=target.get("row_count"), coverage=None, error_code=None, retryable=False,
            attempts=int(result.get("range_queries", 0)), elapsed_ms=0.0, detail=result))
    except TdUnavailable as exc:
        return emit(JobResult(1, "daily_kline", args.date, "failed", 0, "baostock", "daily_kline", None,
                              None, None, "TD_UNAVAILABLE", True, 1, 0.0, {"error": str(exc)[:200]}))
    except (TdVerifyError, TdSourceRejected) as exc:
        return emit(JobResult(1, "daily_kline", args.date, "failed", 0, "baostock", "daily_kline", None,
                              None, None, "TD_VERIFY_FAILED", False, 1, 0.0, {"error": str(exc)[:300]}))
    except Exception as exc:  # provider range timeout/connection errors are retryable under the existing ladder
        message = str(exc)
        retryable = any(token in message.upper() for token in
                        ("TIMEOUT", "CONNECTION", "UNAVAILABLE", "SOCKET", "BUDGET_EXHAUSTED",
                         "GAP_UNRESOLVED"))
        return emit(JobResult(1, "daily_kline", args.date, "failed", 0, "baostock", "daily_kline", None,
                              None, None, "BAOSTOCK_RANGE_FAILED", retryable, 1, 0.0,
                              {"error": type(exc).__name__ + ":" + message[:200]}))


if __name__ == "__main__":
    raise SystemExit(main())
