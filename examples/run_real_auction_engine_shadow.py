"""Run the bounded auction fact shadow through the public Engine signal path.

The TD reader is intentionally the existing bounded read-only helper.  Each
auction projection row is adapted to the canonical market projection contract,
then submitted as ``MARKET_UPDATE`` followed by a same-time ``TIMER``.  This
proves the real projection can traverse the Engine's public queue without
adding a new production adapter or changing the Rabbit/Redis/TD owners.

This remains a validation harness: it is single-symbol, read-only, and emits
fact-only output.  It is not a production full-market loop or an arrival-order
replay.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    AuctionShadowStrategy,
    DataStatus,
    DeterministicEngine,
    EngineSignal,
    FrozenDataBundle,
    MarketDataEnvelope,
    MarketStateReducer,
    PayloadKind,
    Q2ProjectionSnapshot,
    SignalKind,
    WindowManager,
    WindowSpec,
    canonical_json,
    semantic_hash,
)
from engine_core.contracts import Provenance  # noqa: E402
from engine_core.q2 import Q2Quote  # noqa: E402

try:  # Script execution resolves sibling examples directly.
    from run_real_auction_shadow import (  # type: ignore
        ANCHOR_ORDER,
        build_shadow_from_rows,
        build_snapshots_from_rows,
        query_rows,
    )
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.run_real_auction_shadow import (  # type: ignore
        ANCHOR_ORDER,
        build_shadow_from_rows,
        build_snapshots_from_rows,
        query_rows,
    )


def _projection_from_snapshot(
    snapshot: Any,
    *,
    trade_date: str,
    symbol: str,
) -> Q2ProjectionSnapshot:
    """Adapt one immutable auction snapshot to the Engine market projection.

    The adapter copies only the canonical fields already accepted by the
    auction fact wheel.  It does not reinterpret ``amount_yuan`` or fabricate
    volume, and it keeps the TD source timestamp as the projection's source
    range.
    """

    state = dict(snapshot.symbol_states[symbol])
    source_time = snapshot.source_observation_metadata.get("source_record_time_ms")
    if not isinstance(source_time, int):
        raise ValueError("auction snapshot is missing source_record_time_ms")
    quote = Q2Quote(
        symbol=symbol,
        market=None,
        name=None,
        price_milli=state.get("price_milli"),
        pre_close_milli=None,
        amount_yuan=None,
        volume_lots=None,
        source_record_time_ms=source_time,
        phase=None,
        limit_state=None,
        auction_amount_yuan=state.get("auction_amount_yuan"),
        auction_bid_amount_yuan=state.get("auction_bid_amount_yuan"),
        auction_ask_amount_yuan=state.get("auction_ask_amount_yuan"),
        amount_2m_yuan=None,
        amount_5m_yuan=None,
        speed_1m_bp=None,
        vector_3m_bp=None,
        vector_5m_bp=None,
        raw_fields=state,
    )
    status = (
        DataStatus.READY
        if snapshot.completeness == "READY"
        else DataStatus.PARTIAL
        if snapshot.completeness == "PARTIAL"
        else DataStatus.MISSING
    )
    evidence_ref = snapshot.evidence_refs[0] if snapshot.evidence_refs else None
    provenance = Provenance(
        source_id="td_auction_projection",
        source_kind="TD_PROJECTION",
        source_schema="auction_snapshot_v2",
        source_trade_date=trade_date,
        effective_at_ms=source_time,
        observed_at_ms=source_time,
        evidence_ref=evidence_ref,
    )
    envelope = MarketDataEnvelope(
        envelope_id="engine-shadow:%s:%s:%s" % (trade_date, symbol, snapshot.trigger_id),
        payload_kind=PayloadKind.L2_PROJECTION_SNAPSHOT,
        source_id="td_auction_projection",
        schema_version=1,
        effective_time_ms=source_time,
        observed_time_ms=source_time,
        generation=None,
        generation_kind="OBSERVATION_COHORT",
        payload={symbol: state},
        provenance=provenance,
    )
    return Q2ProjectionSnapshot(
        trade_date=trade_date,
        envelope=envelope,
        quotes={symbol: quote},
        expected_symbols=(symbol,),
        missing_symbols=(),
        stale_symbols=(),
        coverage=snapshot.coverage,
        status=status,
        consistency_status="TD_AUCTION_PROJECTION",
        oldest_source_time_ms=source_time,
        newest_source_time_ms=source_time,
        content_hash=semantic_hash(
            {
                "trade_date": trade_date,
                "symbol": symbol,
                "trigger_id": snapshot.trigger_id,
                "source_record_time_ms": source_time,
                "state": state,
            }
        ),
    )


def run_engine_shadow(
    *,
    rows: list[tuple[Any, ...]],
    trade_date: str,
    symbol: str,
) -> dict[str, Any]:
    """Run one real-row set through Engine and return a stable trace."""

    snapshots = build_snapshots_from_rows(rows, trade_date=trade_date, symbol=symbol)
    strategy = AuctionShadowStrategy(
        scope_id=symbol,
        start_trigger_id="AUCTION_0920",
        middle_trigger_id="AUCTION_0924",
        end_trigger_id="AUCTION_0925",
        previous_segment_id="auction_%s_%s_0920_to_0924" % (trade_date, symbol),
        current_segment_id="auction_%s_%s_0924_to_0925" % (trade_date, symbol),
        previous_coverage_status=(
            "READY"
            if snapshots["0920"].completeness == "READY"
            and snapshots["0924"].completeness == "READY"
            else "PARTIAL"
        ),
        current_coverage_status=(
            "READY"
            if snapshots["0924"].completeness == "READY"
            and snapshots["0925"].completeness == "READY"
            else "PARTIAL"
        ),
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction", 0, 10**15),)),
        strategy,
        session_id=trade_date,
        phase="AUCTION",
    )
    for index, tag in enumerate(ANCHOR_ORDER):
        snapshot = snapshots[tag]
        logical_time = snapshot.logical_time_ms
        projection = _projection_from_snapshot(
            snapshot, trade_date=trade_date, symbol=symbol
        )
        engine.submit(
            EngineSignal(
                "auction-market-%s" % tag,
                logical_time,
                index * 2 + 1,
                SignalKind.MARKET_UPDATE,
                projection,
            )
        )
        engine.submit(
            EngineSignal(
                "auction-timer-%s" % tag,
                logical_time,
                index * 2 + 2,
                SignalKind.TIMER,
                {"trigger_id": snapshot.trigger_id},
            )
        )
    result = engine.run_until_empty()
    if len(result.strategy_results) != len(ANCHOR_ORDER):
        raise RuntimeError("unexpected number of Engine strategy results")
    final_result = result.strategy_results[-1]
    direct = build_shadow_from_rows(rows, trade_date=trade_date, symbol=symbol)
    direct_shadow = direct["shadow"]
    engine_shadow = final_result.trace.get("auction_fact_shadow")
    if not isinstance(engine_shadow, Mapping):
        raise RuntimeError("Engine did not emit auction_fact_shadow")
    return {
        "contract_version": "RealAuctionEngineShadowV1",
        "trade_date": trade_date,
        "symbol": symbol,
        "read_only": True,
        "side_effect_boundary": "TD SELECT + in-memory Engine only",
        "processed_signals": result.processed_signals,
        "strategy_result_count": len(result.strategy_results),
        "engine_fact_status": engine_shadow["status"],
        "engine_fact_only": engine_shadow["decision_status"] == "FACT_ONLY",
        "engine_fact_content_hash": engine_shadow["content_hash"],
        "direct_fact_content_hash": direct_shadow["content_hash"],
        "semantic_hash_equal": engine_shadow["content_hash"] == direct_shadow["content_hash"],
        "engine_evidence_hash": engine_shadow["evidence_hash"],
        "direct_evidence_hash": direct_shadow["evidence_hash"],
        "engine_strategy_evidence_refs": final_result.evidence_refs,
        "snapshot_source_time_range": {
            tag: {
                "oldest": snapshots[tag].source_observation_metadata["source_record_time_ms"],
                "newest": snapshots[tag].source_observation_metadata["source_record_time_ms"],
            }
            for tag in ANCHOR_ORDER
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--td-host", default=os.environ.get("TDENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--td-port", type=int, default=int(os.environ.get("TDENGINE_PORT", "6030")))
    parser.add_argument("--td-user", default=os.environ.get("TDENGINE_USER", "root"))
    parser.add_argument("--td-password", default=os.environ.get("TDENGINE_PASSWORD", "taosdata"))
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    args = parser.parse_args()
    rows = query_rows(
        trade_date=args.trade_date,
        symbol=args.symbol,
        host=args.td_host,
        port=args.td_port,
        user=args.td_user,
        password=args.td_password,
        database=args.td_database,
    )
    result = run_engine_shadow(rows=rows, trade_date=args.trade_date, symbol=args.symbol)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(canonical_json(result))
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
