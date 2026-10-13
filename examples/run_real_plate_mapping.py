"""Read-only snapshot of the legacy symbol->plate mappings.

Redis HSCAN/HLEN only: ``market:stock_plate`` (symbol -> plate text) and
``config:plate_mapping:s2p`` (symbol -> JSON theme list).  The output is a new
sealed JSON; nothing is written back or repaired.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _scan(client, key):
    before = int(client.hlen(key))
    items = sorted(((_text(k), _text(v)) for k, v in client.hscan_iter(key)))
    after = int(client.hlen(key))
    return items, before == after == len(items)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import redis  # type: ignore[import-not-found]

    client = redis.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                         port=int(os.environ.get("REDIS_PORT", "6379")),
                         db=int(os.environ.get("REDIS_DB", "0")),
                         password=os.environ.get("REDIS_PASSWORD"),
                         socket_timeout=10, socket_connect_timeout=5)
    try:
        plate_items, plate_ok = _scan(client, "market:stock_plate")
        s2p_items, s2p_ok = _scan(client, "config:plate_mapping:s2p")
    finally:
        client.close()
    s2p, bad = {}, 0
    for symbol, raw in s2p_items:
        try:
            value = json.loads(raw)
        except ValueError:
            bad += 1
            continue
        if isinstance(value, list):
            s2p[symbol] = [str(x) for x in value]
        else:
            bad += 1
    out = {
        "contract": "RealPlateMappingSnapshotV1",
        "trade_date": args.trade_date,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "stock_plate": dict(plate_items), "stock_plate_scan_consistent": plate_ok,
        "s2p": s2p, "s2p_scan_consistent": s2p_ok, "s2p_invalid_count": bad,
        "stock_plate_sha256": hashlib.sha256(json.dumps(plate_items, ensure_ascii=False).encode()).hexdigest(),
        "side_effect_boundary": "Redis HLEN/HSCAN only; no write/repair",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({k: (len(v) if isinstance(v, dict) else v) for k, v in out.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
