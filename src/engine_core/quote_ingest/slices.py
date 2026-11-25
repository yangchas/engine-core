"""Point-in-time quote lookup across Core files and explicitly enabled fallbacks."""
from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def quote_state_at(symbols, asof: datetime, *, root=None, redis_reader=None, td_reader=None):
    """Resolve each symbol from Core immutable segments, then explicit legacy fallbacks."""
    if asof.tzinfo is None:
        raise ValueError("asof must be timezone-aware")
    wanted = {str(symbol).zfill(6) for symbol in symbols}
    result = {}
    if root is not None:
        base = Path(root) / "quote_stream" / f"trade_date={asof.date().isoformat()}"
        if base.exists():
            for path in sorted(base.glob("seg=*/rows.jsonl.gz")):
                with gzip.open(path, "rt", encoding="utf-8") as stream:
                    for line in stream:
                        row = json.loads(line)
                        stamp = row.get("ts")
                        if row.get("symbol") not in wanted or not stamp:
                            continue
                        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        available = row.get("available_at")
                        if not available:
                            continue
                        available_at = datetime.fromisoformat(available.replace("Z", "+00:00"))
                        if available_at <= asof and parsed <= asof and (row["symbol"] not in result or parsed > result[row["symbol"]][0]):
                            result[row["symbol"]] = (parsed, row)
    missing = wanted - result.keys()
    if missing and redis_reader is not None:
        for row in redis_reader(missing, asof):
            if row.get("symbol") in missing:
                result[row["symbol"]] = (None, row)
    missing = wanted - result.keys()
    if missing and td_reader is not None:
        for row in td_reader(missing, asof):
            if row.get("symbol") in missing:
                result[row["symbol"]] = (None, row)
    return {symbol: value[1] for symbol, value in result.items()}

