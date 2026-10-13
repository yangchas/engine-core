"""Read-only summary of the legacy ``market:auction:anchor:{date}`` archive.

Redis GET only.  Emits counts and value statistics, not a repair or a source
for Core computation: the point is to learn what the legacy anchor really holds
(row count, field names, how many change values are non-zero).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import LiveInputReader  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import redis  # type: ignore[import-not-found]

    client = redis.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                         port=int(os.environ.get("REDIS_PORT", "6379")),
                         db=int(os.environ.get("REDIS_DB", "0")),
                         password=os.environ.get("REDIS_PASSWORD"),
                         socket_timeout=5, socket_connect_timeout=5)
    reader = LiveInputReader(client, now_ms=lambda: int(time.time() * 1000))
    try:
        read = reader.read("anchor_archive", trade_date=args.trade_date)
    finally:
        client.close()
    key, ttl = read.key, read.ttl_seconds
    if read.redis_type != "none" and not read.scan_consistent:
        raise ValueError("anchor archive is not valid JSON")
    raw = None if read.redis_type == "none" else True
    out = {"contract": "RealAuctionAnchorArchiveProbeV1", "key": key, "ttl_seconds": ttl,
           "present": raw is not None}
    if raw is not None:
        payload = read.payload
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            rows = payload["rows"]
        elif isinstance(payload, dict):  # symbol -> row mapping (legacy archive)
            rows = [dict(value, symbol=key) for key, value in payload.items() if isinstance(value, dict)]
        else:
            rows = payload if isinstance(payload, list) else []
        names = Counter()
        for row in rows:
            if isinstance(row, dict):
                names.update(row.keys())
        pct = [row.get("change_pct", row.get("chg_pct")) for row in rows if isinstance(row, dict)]
        amt = [row.get("amount", row.get("auction_amount_yuan")) for row in rows if isinstance(row, dict)]
        out.update({
            "payload_shape": "symbol_map" if isinstance(payload, dict) and "rows" not in payload else type(payload).__name__,
            "source_counts": dict(Counter(str(r.get("source")) for r in rows)),
            "row_count": len(rows),
            "field_name_counts": dict(names),
            "change_pct_present": sum(v is not None for v in pct),
            "change_pct_nonzero": sum(isinstance(v, (int, float)) and v != 0 for v in pct),
            "amount_nonzero": sum(isinstance(v, (int, float)) and v != 0 for v in amt),
            "sample_first_row": rows[0] if rows else None,
            "rows_by_symbol": {str(r.get("symbol")): r for r in rows},
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
