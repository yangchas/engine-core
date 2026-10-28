"""Guardian handler body of the ``td_sync_daily_kline`` event (C567).

Event instead of a post-fetch step (a post-step also exists, best effort, for latency): the event owns its own retry
ladder/backoff/alert in schedule.json, a TD outage never fails a data fetch, and a restart or the next scheduled
instance catches up on its own because the sweep is idempotent and covers the whole lookback window.

Exit codes follow the guardian handler contract: 0 ok / 20 not applicable / 30 retryable / 40 deterministic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def _response(code: int, status: str, detail: Any, *, error_code: str = "", retryable: bool = False):
    body = {"contract": 1, "status": status, "error_code": error_code, "retryable": retryable, "artifacts": [],
            "detail": detail}
    return code, body


def run_td_sync(request: Mapping[str, Any], root: Path, *, dry_run: bool = False, store: Any = None):
    from .daily_kline_coverage import make_store, td_settings
    from ..data_pipeline.td_sink import (DailyKlineTdSink, TdUnavailable, TdVerifyError, sync_with_retry,
                                         validate_database_name)

    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    settings = td_settings(database=str(args["database"]) if args.get("database") else None)
    if settings is None:
        return _response(20, "not_applicable", "td_sink_not_configured", error_code="NOT_CONFIGURED")
    try:
        validate_database_name(settings["database"])
    except ValueError as exc:
        return _response(40, "failed", str(exc), error_code="CONFIG_ERROR")
    lookback = int(args.get("lookback_trading_days") or settings.get("lookback_trading_days") or 10)
    own = store is None
    store = store or make_store(settings)
    try:
        sink = DailyKlineTdSink(store, root)
        today = str(request.get("trade_date") or "") or None
        dates = sink.pending_dates(lookback, today)
        if not dates:
            return _response(20, "not_applicable", "no_catalog_days", error_code="NO_CATALOG_DAYS")
        if dry_run:
            return _response(0, "ok", {"dry_run": True, "database": settings["database"], "dates": dates})
        outcomes = sync_with_retry(sink, dates, attempts=int(args.get("attempts") or 1),
                                   backoff_seconds=float(args.get("backoff_seconds") or 1.0))
        detail = {"database": settings["database"], "outcomes": [o.as_dict() for o in outcomes]}
        _clear_unreadable(root, request)
        return _response(0, "ok", detail)
    except TdUnavailable as exc:
        return _response(30, "failed", {"error": str(exc)[:300]}, error_code="TD_UNAVAILABLE", retryable=True)
    except TdVerifyError as exc:
        return _response(40, "failed", {"error": str(exc)[:600]}, error_code="TD_VERIFY_FAILED")
    finally:
        if own:
            store.close()


def _clear_unreadable(root: Path, request: Mapping[str, Any]) -> None:
    """A successful sync proves TD is readable again: re-arm the one-shot unreadable alert."""
    try:
        from datetime import datetime
        from .daily_kline_coverage import CoverageResult, note_td_readability
        now = datetime.fromisoformat(str(request["now"])) if request.get("now") else datetime.now().astimezone()
        note_td_readability(root, CoverageResult({}, "td"), now)
    except Exception:
        pass
