"""Differential checks for the smallest Gate B auction fact slice.

The expected values below are an independent oracle from the captured source
rows and the verified current t1-v2 field contract.  This test deliberately
does not import the legacy project: old code is evidence, not a runtime
dependency of engine_core.
"""

from __future__ import annotations

import json
from pathlib import Path

from engine_core import (
    EngineSnapshot,
    build_segment_frame,
    compare_adjacent_segments,
    semantic_hash,
)


FIXTURE = Path(__file__).parent / "fixtures/facts/auction_600519_20260903.json"


def _snapshot(fixture: dict, name: str) -> EngineSnapshot:
    item = fixture["snapshots"][name]
    state = item["state"]
    content = {
        "snapshot_id": item["snapshot_id"],
        "trigger_id": item["trigger_id"],
        "logical_time_ms": item["business_anchor_time_ms"],
        "symbol": fixture["symbol"],
        "state": state,
    }
    return EngineSnapshot(
        snapshot_id=item["snapshot_id"],
        trigger_id=item["trigger_id"],
        logical_time_ms=item["business_anchor_time_ms"],
        session_id=fixture["trade_date"],
        phase="AUCTION",
        market_state_revision=1,
        source_observation_metadata={
            "source_table": item["source_table"],
            "business_anchor": item["trigger_id"],
            "business_anchor_time_ms": item["business_anchor_time_ms"],
            "source_record_time_ms": item["source_record_time_ms"],
        },
        symbol_states={fixture["symbol"]: state},
        raw_market_cross_section={},
        raw_theme_cross_section={},
        windows={},
        coverage=1.0,
        completeness="READY",
        content_hash=semantic_hash(content),
        evidence_refs=tuple(fixture["evidence_refs"]),
    )


def _legacy_oracle(fixture: dict) -> dict[str, int]:
    """Compute expected anchor differences without using engine_core facts."""

    previous = fixture["snapshots"]["auction_0920"]["state"]
    current = fixture["snapshots"]["auction_0924"]["state"]
    pressure_previous = (
        previous["auction_bid_amount_yuan"]
        - previous["auction_ask_amount_yuan"]
    )
    pressure_current = (
        current["auction_bid_amount_yuan"]
        - current["auction_ask_amount_yuan"]
    )
    return {
        "price_delta_milli": current["price_milli"] - previous["price_milli"],
        "amount_delta_yuan": (
            current["auction_amount_yuan"] - previous["auction_amount_yuan"]
        ),
        "rest_bid_delta_yuan": (
            current["auction_bid_amount_yuan"]
            - previous["auction_bid_amount_yuan"]
        ),
        "rest_ask_delta_yuan": (
            current["auction_ask_amount_yuan"]
            - previous["auction_ask_amount_yuan"]
        ),
        "pressure_previous_yuan": pressure_previous,
        "pressure_current_yuan": pressure_current,
        "pressure_delta_yuan": pressure_current - pressure_previous,
    }


def test_600519_adjacent_facts_match_independent_legacy_oracle():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    base = _snapshot(fixture, "pre_auction_0915")
    at_0920 = _snapshot(fixture, "auction_0920")
    at_0924 = _snapshot(fixture, "auction_0924")
    oracle = _legacy_oracle(fixture)

    segment_a = build_segment_frame(
        "auction_trial_600519",
        base,
        at_0920,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN",
        coverage_status="PARTIAL",
        observed_start_time_ms=1788398108000,
        observed_end_time_ms=1788398403000,
    )
    segment_b = build_segment_frame(
        "auction_reprice_600519",
        at_0920,
        at_0924,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN",
        coverage_status="READY",
        observed_start_time_ms=1788398403000,
        observed_end_time_ms=1788398650000,
    )
    comparison = compare_adjacent_segments(segment_a, segment_b)

    assert segment_b.price.start_price_milli == fixture["snapshots"]["auction_0920"]["state"]["price_milli"]
    assert segment_b.price.end_price_milli == fixture["snapshots"]["auction_0924"]["state"]["price_milli"]
    assert segment_b.price.end_price_milli - segment_b.price.start_price_milli == oracle["price_delta_milli"]
    assert segment_b.volume.amount_delta_yuan == oracle["amount_delta_yuan"]
    assert segment_b.order_book.directional_pressure_yuan == oracle["pressure_current_yuan"]
    assert segment_a.order_book.directional_pressure_yuan == oracle["pressure_previous_yuan"]
    assert oracle["pressure_delta_yuan"] == 778730
    assert comparison.price_change == "PRICE_WEAKER"
    assert comparison.volume_change == "VOLUME_EXPANDING"
    assert comparison.order_book_change == "PRESSURE_IMPROVING"
