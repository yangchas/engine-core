#!/usr/bin/env python3
"""Resumable Core-owned Baostock history and indicator import."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from engine_core.daily_history import BaostockRangeClient, backfill_daily_history
from engine_core.data_pipeline.storage import ArtifactCatalog


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--checkpoint-root")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1,
                        help="isolated Baostock connections; max 4")
    parser.add_argument("--read-timeout", type=float, default=30.0)
    parser.add_argument("--reindex-only", action="store_true", help="recompute indicators from stored bars; no network")
    parser.add_argument("--write", action="store_true", help="required to commit Core artifacts and Redis hot view")
    args = parser.parse_args(argv)
    if not args.write:
        parser.error("--write is required; this command writes immutable Core versions and core:cache hot view")
    root = Path(args.artifact_root).expanduser()
    checkpoint = Path(args.checkpoint_root).expanduser() if args.checkpoint_root else root / ".daily_history_backfill"
    try:
        import baostock as bs
    except ImportError as exc:
        parser.exit(2, f"backfill_daily_history: Baostock is required ({type(exc).__name__})\n")
    redis_client = None
    if os.getenv("REDIS_URL"):
        try:
            import redis
            redis_client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=2,
                                                socket_timeout=5, decode_responses=False)
        except ImportError:
            redis_client = None
    if args.reindex_only:
        from engine_core.daily_history import reindex_daily_indicators
        print(json.dumps(reindex_daily_indicators(ArtifactCatalog(root), start=args.start, end=args.end,
                                                  redis=redis_client), sort_keys=True))
        return 0
    client = BaostockRangeClient(bs, read_timeout=args.read_timeout)
    report = backfill_daily_history(ArtifactCatalog(root), start=args.start, end=args.end,
                                    checkpoint_root=checkpoint, client=client,
                                    batch_size=args.batch_size, workers=args.workers,
                                    read_timeout=args.read_timeout, redis=redis_client)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["completed_symbols"] == report["symbol_count"] and report["days_committed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
