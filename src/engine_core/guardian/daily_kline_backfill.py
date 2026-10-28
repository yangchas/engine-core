"""Guardian entry point for the TD-watermark Baostock gap repair."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def run_daily_kline_backfill(root: str | Path, target: str, *, args: Mapping[str, Any] | None = None,
                             only_source_empty: bool = False) -> dict[str, Any]:
    from ..data_pipeline.daily_kline_gapfill import run_td_daily_kline_gapfill
    from .daily_kline_coverage import make_store, td_settings

    options = dict(args or {})
    settings = td_settings()
    if settings is None:
        raise RuntimeError("TD_DAILY_KLINE_BACKFILL_NOT_CONFIGURED")
    store = make_store(settings)
    try:
        return run_td_daily_kline_gapfill(
            root, target, store=store,
            history_min_trading_days=int(options.get("history_min_trading_days", 10)),
            total_timeout_seconds=float(options.get("total_timeout_seconds", 6900)),
            only_source_empty=only_source_empty,
            minimum_coverage=float(options.get("minimum_coverage", 0.90)),
            completion_policy=options.get("daily_kline_policy"),
        )
    finally:
        store.close()
