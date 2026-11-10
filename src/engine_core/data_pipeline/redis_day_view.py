"""Write trading-day views and their completion metadata to Redis."""
from datetime import datetime, time
import json
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from ..calendar import parse_trade_date
from .trade_calendar import nth_trade_date


def view_key(family, trade_date):
    return f"core:view:{family}:{parse_trade_date(trade_date).isoformat()}"


def meta_key(family, trade_date):
    return view_key(family, trade_date) + ":meta"


def expire_at(trade_date):
    day = nth_trade_date(trade_date, 5)
    return int(datetime.combine(day, time.min, ZoneInfo("Asia/Shanghai")).timestamp())


def write_day_view(redis, family, trade_date, rows: Mapping[str, Mapping], *, source, version, available_at, fields):
    deadline = expire_at(trade_date)
    key = view_key(family, trade_date)
    meta = meta_key(family, trade_date)
    fields = tuple(fields)
    payload = {symbol: json.dumps({field: row[field] for field in fields if field in row},
                                  sort_keys=True, separators=(",", ":"))
               for symbol, row in rows.items()}
    metadata = {"family": family, "trade_date": parse_trade_date(trade_date).isoformat(),
                "row_count": len(payload), "source": source, "version": version,
                "available_at": available_at, "written_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "status": "COMPLETE" if payload else "EMPTY"}
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    # Invalidate old metadata first, then clear the hash so removed rows cannot survive a rewrite.
    redis.delete(meta)
    redis.delete(key)
    pipe = redis.pipeline()
    if payload:
        pipe.hset(key, mapping=payload)
        pipe.execute()
    if redis.hlen(key) != len(payload):
        raise ValueError("day view row count mismatch")
    pipe.expireat(key, deadline)
    pipe.execute()
    try:
        pipe.set(meta, encoded)
        pipe.expireat(meta, deadline)
        pipe.execute()
    except Exception:
        try:
            redis.delete(meta)
        except Exception:
            pass
        raise
