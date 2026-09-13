"""Read real Redis Q2 and build opening facts for a bounded symbol set.

This is a server-only, read-only validation tool.  It reuses the existing
Redis Q2 key/field dialect through :class:`RedisQ2ProjectionAdapter`, then
calls the pure opening wheel.  It does not import the legacy reporting path,
write Redis/TD, consume Rabbit, repair data, or send notifications.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import RedisQ2ProjectionAdapter, build_open_fact, semantic_hash


def _date_text(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("trade date must be strict YYYY-MM-DD")
    return value


def _symbols(value: str) -> tuple[str, ...]:
    result = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
    if not result:
        raise ValueError("at least one symbol is required")
    if any(len(item) != 6 or not item.isdigit() for item in result):
        raise ValueError("symbols must be six-digit stock codes")
    return result


def build_real_opening_facts(
    client: Any,
    *,
    trade_date: str,
    symbols: tuple[str, ...],
    observed_at: datetime,
    stale_after_ms: int,
) -> dict[str, Any]:
    """Read one real Q2 projection and normalize selected opening facts."""

    if (
        isinstance(stale_after_ms, bool)
        or not isinstance(stale_after_ms, int)
        or stale_after_ms < 0
    ):
        raise ValueError("stale_after_ms must be a nonnegative integer")

    projection = RedisQ2ProjectionAdapter(client).read(
        trade_date,
        observed_at,
        stale_after_ms=stale_after_ms,
    )
    facts: dict[str, dict[str, Any]] = {}
    source_meta: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        quote = projection.quotes.get(symbol)
        if quote is None:
            facts[symbol] = {
                "symbol": symbol,
                "status": "unavailable",
                "reason": "symbol_not_in_q2_cohort",
            }
            continue
        mapping = quote.to_mapping()
        row = {
            "symbol": symbol,
            "timestamp_ms": quote.source_record_time_ms,
            "price_milli": quote.price_milli,
            "previous_close_milli": quote.pre_close_milli,
            "amount_2m_yuan": quote.amount_2m_yuan,
            "limit_state": quote.limit_state,
            "name": quote.name,
            # ``spd1m`` is the legacy source field; retain its value and do
            # not silently convert the unit while validating the opening rule.
            "speed_1m": quote.speed_1m_bp,
        }
        facts[symbol] = build_open_fact(row)
        source_meta[symbol] = {
            "source_record_time_ms": quote.source_record_time_ms,
            "field_errors": list(quote.field_errors),
            "raw_field_names": sorted(quote.raw_fields),
            "source_mapping_fields": sorted(mapping),
        }
    semantic_payload = {
        "trade_date": trade_date,
        "symbols": symbols,
        "facts": facts,
        "projection_status": projection.status,
        "coverage": projection.coverage,
        "stale_symbols": projection.stale_symbols,
    }
    return {
        "trade_date": trade_date,
        "observed_at": observed_at.isoformat(),
        "symbols": symbols,
        "projection_status": projection.status,
        "projection_consistency": projection.consistency_status,
        "coverage": projection.coverage,
        "freshness_status": (
            "FRESH" if not projection.stale_symbols else "STALE_OR_MIXED"
        ),
        "freshness_policy_stale_after_ms": stale_after_ms,
        "expected_symbol_count": len(projection.expected_symbols),
        "quote_count": len(projection.quotes),
        "missing_symbols": list(projection.missing_symbols),
        "stale_symbols": list(projection.stale_symbols),
        "oldest_source_time_ms": projection.oldest_source_time_ms,
        "newest_source_time_ms": projection.newest_source_time_ms,
        "projection_hash": projection.content_hash,
        "facts": facts,
        "source_meta": source_meta,
        "semantic_hash": semantic_hash(semantic_payload),
        "read_only": True,
        "side_effect_boundary": "Redis SMEMBERS/HGETALL only; no TD/Rabbit/write/repair/notification",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbols", default="600519,000001,300750")
    parser.add_argument(
        "--stale-after-ms",
        type=int,
        required=True,
        help="explicit production validation freshness budget in milliseconds",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trade_date = _date_text(args.trade_date)
    if args.stale_after_ms < 0:
        parser.error("stale-after-ms must be nonnegative")
    observed_at = datetime.now(timezone.utc)
    import redis  # type: ignore[import-not-found]

    client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    try:
        result = build_real_opening_facts(
            client,
            trade_date=trade_date,
            symbols=_symbols(args.symbols),
            observed_at=observed_at,
            stale_after_ms=args.stale_after_ms,
        )
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with args.output.open("x", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
