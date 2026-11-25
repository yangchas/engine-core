"""Read completed Redis day views, falling back to a supplied TD loader."""
import json

from .redis_day_view import meta_key, view_key


def read_day_view(redis, family, trade_date, symbols, *, expected_rows=None, td_loader, ledger):
    symbols = list(symbols)
    metadata_key = meta_key(family, trade_date)
    reason = "REDIS_VIEW_MISSING"
    meta = {}
    for attempt in range(2):
        raw_meta = redis.get(metadata_key)
        meta = json.loads(raw_meta) if raw_meta is not None else {}
        if raw_meta is None:
            reason = "REDIS_VIEW_PARTIAL" if attempt else "REDIS_VIEW_MISSING"
            break
        if meta.get("status") != "COMPLETE" or (
            expected_rows is not None and meta.get("row_count") != expected_rows
        ):
            reason = "REDIS_VIEW_PARTIAL"
            break
        rows = {}
        key = view_key(family, trade_date)
        for start in range(0, len(symbols), 500):
            batch = symbols[start:start + 500]
            values = redis.hmget(key, batch)
            rows.update((symbol, json.loads(value)) for symbol, value in zip(batch, values) if value is not None)
        if redis.get(metadata_key) == raw_meta:
            return {"source": "redis", "reason": None, "rows": rows, "row_count": len(rows), "meta": meta}
        reason = "REDIS_VIEW_PARTIAL"

    rows = td_loader(family, trade_date, symbols)
    row_count = len(rows)
    ledger("VIEW_DEGRADED", {"family": family, "trade_date": trade_date,
                             "reason": reason, "row_count": row_count})
    return {"source": "td_fallback", "reason": reason, "rows": rows,
            "row_count": row_count, "meta": meta}
