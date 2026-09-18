"""Run the first legacy theme-delta strategy rule on real Redis facts.

This is a bounded, read-only Shadow runner.  It reuses the existing Core
Redis projection path and theme mapping views, then applies only the explicit
legacy compatibility fact and the pure signal rule.  It does not write Redis
or TD, consume RabbitMQ, recover data, send notifications, or emit effects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_core import (  # noqa: E402
    build_legacy_theme_auction_delta_compat_facts,
    canonical_json,
    infer_legacy_theme_delta_signal,
    read_redis_auction_projection,
    resolve_legacy_theme_weights,
)
try:  # Script execution puts ``examples`` on sys.path.
    from run_real_theme_auction_delta_shadow import (  # type: ignore[import-not-found]  # noqa: E402
        _mapping_names,
        build_normalized_theme_delta_rows,
    )
except ImportError:  # Pytest imports it as ``examples.<module>``.
    from examples.run_real_theme_auction_delta_shadow import (  # noqa: E402
        _mapping_names,
        build_normalized_theme_delta_rows,
    )


def run_real_theme_delta_strategy_shadow(
    *,
    client: Any,
    trade_date: str,
    symbols: Sequence[str] = (),
) -> Mapping[str, Any]:
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
            "projection_status": {tag: by_tag[tag].status for tag in sorted(by_tag)},
            "facts": (),
            "read_only": True,
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
        weights_by_symbol[symbol] = resolve_legacy_theme_weights(
            plate_map.get(symbol), _mapping_names(s2p_map.get(symbol))
        )
    compat_facts = build_legacy_theme_auction_delta_compat_facts(rows, weights_by_symbol)
    output_facts = []
    for fact in compat_facts:
        signal = infer_legacy_theme_delta_signal(
            amount_delta_24_25=fact.amount_delta_24_25,
            bid_amount_delta_24_25=fact.bid_amount_delta_24_25,
            change_pct_delta_avg=fact.change_pct_delta_avg,
            amount_ratio_avg=fact.amount_ratio_avg,
        )
        item = dict(fact.as_mapping())
        item["signal"] = signal
        output_facts.append(item)
    return {
        "status": "OBSERVED" if output_facts else "UNAVAILABLE",
        "trade_date": trade_date,
        "observed_at_ms": observed_at_ms,
        "scope": "REDIS_TOP_AMOUNT_INTERSECTION",
        "projection_status": {"0924": previous.status, "0925": current.status},
        "row_count": len(rows),
        "mapping_count": sum(bool(value) for value in weights_by_symbol.values()),
        "mapping_missing_count": sum(not value for value in weights_by_symbol.values()),
        "signal_counts": dict(sorted(Counter(item["signal"] for item in output_facts).items())),
        "facts": tuple(output_facts),
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
        result = run_real_theme_delta_strategy_shadow(client=client, trade_date=args.trade_date, symbols=symbols)
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
