"""Read-only probe of the per-symbol daily caches (hot_rank, limit_truth).

Redis TYPE/HLEN/HSCAN/GET only.  The rows go through the pure
``normalize_symbol_cache_rows`` normalizer; nothing is written or repaired.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import normalize_symbol_cache_rows  # noqa: E402


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


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
    key = "cache:%s:%s" % (args.dataset, args.trade_date)
    meta_key = "cache:%s_meta:%s" % (args.dataset, args.trade_date)
    rows, decode_errors = [], 0
    digest = hashlib.sha256()
    try:
        key_type = _text(client.type(key))
        if key_type == "hash":
            n_before = int(client.hlen(key))
            items = sorted(client.hscan_iter(key), key=lambda kv: _text(kv[0]))
            n_after = int(client.hlen(key))
            for field, raw in items:
                digest.update(_text(field).encode() + b"\0" + _text(raw).encode() + b"\n")
                try:
                    value = json.loads(_text(raw))
                except ValueError:
                    decode_errors += 1
                    continue
                if isinstance(value, dict):
                    value.setdefault("symbol", _text(field))
                    rows.append(value)
                else:
                    decode_errors += 1
        else:
            n_before = n_after = 0
            items = []
        raw_meta = client.get(meta_key)
    finally:
        client.close()
    try:
        meta = json.loads(_text(raw_meta)) if raw_meta else None
    except ValueError:
        meta = {"valid_json": False}
    result = normalize_symbol_cache_rows(args.dataset, args.trade_date, rows)
    out = {
        "contract": "RealSymbolCacheProbeV1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "key": key, "meta_key": meta_key, "redis_type": key_type,
        "hlen_before": n_before, "hlen_after": n_after, "scan_items": len(items),
        "scan_consistent": n_before == n_after == len(items),
        "decode_errors": decode_errors, "payload_value_sha256": digest.hexdigest(),
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
