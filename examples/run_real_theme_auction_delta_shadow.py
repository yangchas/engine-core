"""Run the theme auction-delta fact wheel on real Redis projections.

This is a bounded, read-only shadow.  It consumes the existing Core Redis
auction projection adapter for 0924/0925 and the already-published Redis theme
mapping hashes.  It does not query TD/network sources, repair Redis, consume
RabbitMQ, write anything, or emit strategy/effect output.

The result is explicitly scoped to the intersection of the two Redis
``TOP_AMOUNT`` projections.  Missing 0924/0925 keys or missing mapping rows are
not filled from another source.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    build_theme_auction_delta_facts,
    canonical_json,
    read_redis_auction_projection,
    resolve_legacy_theme_weights,
)


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _delta(current: Any, previous: Any) -> float | None:
    current_value = _finite_number(current)
    previous_value = _finite_number(previous)
    if current_value is None or previous_value is None:
        return None
    return current_value - previous_value


def _ratio(current: Any, previous: Any) -> float | None:
    current_value = _finite_number(current)
    previous_value = _finite_number(previous)
    if current_value is None or previous_value is None:
        return None
    if previous_value <= 0:
        return 0.0
    return current_value / previous_value


def build_normalized_theme_delta_rows(
    previous_rows: Sequence[Mapping[str, Any]],
    current_rows: Sequence[Mapping[str, Any]],
    *,
    evidence_ref_prefix: str,
) -> tuple[dict[str, Any], ...]:
    """Build canonical 0924->0925 rows without zero-filling missing fields."""

    previous_by_symbol = {str(row["symbol"]): row for row in previous_rows}
    current_by_symbol = {str(row["symbol"]): row for row in current_rows}
    rows: list[dict[str, Any]] = []
    for symbol in sorted(set(previous_by_symbol) & set(current_by_symbol)):
        previous = previous_by_symbol[symbol]
        current = current_by_symbol[symbol]
        current_amount = current.get("auction_amount_yuan")
        previous_amount = previous.get("auction_amount_yuan")
        current_change = _finite_number(current.get("change_ratio"))
        previous_change = _finite_number(previous.get("change_ratio"))
        change_delta = (
            (current_change - previous_change) * 100.0
            if current_change is not None and previous_change is not None
            else None
        )
        rows.append(
            {
                "symbol": symbol,
                "tag": "0925",
                "previous_tag": "0924",
                "amount_yuan": current_amount,
                "amount_delta_yuan": _delta(current_amount, previous_amount),
                "bid_amount_delta_yuan": _delta(
                    current.get("bid_amount_yuan"), previous.get("bid_amount_yuan")
                ),
                "change_pct_delta": change_delta,
                "amount_ratio": _ratio(current_amount, previous_amount),
                "evidence_ref": f"{evidence_ref_prefix.rstrip('/')}/{symbol}",
            }
        )
    return tuple(rows)


def _mapping_names(raw: Any) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def run_real_theme_shadow(
    *,
    client: Any,
    trade_date: str,
    symbols: Sequence[str] = (),
) -> Mapping[str, Any]:
    """Read Redis and return a deterministic, fact-only theme shadow."""

    observed_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    projections = read_redis_auction_projection(
        client,
        trade_date=trade_date,
        observed_at_ms=observed_at_ms,
        tags=("0924", "0925"),
        symbols=tuple(symbols),
    )
    by_tag = {projection.tag: projection for projection in projections}
    previous = by_tag.get("0924")
    current = by_tag.get("0925")
    if previous is None or current is None or previous.status != "READY" or current.status != "READY":
        return {
            "status": "UNAVAILABLE",
            "reason": "0924_or_0925_projection_missing_or_invalid",
            "trade_date": trade_date,
            "observed_at_ms": observed_at_ms,
            "projection_status": {
                tag: by_tag[tag].status for tag in sorted(by_tag)
            },
            "facts": (),
        }

    plate_map = dict(client.hgetall("market:stock_plate") or {})
    s2p_map = dict(client.hgetall("config:plate_mapping:s2p") or {})
    rows = build_normalized_theme_delta_rows(
        previous.rows,
        current.rows,
        evidence_ref_prefix=f"redis://market-auction/{trade_date}/0924-0925",
    )
    requested = {str(symbol) for symbol in symbols}
    if requested:
        rows = tuple(row for row in rows if row["symbol"] in requested)
    weights_by_symbol: dict[str, tuple[tuple[str, float], ...]] = {}
    for row in rows:
        symbol = row["symbol"]
        plate = plate_map.get(symbol)
        names = _mapping_names(s2p_map.get(symbol))
        weights_by_symbol[symbol] = resolve_legacy_theme_weights(plate, names)
    facts = build_theme_auction_delta_facts(rows, weights_by_symbol)
    return {
        "status": "OBSERVED" if facts else "UNAVAILABLE",
        "trade_date": trade_date,
        "observed_at_ms": observed_at_ms,
        "scope": "REDIS_TOP_AMOUNT_INTERSECTION",
        "projection_status": {"0924": previous.status, "0925": current.status},
        "row_count": len(rows),
        "mapping_count": sum(bool(value) for value in weights_by_symbol.values()),
        "mapping_missing_count": sum(not value for value in weights_by_symbol.values()),
        "facts": tuple(fact.as_mapping() for fact in facts),
        "read_only": True,
        "side_effect_boundary": "Redis HGETALL only; no fallback, repair, TD, Rabbit, notification or effect",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.environ.get("REDIS_DB", "0")))
    parser.add_argument("--redis-password", default=os.environ.get("REDIS_PASSWORD"))
    args = parser.parse_args()
    import redis  # type: ignore[import-not-found]

    client = redis.Redis(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=args.redis_password,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    try:
        symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
        result = run_real_theme_shadow(client=client, trade_date=args.trade_date, symbols=symbols)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(canonical_json(result))
        output.write("\n")
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
