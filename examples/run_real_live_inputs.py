"""Read every legacy Redis input once through the unified read-only layer.

Redis TYPE/HLEN/HSCAN/GET/TTL only.  Writes one new trace file; no row bodies
are stored, only counts, digests and the cache state a consumer would see.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import LiveInputCache, LiveInputReader  # noqa: E402

INPUTS = ("hot_plates", "hot_rank", "limit_truth", "yest_limit_pool",
          "stock_plate", "s2p", "anchor_archive")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-trade-date", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-rows", default="",
                        help="comma-separated input names whose raw (field, text) rows are stored")
    args = parser.parse_args()
    include_rows = {item.strip() for item in args.include_rows.split(",") if item.strip()}
    unknown = include_rows - set(INPUTS)
    if unknown:
        parser.error("unknown inputs: " + ",".join(sorted(unknown)))
    raw_rows = {}

    import redis  # type: ignore[import-not-found]

    client = redis.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                         port=int(os.environ.get("REDIS_PORT", "6379")),
                         db=int(os.environ.get("REDIS_DB", "0")),
                         password=os.environ.get("REDIS_PASSWORD"),
                         socket_timeout=10, socket_connect_timeout=5)
    reader = LiveInputReader(client, now_ms=lambda: int(time.time() * 1000))
    cache = LiveInputCache()
    rounds = []
    try:
        for index in range(max(1, args.repeat)):
            if index and args.interval_seconds:
                time.sleep(args.interval_seconds)
            states = []
            for name in INPUTS:
                read = reader.read(name, trade_date=args.trade_date,
                                   previous_trade_date=args.previous_trade_date)
                trace = dict(cache.refresh(read).as_trace())
                if name in include_rows and read.kind == "hash":
                    raw_rows[name] = [list(item) for item in read.rows]
                trace.update({"redis_type": read.redis_type, "scan_consistent": read.scan_consistent,
                              "ttl_seconds": read.ttl_seconds})
                states.append(trace)
            rounds.append(states)
    finally:
        client.close()
    out = {
        "contract": "RealLiveInputsProbeV1",
        "trade_date": args.trade_date,
        "previous_trade_date": args.previous_trade_date,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "rounds": rounds,
        "raw_rows": raw_rows,
        "side_effect_boundary": "Redis TYPE/HLEN/HSCAN/GET/TTL only; no write/repair",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    for trace in rounds[-1]:
        print(trace["name"], trace["status"], trace["row_count"], trace["reason"], trace["key"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
