"""Read the legacy Redis auction projection through the Core adapter.

This is a bounded, read-only migration probe.  It calls only Redis HGETALL
through :func:`engine_core.read_redis_auction_projection`; it does not import
``engine-next``, read the anchor fallback, query TDengine, repair caches,
consume RabbitMQ, or emit any external effect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import read_redis_auction_projection  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--tags", default="0920,0924,0925")
    parser.add_argument("--symbols", default="600519,000001,000002")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import redis  # type: ignore[import-not-found]

    observed_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    try:
        projections = read_redis_auction_projection(
            client,
            trade_date=args.trade_date,
            observed_at_ms=observed_at_ms,
            tags=tuple(item.strip() for item in args.tags.split(",") if item.strip()),
            symbols=tuple(item.strip() for item in args.symbols.split(",") if item.strip()),
        )
        result = {
            "contract_version": "RealRedisAuctionProjectionProbeV1",
            "trade_date": args.trade_date,
            "observed_at_ms": observed_at_ms,
            "projections": [projection.as_mapping() for projection in projections],
            "read_only": True,
            "side_effect_boundary": "Redis HGETALL only; no GET anchor, fallback, repair, TD, Rabbit, notification or effect",
        }
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
