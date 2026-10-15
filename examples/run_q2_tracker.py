"""Poll Redis Q2 current values and publish isolated, expiring snapshots."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core.q2_tracker import Q2Tracker, run_polling


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("input JSON must be an object")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--input-json", help="offline fixture: q2, relations, baseline, high_limit_pool")
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.environ.get("REDIS_DB", "0")))
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--ttl", type=int, default=86400)
    parser.add_argument("--once", action="store_true", help="take one observation and exit")
    parser.add_argument("--dry-run", action="store_true", help="require --input-json; never connect to Redis")
    args = parser.parse_args(argv)
    if args.dry_run and not args.input_json:
        parser.error("--dry-run requires --input-json")
    if args.input_json:
        data = _load(args.input_json)
        tracker = Q2Tracker(data.get("relations", {}), data.get("baseline", {}),
                            high_limit_pool=data.get("high_limit_pool", {}),
                            plate_names=data.get("plate_names", {}))
        if args.dry_run:
            try:
                observed_at = datetime.fromisoformat(data["observed_at"])
            except (KeyError, TypeError, ValueError) as exc:
                parser.error("dry-run input needs an ISO observed_at")
            payload = tracker.observe(data.get("q2", {}), observed_at)
            print(json.dumps({"observed_at": payload["observed_at"], "market": payload["market"],
                              "plate_count": len(payload["plates"]),
                              "emergent": payload["emergent_summary"], "dry_run": True},
                             ensure_ascii=False, separators=(",", ":")))
            return 0
    else:
        parser.error("live mode requires --input-json containing relations, baseline and high_limit_pool")
    try:
        import redis
    except ImportError as exc:
        raise SystemExit("live mode requires redis-py") from exc
    client = redis.Redis(host=args.redis_host, port=args.redis_port, db=args.redis_db,
                         password=os.environ.get("REDIS_PASSWORD"), decode_responses=True,
                         socket_timeout=10)
    run_polling(client, tracker, args.trade_date, args.ttl, interval=args.interval, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
