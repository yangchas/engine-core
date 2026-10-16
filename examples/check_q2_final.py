"""Read-only spot check for sealed q2 end-of-day values."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _text(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def _number(value: Any) -> float | None:
    try:
        if isinstance(value, bytes):
            value = value.decode("ascii")
        return float(value) if value not in (None, "", b"") else None
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def _timestamp(value: Any) -> datetime | None:
    stamp = _number(value)
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp / 1000, SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None


def inspect_q2(redis: Any, trade_date: str, sample_size: int = 10) -> dict[str, Any]:
    symbols = sorted({_text(item).zfill(6) for item in redis.smembers(f"q2:active:{trade_date}")})
    pipe = redis.pipeline(transaction=False)
    for symbol in symbols:
        pipe.hgetall(f"q2:{symbol}")
    q2_rows = pipe.execute() if symbols else []
    ready = redis.hgetall(f"cache:kline_ready:{trade_date}")
    ready_by_symbol = {_text(key).zfill(6): value for key, value in ready.items()}

    timestamps: list[datetime] = []
    candidates: list[dict[str, Any]] = []
    for symbol, raw in zip(symbols, q2_rows):
        values = {_text(key): value for key, value in (raw or {}).items()}
        stamp = _timestamp(values.get("ts"))
        if stamp is not None:
            timestamps.append(stamp)
        px = _number(values.get("px"))
        pc = _number(values.get("pc"))
        if stamp is None or stamp.date().isoformat() != trade_date or stamp.hour < 15 or px is None:
            continue
        ready_raw = ready_by_symbol.get(symbol, {})
        ready_values = {_text(key): value for key, value in (ready_raw or {}).items()}
        close = px / 1000
        expected_close = _number(ready_values.get("close"))
        candidates.append({
            "symbol": symbol,
            "q2_ts": stamp.isoformat(),
            "q2_close": close,
            "ready_close": expected_close,
            "close_diff": close - expected_close if expected_close is not None else None,
            "q2_pct_chg": ((px / pc - 1) * 100) if pc else None,
            "ready_pct_chg": _number(ready_values.get("pct_chg")),
            "q2_amount_yuan": _number(values.get("amt")),
            "ready_amount_yuan": _number(ready_values.get("amount")),
            "q2_volume_unit": "UNKNOWN",
        })

    eligible = sorted(stamp.isoformat() for stamp in timestamps
                      if stamp.date().isoformat() == trade_date)
    return {
        "trade_date": trade_date,
        "active_symbols": len(symbols),
        "q2_rows": len(q2_rows),
        "eligible_final_rows": len(candidates),
        "missing_or_stale_rows": len(symbols) - len(candidates),
        "ts_min": eligible[0] if eligible else None,
        "ts_max": eligible[-1] if eligible else None,
        "sample_vs_kline_ready": candidates[:max(0, sample_size)],
        "read_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--redis-url", default=os.getenv("LEGACY_REDIS_URL"))
    args = parser.parse_args(argv)
    if not args.redis_url:
        parser.error("--redis-url or LEGACY_REDIS_URL is required")

    try:
        import redis
    except ImportError as exc:
        parser.error(f"redis package unavailable: {exc}")
    client = redis.Redis.from_url(args.redis_url, decode_responses=False)
    print(json.dumps(inspect_q2(client, args.date, args.samples), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
