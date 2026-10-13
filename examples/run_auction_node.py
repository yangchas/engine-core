"""Start the read-only live auction node for one trading day."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # the node reuses examples.shadow_task008_redis_auction_session

from engine_core.auction_node import run_auction_node  # noqa: E402


DEFAULT_READ_TIMES = (
    "09:15:30,09:19:50,09:20:04,09:24:11,09:25:12,09:26:05,"
    "09:27:05,09:29:50,09:30:10,09:31:10,09:32:05,09:32:30"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--read-times", default=DEFAULT_READ_TIMES)
    parser.add_argument("--live-pool-previous-date")
    parser.add_argument("--td-rows", type=Path)
    parser.add_argument("--confirm-read-only", action="store_true", required=True)
    args = parser.parse_args()
    read_times = [item.strip() for item in args.read_times.split(",") if item.strip()]
    out_dir = args.out_root / ("auction-node-" + args.trade_date)
    ledger_path = args.out_root / "ledger" / "timers.jsonl"

    try:
        import redis  # type: ignore[import-not-found]
    except ImportError:
        parser.error("redis package is required for live source")
    client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True, socket_timeout=5, socket_connect_timeout=5,
    )
    try:
        result = run_auction_node(
            trade_date=args.trade_date, out_dir=out_dir, live_client=client,
            ledger_path=ledger_path, read_times=read_times,
            live_pool_previous_date=args.live_pool_previous_date,
            td_rows_path=args.td_rows, confirm_read_only=args.confirm_read_only,
        )
    except Exception as exc:
        client.close()
        raise RuntimeError("auction node failed: " + type(exc).__name__) from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
