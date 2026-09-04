import json
from pathlib import Path

from engine_core import (
    EngineSnapshot,
    build_segment_frame,
    compare_adjacent_segments,
    semantic_hash,
)


FIXTURE = Path(__file__).parent / "fixtures/facts/auction_600519_20260903.json"


def _snapshot(fixture, name):
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


def test_real_auction_pair_builds_repeatable_engine_independent_facts():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    base = _snapshot(fixture, "pre_auction_0915")
    at_0920 = _snapshot(fixture, "auction_0920")
    at_0924 = _snapshot(fixture, "auction_0924")
    a_spec = fixture["business_intervals"]["segment_a"]
    b_spec = fixture["business_intervals"]["segment_b"]

    segment_a = build_segment_frame(
        "auction_trial_600519",
        base,
        at_0920,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN",
        coverage_status=a_spec["coverage_status"],
        observed_start_time_ms=a_spec["observed_start_ms"],
        observed_end_time_ms=a_spec["observed_end_ms"],
    )
    segment_b = build_segment_frame(
        "auction_reprice_600519",
        at_0920,
        at_0924,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN",
        coverage_status=b_spec["coverage_status"],
        observed_start_time_ms=b_spec["observed_start_ms"],
        observed_end_time_ms=b_spec["observed_end_ms"],
    )
    comparison = compare_adjacent_segments(segment_a, segment_b)

    assert segment_a.start_time_ms == a_spec["start_ms"]
    assert segment_a.end_time_ms == a_spec["end_exclusive_ms"]
    assert segment_a.coverage_status == "PARTIAL"
    assert segment_a.observed_start_time_ms == 1788398108000
    assert segment_a.price.return_bp == 15
    assert segment_a.volume.amount_delta_yuan == 2209938
    assert segment_a.order_book.directional_pressure_yuan == -129960
    assert segment_a.volume.volume_delta_lots is None
    assert segment_a.price.field_lineage["end_price_milli"] == (at_0920.snapshot_id,)
    assert at_0920.evidence_refs == tuple(fixture["evidence_refs"])
    assert segment_b.price.return_bp == -15
    assert segment_b.volume.amount_delta_yuan == 4407516
    assert segment_b.order_book.directional_pressure_yuan == 648770
    assert comparison.price_change == "PRICE_WEAKER"
    assert comparison.volume_change == "VOLUME_EXPANDING"
    assert comparison.order_book_change == "PRESSURE_IMPROVING"

    repeat = build_segment_frame(
        "auction_trial_600519",
        base,
        at_0920,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN",
        coverage_status=a_spec["coverage_status"],
        observed_start_time_ms=a_spec["observed_start_ms"],
        observed_end_time_ms=a_spec["observed_end_ms"],
    )
    assert segment_a.content_hash == repeat.content_hash
