"""Run the existing Core Engine queue with real Redis auction projections.

This is the smallest Redis-to-Engine seam.  It intentionally keeps the
projection partial: Redis ``top_amount`` does not provide a verified previous
close or a canonical milli-price contract, so ``price_milli`` remains missing
instead of being guessed from ``price_yuan``.  The engine therefore proves
amount/bid lineage and missing propagation, not a full auction decision.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    DataStatus,
    DeterministicEngine,
    EngineSignal,
    MarketDataEnvelope,
    MarketStateReducer,
    PayloadKind,
    Q2ProjectionSnapshot,
    SignalKind,
    AuctionShadowStrategy,
    WindowManager,
    WindowSpec,
    canonical_json,
    read_redis_auction_projection,
    semantic_hash,
)
from engine_core.contracts import Provenance  # noqa: E402
from engine_core.q2 import Q2Quote  # noqa: E402


ANCHOR_CLOCKS = {"0920": time(9, 20), "0924": time(9, 24), "0925": time(9, 25)}
ANCHOR_ORDER = ("0920", "0924", "0925")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _strict_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade-date must be strict YYYY-MM-DD")
    return value


def _strict_symbol(value: str) -> str:
    if len(value) != 6 or not value.isdigit():
        raise ValueError("symbol must be a six-digit code")
    return value


def _epoch_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not value > 0:
        return None
    return int(value)


def _business_anchor_ms(trade_date: str, tag: str) -> int:
    return int(
        datetime.combine(
            date.fromisoformat(trade_date),
            ANCHOR_CLOCKS[tag],
            tzinfo=SHANGHAI,
        ).astimezone(timezone.utc).timestamp()
        * 1000
    )


def build_engine_projection(
    projection: Any,
    *,
    trade_date: str,
    symbol: str,
) -> Q2ProjectionSnapshot:
    """Adapt one Redis projection tag to the existing Engine input contract."""

    if projection.trade_date != trade_date:
        raise ValueError("projection trade_date does not match requested date")
    symbol = _strict_symbol(symbol)
    if projection.tag not in ANCHOR_ORDER:
        raise ValueError("unsupported auction tag")
    selected = {
        str(row["symbol"]): row
        for row in projection.rows
        if str(row.get("symbol") or "") == symbol
    }
    row = selected.get(symbol)
    missing = () if row is not None else (symbol,)
    source_time = _epoch_ms(projection.meta.get("ts"))
    state = {
        # Redis projection price_yuan has not yet been promoted to a verified
        # canonical milli-price contract.  Keep it in raw_fields only.
        "price_milli": None,
        "pre_close_milli": None,
        "auction_amount_yuan": row.get("auction_amount_yuan") if row else None,
        "auction_bid_amount_yuan": row.get("bid_amount_yuan") if row else None,
        "auction_ask_amount_yuan": row.get("ask_amount_yuan") if row else None,
    }
    raw_fields = dict(row) if row else {}
    quote = Q2Quote(
        symbol=symbol,
        market=None,
        name=None,
        price_milli=None,
        pre_close_milli=None,
        amount_yuan=None,
        volume_lots=None,
        source_record_time_ms=source_time,
        phase=None,
        limit_state=None,
        auction_amount_yuan=state["auction_amount_yuan"],
        auction_bid_amount_yuan=state["auction_bid_amount_yuan"],
        auction_ask_amount_yuan=state["auction_ask_amount_yuan"],
        amount_2m_yuan=None,
        amount_5m_yuan=None,
        speed_1m_bp=None,
        vector_3m_bp=None,
        vector_5m_bp=None,
        raw_fields=raw_fields,
    )
    if projection.status == "INVALID":
        status = DataStatus.INVALID
    elif row is None:
        status = DataStatus.MISSING
    else:
        status = DataStatus.PARTIAL
    source_times = (source_time,) if source_time is not None else ()
    content = {
        "contract_version": "RedisAuctionEngineProjectionV1",
        "projection_content_hash": projection.content_hash,
        "trade_date": trade_date,
        "tag": projection.tag,
        "symbol": symbol,
        "state": state,
        "missing_symbols": missing,
    }
    content_hash = semantic_hash(content)
    evidence_ref = f"{projection.evidence_ref}/{symbol}"
    provenance = Provenance(
        source_id="redis_auction_projection",
        source_kind="REDIS_PROJECTION",
        source_schema="RedisAuctionProjectionV1",
        source_trade_date=trade_date,
        effective_at_ms=source_time,
        observed_at_ms=projection.observed_at_ms,
        evidence_ref=evidence_ref,
        notes=("TOP_AMOUNT", "price_milli_unavailable"),
    )
    envelope = MarketDataEnvelope(
        envelope_id=semantic_hash(
            {
                "source_id": "redis_auction_projection",
                "trade_date": trade_date,
                "tag": projection.tag,
                "symbol": symbol,
                "content_hash": content_hash,
            }
        ),
        payload_kind=PayloadKind.L2_PROJECTION_SNAPSHOT,
        source_id="redis_auction_projection",
        schema_version=1,
        effective_time_ms=source_time,
        observed_time_ms=projection.observed_at_ms,
        generation=None,
        generation_kind="OBSERVATION_COHORT",
        payload={symbol: state},
        provenance=provenance,
    )
    return Q2ProjectionSnapshot(
        trade_date=trade_date,
        envelope=envelope,
        quotes={} if row is None else {symbol: quote},
        expected_symbols=(symbol,),
        missing_symbols=missing,
        stale_symbols=(),
        coverage=0.0 if row is None else 1.0,
        status=status,
        consistency_status="REDIS_AUCTION_TOP_AMOUNT",
        oldest_source_time_ms=min(source_times) if source_times else None,
        newest_source_time_ms=max(source_times) if source_times else None,
        content_hash=content_hash,
    )


def run_engine_shadow_from_redis(
    projections: Sequence[Any],
    *,
    trade_date: str,
    symbol: str,
) -> Mapping[str, Any]:
    """Run one bounded real Redis symbol through the public Engine queue."""

    by_tag = {projection.tag: projection for projection in projections}
    missing_tags = [tag for tag in ANCHOR_ORDER if tag not in by_tag]
    if missing_tags:
        raise ValueError("missing auction tags: " + ",".join(missing_tags))
    strategy = AuctionShadowStrategy(
        scope_id=symbol,
        start_trigger_id="AUCTION_0920",
        middle_trigger_id="AUCTION_0924",
        end_trigger_id="AUCTION_0925",
        previous_segment_id=f"redis_auction_{trade_date}_{symbol}_0920_to_0924",
        current_segment_id=f"redis_auction_{trade_date}_{symbol}_0924_to_0925",
        previous_coverage_status="PARTIAL",
        current_coverage_status="PARTIAL",
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction", 0, 10**15),)),
        strategy,
        session_id=trade_date,
        phase="AUCTION",
    )
    snapshots = {}
    for index, tag in enumerate(ANCHOR_ORDER):
        projection = build_engine_projection(
            by_tag[tag], trade_date=trade_date, symbol=symbol
        )
        snapshots[tag] = projection
        logical_time = _business_anchor_ms(trade_date, tag)
        engine.submit(
            EngineSignal(
                signal_id=f"redis-auction-market-{tag}-{symbol}",
                logical_time_ms=logical_time,
                signal_seq=index * 2 + 1,
                signal_kind=SignalKind.MARKET_UPDATE,
                payload=projection,
            )
        )
        engine.submit(
            EngineSignal(
                signal_id=f"redis-auction-timer-{tag}-{symbol}",
                logical_time_ms=logical_time,
                signal_seq=index * 2 + 2,
                signal_kind=SignalKind.TIMER,
                payload={"trigger_id": f"AUCTION_{tag}"},
            )
        )
    result = engine.run_until_empty()
    final_trace = result.strategy_results[-1].trace if result.strategy_results else {}
    fact = final_trace.get("auction_fact_shadow")
    fact_status = final_trace.get("fact_status")
    if isinstance(fact_status, DataStatus):
        fact_status = fact_status.value
    else:
        fact_status = getattr(fact_status, "value", fact_status)
    return {
        "contract_version": "RealRedisAuctionEngineShadowV1",
        "trade_date": trade_date,
        "symbol": symbol,
        "read_only": True,
        "side_effect_boundary": "Redis HGETALL + in-memory Core Engine only",
        "processed_signals": result.processed_signals,
        "strategy_result_count": len(result.strategy_results),
        "projection_statuses": {
            tag: snapshots[tag].status.value for tag in ANCHOR_ORDER
        },
        "projection_hashes": {
            tag: snapshots[tag].content_hash for tag in ANCHOR_ORDER
        },
        "source_time_range": {
            tag: {
                "oldest": snapshots[tag].oldest_source_time_ms,
                "newest": snapshots[tag].newest_source_time_ms,
            }
            for tag in ANCHOR_ORDER
        },
        "fact_status": fact_status,
        "fact_only": final_trace.get("decision_status") == "FACT_ONLY",
        "fact_content_hash": fact.get("content_hash") if isinstance(fact, Mapping) else None,
        "fact_trace": fact,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trade_date = _strict_date(args.trade_date)
    symbol = _strict_symbol(args.symbol)

    import redis  # type: ignore[import-not-found]

    observed_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
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
        projections = read_redis_auction_projection(
            client,
            trade_date=trade_date,
            observed_at_ms=observed_at_ms,
            tags=ANCHOR_ORDER,
            symbols=(symbol,),
        )
        result = dict(
            run_engine_shadow_from_redis(
                projections,
                trade_date=trade_date,
                symbol=symbol,
            )
        )
        result["read_observed_at_ms"] = observed_at_ms
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(canonical_json(result))
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
