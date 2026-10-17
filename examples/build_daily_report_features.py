#!/usr/bin/env python3
"""Materialize PIT-safe report features from Core-owned daily_kline."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from engine_core.daily_history import materialize_daily_report_features
from engine_core.data_pipeline.storage import ArtifactCatalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="daily_kline date used to build the feature snapshot")
    parser.add_argument("--asof", help="PIT cutoff; defaults to end of the selected date in Asia/Shanghai")
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--publish-redis", action="store_true")
    args = parser.parse_args()
    asof = args.asof or f"{args.date}T23:59:59+08:00"
    redis = None
    if args.publish_redis:
        import redis as redis_package
        redis = redis_package.Redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    result = materialize_daily_report_features(ArtifactCatalog(Path(args.artifact_root).expanduser()),
                                               trade_date=args.date, asof=asof, redis=redis)
    print(json.dumps({"job": "daily_report_features", "trade_date": args.date,
                      "row_count": result["row_count"], "available_at": result["available_at"],
                      "history_trade_dates": result["history_trade_dates"],
                      "cache_bytes": result["cache_bytes"],
                      "version": result["manifest"].get("version")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
