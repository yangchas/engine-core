#!/usr/bin/env python3
"""Check Core artifact/status/Redis freshness without mutating data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from engine_core.data_pipeline.ops import dataset_health


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--redis", action="store_true", help="read Core Redis keys for consistency and TTL")
    args = parser.parse_args(argv)
    root = Path(args.artifact_root).expanduser()
    redis = None
    if args.redis:
        if not os.getenv("CORE_REDIS_URL"): parser.error("CORE_REDIS_URL is required for --redis")
        import redis as redis_package
        redis = redis_package.Redis.from_url(os.environ["CORE_REDIS_URL"], decode_responses=True)
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
    dates = TradeCalendarStore(root).all_trading_dates() or None
    report = dataset_health(root, redis=redis, trade_dates=dates)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["summary"] == "GREEN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
