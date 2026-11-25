"""Read-only probe of the per-symbol daily caches (hot_rank, limit_truth).

Redis TYPE/HLEN/HSCAN/GET only.  The rows go through the pure
``normalize_symbol_cache_rows`` normalizer; nothing is written or repaired.
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

from engine_core import LiveInputReader, normalize_symbol_cache_rows  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--dataset", choices=("hot_rank", "limit_truth"), required=True)
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.environ.get("REDIS_DB", "0")))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import redis  # type: ignore[import-not-found]

    client = redis.Redis(host=args.redis_host, port=args.redis_port, db=args.redis_db,
                         password=os.environ.get("REDIS_PASSWORD"),
                         socket_timeout=5, socket_connect_timeout=5)
    reader = LiveInputReader(client, now_ms=lambda: int(time.time() * 1000))
    try:
        read = reader.read(args.dataset, trade_date=args.trade_date)
    finally:
        client.close()
    key, meta_key = read.key, read.meta_key
    key_type = read.redis_type
    n_before, n_after = read.hlen_before or 0, read.hlen_after or 0
    items = read.rows
    decode_errors = 0
    rows = []
    for field, raw in items:
        try:
            value = json.loads(raw)
        except ValueError:
            decode_errors += 1
            continue
        if isinstance(value, dict):
            value.setdefault("symbol", field)
            rows.append(value)
        else:
            decode_errors += 1
    meta = read.meta
    result = normalize_symbol_cache_rows(args.dataset, args.trade_date, rows)
    out = {
        "contract": "RealSymbolCacheProbeV1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "key": key, "meta_key": meta_key, "redis_type": key_type,
        "hlen_before": n_before, "hlen_after": n_after, "scan_items": len(items),
        "scan_consistent": n_before == n_after == len(items),
        "decode_errors": decode_errors, "payload_value_sha256": read.content_sha256,
        "meta": meta, "result": result.as_trace(),
        "rows": list(result.rows),
        "side_effect_boundary": "Redis TYPE/HLEN/HSCAN/GET only; no write/repair",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in ("key", "redis_type", "scan_items", "scan_consistent", "decode_errors", "meta")} | {"result": out["result"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
