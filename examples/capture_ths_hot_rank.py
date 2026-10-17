"""Capture the public THS hot rank into Core immutable storage and cache."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core.data_pipeline.jobs import JobRunner, RequestBudget  # noqa: E402
from engine_core.data_pipeline.providers.ths_hot_rank import ThsHotRankProvider  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", help="actual capture date; defaults to Shanghai calendar date")
    parser.add_argument("--artifact-root", type=Path,
                        default=Path(os.environ.get("CORE_DATA_ROOT", Path.home() / "core-data")))
    parser.add_argument("--execute", action="store_true", help="persist immutable version and Redis hot view")
    args = parser.parse_args(argv)
    captured_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="microseconds")
    trade_date = args.trade_date or captured_at[:10]
    client = None
    if args.execute:
        import redis
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            parser.error("REDIS_URL is required for --execute")
        client = redis.Redis.from_url(redis_url, socket_timeout=10, socket_connect_timeout=5)
    try:
        runner = JobRunner(artifact_root=args.artifact_root, redis=client,
                           budget=RequestBudget(min_interval_seconds=10))
        provider = ThsHotRankProvider(timeout=10)
        result = runner.run("hot_rank", trade_date, fetch=lambda _day: provider.fetch(100),
            now=captured_at, available_at=captured_at, as_of=captured_at,
            source="ths_hot_rank", publish=args.execute, dry_run=not args.execute,
            compute=lambda rows: [dict(row, captured_at=captured_at) for row in rows])
        print(f"CAPTURED_AT {captured_at}")
        print(f"TRADE_DATE {trade_date}")
        print(f"ROW_COUNT {result.manifest['row_count']}")
        print(f"SOURCE {result.manifest['source']}")
        print(f"VERSION {result.manifest.get('version', 'DRY_RUN')}")
        print(f"CACHE_PUBLISH {result.manifest.get('cache_publish', 'SKIPPED')}")
        return 0
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
