#!/usr/bin/env python3
"""Read-only status and timing check for configured Core data slices."""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from engine_core.slices import SliceProviders, SliceReader, previous_trade_date


SLICE_IDS = ("stock_name_map", "plate_registry", "plate_members", "daily_history",
             "auction_baseline", "yest_limit_pool", "limit_reason_history",
             "hot_plates_today", "hot_plates_yesterday", "hot_rank", "trading_calendar", "path_stats")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--root", default="~/core-data")
    parser.add_argument("--prev-trade-date")
    args = parser.parse_args(argv)
    redis = None
    try:
        import redis as redis_module
        redis = redis_module.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                            decode_responses=False)
    except Exception:
        pass
    providers = SliceProviders(artifact_root=Path(args.root).expanduser(), redis=redis)
    reader = SliceReader(providers.providers(), snapshot_id="check_slices")
    prior = args.prev_trade_date or previous_trade_date(providers.catalog, args.trade_date)
    for slice_id in SLICE_IDS:
        started = time.perf_counter()
        slice_asof = args.asof
        params = {}
        if prior:
            params["prior_trade_date"] = prior
            if slice_id in {"yest_limit_pool", "hot_plates_yesterday", "hot_plates_today"}:
                params["source_date"] = prior
        result = reader.get(slice_id, trade_date=args.trade_date, asof=slice_asof, **params)
        elapsed = time.perf_counter() - started
        print(f"{slice_id} source={result.meta['source']} status={result.meta['status']} "
              f"rows={len(result.rows)} elapsed={elapsed:.3f}s reason={result.meta.get('reason', '')} "
              f"requested_date={args.trade_date} used_date="
              f"{(result.meta.get('lineage') or [{}])[-1].get('used_date', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
