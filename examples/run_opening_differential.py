"""Compare Core opening facts with the deployed engine-next helpers.

This is a bounded, server-side evidence tool.  It reads the same Redis Q2
projection used by the existing opening probe and, optionally, the 0925 TD
projection for the auction change input.  The legacy module is imported only
when this command is run with an explicit deployed release root; it is never a
runtime dependency of ``engine_core`` or its unit tests.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    FreshnessPolicy,
    RedisQ2ProjectionAdapter,
    build_open_fact,
    build_opening_transition_fact,
    canonical_json,
    normalize_auction_change_bp_to_pct,
    semantic_hash,
)

try:  # Script execution resolves sibling examples directly.
    from run_real_auction_shadow import query_rows  # noqa: E402
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.run_real_auction_shadow import query_rows  # noqa: E402


def _strict_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("trade date must be strict YYYY-MM-DD")
    return value


def _symbols(value: str) -> tuple[str, ...]:
    result = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
    if not result or any(len(item) != 6 or not item.isdigit() for item in result):
        raise ValueError("symbols must be six-digit stock codes")
    return result


def _row_from_quote(quote: Any, symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timestamp_ms": quote.source_record_time_ms,
        "price_milli": quote.price_milli,
        "previous_close_milli": quote.pre_close_milli,
        "amount_2m_yuan": quote.amount_2m_yuan,
        "limit_state": quote.limit_state,
        "name": quote.name,
        "speed_1m": quote.speed_1m_bp,
    }


def _legacy_transition(
    legacy: ModuleType,
    row: Mapping[str, Any],
    auction_change_pct: Any,
) -> dict[str, Any]:
    """Reconstruct the deployed transition helper from its pure primitives."""

    opening = legacy._open_fact(row)
    auction = legacy._number(auction_change_pct)
    opening_change = opening.get("change_pct")
    delta = legacy._delta(opening_change, auction)
    return {
        "symbol": opening.get("symbol", ""),
        "auction_change_pct": auction,
        "opening_change_pct": opening_change,
        "delta_change_pct": delta,
        "delta_change_bp": legacy._change_bp(delta),
        "delta_state": legacy._state(delta),
        "sign_state": legacy._sign_state(auction, opening_change),
        "status": "available" if delta is not None else "unavailable",
    }


def compare_opening_rows(
    legacy: ModuleType,
    rows: Sequence[Mapping[str, Any]],
    *,
    auction_change_pct: Any = None,
) -> dict[str, Any]:
    """Compare one normalized row set without connecting to any source."""

    comparisons = []
    for row in rows:
        core_open = build_open_fact(row)
        legacy_open = legacy._open_fact(row)
        opening_exact = canonical_json(core_open) == canonical_json(legacy_open)
        item: dict[str, Any] = {
            "symbol": str(row.get("symbol") or ""),
            "opening_exact": opening_exact,
            "core_open": core_open,
            "legacy_open": legacy_open,
        }
        if auction_change_pct is not None:
            core_transition = build_opening_transition_fact(
                auction_change_pct,
                row,
            )
            legacy_transition = _legacy_transition(
                legacy,
                row,
                auction_change_pct,
            )
            item.update(
                {
                    "transition_exact": canonical_json(core_transition)
                    == canonical_json(legacy_transition),
                    "core_transition": core_transition,
                    "legacy_transition": legacy_transition,
                }
            )
        comparisons.append(item)
    return {
        "row_count": len(comparisons),
        "opening_exact": all(item["opening_exact"] for item in comparisons),
        "transition_exact": (
            all(item.get("transition_exact", True) for item in comparisons)
            if auction_change_pct is not None
            else None
        ),
        "comparisons": tuple(comparisons),
    }


def _comparison_status(comparison: Mapping[str, Any], *, transition_requested: bool) -> str:
    """Classify a differential without turning missing inputs into a match."""

    if comparison.get("opening_exact") is not True:
        return "MISMATCH"
    if transition_requested and comparison.get("transition_exact") is None:
        return "NON_COMPARABLE"
    if transition_requested and comparison.get("transition_exact") is not True:
        return "MISMATCH"
    return "MATCH"


def _aggregate_exact(
    comparisons: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> bool | None:
    """Aggregate exactness without turning non-comparable into mismatch.

    ``False`` means a comparable row actually differed, ``None`` means no
    mismatch was observed but at least one row lacked the requested input, and
    ``True`` means every row was comparable and exact.
    """

    values = [item.get(key) for item in comparisons]
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True


def _load_legacy(legacy_root: str) -> ModuleType:
    root = Path(legacy_root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("legacy root does not exist: " + str(root))
    sys.path.insert(0, str(root))
    return importlib.import_module("engine_next.runtime.open_confirmation")


def _auction_change_evidence_from_rows(
    rows: Sequence[Sequence[Any] | Mapping[str, Any]],
) -> tuple[float | None, str | None]:
    names = (
        "ts", "px_milli", "chg_bp", "match_amt_yuan",
        "rest_bid_amt_yuan", "rest_ask_amt_yuan", "limit_state",
        "symbol", "trade_date", "auction_tag",
    )
    saw_0925 = False
    for row in rows:
        if isinstance(row, Mapping):
            item = dict(row)
        else:
            if len(row) != len(names):
                raise ValueError("TD row has an unexpected column count")
            item = dict(zip(names, row))
        if str(item.get("auction_tag") or "").strip() != "0925":
            continue
        saw_0925 = True
        value = normalize_auction_change_bp_to_pct(item.get("chg_bp"))
        return value, None if value is not None else "0925_chg_bp_missing_or_invalid"
    return None, "0925_row_missing" if not saw_0925 else "0925_chg_bp_missing_or_invalid"


def _auction_change_from_rows(rows: Sequence[Sequence[Any] | Mapping[str, Any]]) -> float | None:
    """Return only the typed change value for small pure-function tests."""

    return _auction_change_evidence_from_rows(rows)[0]


def run_real_differential(
    legacy: ModuleType,
    client: Any,
    *,
    trade_date: str,
    symbols: tuple[str, ...],
    observed_at: datetime,
    stale_after_ms: int,
    td_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    projection = RedisQ2ProjectionAdapter(client).read(
        trade_date,
        observed_at,
        freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
    )
    comparisons = []
    for symbol in symbols:
        quote = projection.quotes.get(symbol)
        if quote is None:
            comparisons.append(
                {
                    "symbol": symbol,
                    "status": "UNAVAILABLE",
                    "reason": "symbol_not_in_q2_projection",
                }
            )
            continue
        row = _row_from_quote(quote, symbol)
        auction_change = None
        change_reason = None
        if td_config is not None:
            raw_rows = query_rows(symbol=symbol, trade_date=trade_date, **dict(td_config))
            auction_change, change_reason = _auction_change_evidence_from_rows(raw_rows)
        item = compare_opening_rows(
            legacy,
            (row,),
            auction_change_pct=auction_change,
        )["comparisons"][0]
        if auction_change is None and td_config is not None:
            item["reason"] = change_reason or "0925_change_unavailable"
        item["status"] = _comparison_status(item, transition_requested=td_config is not None)
        comparisons.append(item)
    return {
        "trade_date": trade_date,
        "symbols": symbols,
        "projection_status": projection.status,
        "projection_consistency": projection.consistency_status,
        "coverage": projection.coverage,
        "stale_symbols": projection.stale_symbols,
        "oldest_source_time_ms": projection.oldest_source_time_ms,
        "newest_source_time_ms": projection.newest_source_time_ms,
        "comparisons": tuple(comparisons),
        "transition_comparable_count": (
            sum(item.get("transition_exact") is not None for item in comparisons)
            if td_config is not None
            else None
        ),
        "transition_non_comparable_count": (
            sum(item.get("transition_exact") is None for item in comparisons)
            if td_config is not None
            else None
        ),
        "transition_mismatch_count": (
            sum(item.get("transition_exact") is False for item in comparisons)
            if td_config is not None
            else None
        ),
        "transition_non_comparable_reasons": (
            {
                reason: sum(1 for item in comparisons if item.get("reason") == reason)
                for reason in sorted(
                    {
                        str(item.get("reason"))
                        for item in comparisons
                        if item.get("transition_exact") is None
                    }
                )
            }
            if td_config is not None
            else {}
        ),
        "opening_exact": all(item.get("opening_exact", False) for item in comparisons),
        "transition_exact": (
            _aggregate_exact(comparisons, key="transition_exact")
            if td_config is not None
            else None
        ),
        "semantic_hash": semantic_hash(
            {
                "trade_date": trade_date,
                "symbols": symbols,
                "comparisons": tuple(comparisons),
            }
        ),
        "read_only": True,
        "side_effect_boundary": "Redis SMEMBERS/HGETALL and TD SELECT only; no writes, ACK, recovery, notification or effect",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbols", default="600519,000001,300750")
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--legacy-root", default=os.environ.get("ENGINE_NEXT_ROOT", "/home/exedev/services/engine-next/current"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--td-host", default=os.environ.get("TDENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--td-port", type=int, default=int(os.environ.get("TDENGINE_PORT", "6030")))
    parser.add_argument("--td-user", default=os.environ.get("TDENGINE_USER", "root"))
    parser.add_argument("--td-password", default=os.environ.get("TDENGINE_PASSWORD", "taosdata"))
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    args = parser.parse_args()
    trade_date = _strict_date(args.trade_date)
    symbols = _symbols(args.symbols)
    if args.stale_after_ms < 0:
        parser.error("stale-after-ms must be nonnegative")
    legacy = _load_legacy(args.legacy_root)
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
    td_config = {
        "host": args.td_host,
        "port": args.td_port,
        "user": args.td_user,
        "password": args.td_password,
        "database": args.td_database,
    }
    try:
        result = run_real_differential(
            legacy,
            client,
            trade_date=trade_date,
            symbols=symbols,
            observed_at=datetime.now(timezone.utc),
            stale_after_ms=args.stale_after_ms,
            td_config=td_config,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
