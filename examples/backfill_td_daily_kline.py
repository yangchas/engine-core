#!/usr/bin/env python3
"""Repair recent Core TD daily bars from Baostock through the per-symbol TD fill (C630): one atomic TD write per
symbol, single-flight Redis lease, fail-fast limits, completion marker published once coverage is met."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from dataclasses import replace
from pathlib import Path

from engine_core.data_pipeline.job_contract import JobResult, emit as emit_result, failure_from_exception
from engine_core.data_pipeline.td_sink import TdSourceRejected, TdUnavailable, TdVerifyError


def emit(result: JobResult) -> int:
    # None means the run failed before the statistic could be established.
    stats = {key: None for key in ("planned_days", "planned_symbols", "requested", "empty_returns",
             "rows_written", "incomplete_total", "upstream_empty_total", "listing_source",
             "published_partitions")}
    stats["publish_skipped"] = False
    return emit_result(replace(result, detail={**stats, **(result.detail or {})}))


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
        from engine_core.data_pipeline.daily_kline_fill import run_daily_kline_fill_from_root
        from engine_core.guardian.daily_kline_coverage import make_store, td_settings
        settings = td_settings(database=args.td_database)
        if settings is None or not os.environ.get("TD_CONFIG"):
            raise TdUnavailable("td_not_configured")
        store = make_store(settings)
        try:
            result = run_daily_kline_fill_from_root(
                args.artifact_root, args.date, store=store,
                args={"history_min_trading_days": int(settings.get("history_min_trading_days", 10)),
                      "baostock_no_progress_seconds": args.baostock_no_progress_seconds,
                      "total_timeout_seconds": float(settings.get("baostock_total_timeout_seconds", 6900))})
        finally:
            store.close()
        if result.get("status") == "BUSY":  # another trigger point holds the lease and is filling: nothing to do
            return emit(JobResult(1, "daily_kline", args.date, "not_applicable", 0, "baostock", "daily_kline",
                                  None, None, None, None, False, 0, 0.0, result))
        outcomes = result.get("partitions", [])
        target = next((row for row in outcomes if row.get("trade_date") == args.date), {})
        return emit(JobResult(
            contract=1, job="daily_kline", trade_date=args.date, status="degraded" if result.get("status") == "INCOMPLETE" else "ok", tier=0,
            source="baostock", dataset="daily_kline", version=target.get("version"),
            row_count=target.get("row_count"), coverage=None, error_code=None, retryable=False,
            attempts=int(result.get("attempted", 0)), elapsed_ms=0.0, detail=result))
    except TdUnavailable as exc:          # TD_UNAVAILABLE, or TD_RESOURCE_EXHAUSTED when its query pool is held (0x073a)
        return emit(JobResult(1, "daily_kline", args.date, "failed", 0, "baostock", "daily_kline", None,
                              None, None, exc.error_code, True, 1, 0.0, {"error": str(exc)[:200]}))
    except (TdVerifyError, TdSourceRejected) as exc:
        return emit(JobResult(1, "daily_kline", args.date, "failed", 0, "baostock", "daily_kline", None,
                              None, None, "TD_VERIFY_FAILED", False, 1, 0.0, {"error": str(exc)[:300]}))
    except Exception as exc:
        # the producers raise CoreError (BAOSTOCK_GAP_UNRESOLVED ...); anything else is classified by its type only
        detail = getattr(exc, "daily_kline_detail", None)
        if isinstance(detail, dict) and detail.get("status") == "INCOMPLETE":
            return emit(JobResult(1, "daily_kline", args.date, "degraded", 0, "baostock", "daily_kline",
                                  None, None, None, None, False, int(detail.get("requested", 0)), 0.0, detail))
        failure = failure_from_exception(exc, job="daily_kline", trade_date=args.date, dataset="daily_kline",
                                         source="baostock")
        detail = getattr(exc, "daily_kline_detail", None)
        if detail is not None:
            failure = replace(failure, detail={**failure.detail, **detail})
        return emit(failure)


if __name__ == "__main__":
    raise SystemExit(main())
