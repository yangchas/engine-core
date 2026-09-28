from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine_core import (
    AuctionFactShadow,
    EngineSnapshot,
    build_auction_fact_shadow,
    build_segment_frame,
    semantic_hash,
)
from engine_core.auction_shadow import build_auction_fact_shadow_from_snapshots
from engine_core.facts import FactStatus


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


def _segments() -> tuple:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    at_0920 = _snapshot(fixture, "auction_0920")
    at_0924 = _snapshot(fixture, "auction_0924")
    a = fixture["business_intervals"]["segment_a"]
    b = fixture["business_intervals"]["segment_b"]
    first = build_segment_frame(
        "auction_trial_600519",
        start,
        at_0920,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN",
        coverage_status=a["coverage_status"],
        observed_start_time_ms=a["observed_start_ms"],
        observed_end_time_ms=a["observed_end_ms"],
    )
    second = build_segment_frame(
        "auction_reprice_600519",
        at_0920,
        at_0924,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN",
        coverage_status=b["coverage_status"],
        observed_start_time_ms=b["observed_start_ms"],
        observed_end_time_ms=b["observed_end_ms"],
    )
    return first, second


def test_real_600519_fact_shadow_is_fact_only_and_traceable():
    first, second = _segments()
    result = build_auction_fact_shadow(first, second)

    assert isinstance(result, AuctionFactShadow)
    assert result.status.value == "PARTIAL"
    assert result.coverage_status == "PARTIAL"
    assert result.metrics == {
        "price_delta_milli": -2060,
        "amount_delta_yuan": 4407516,
        "rest_bid_delta_yuan": 648770,
        "rest_ask_delta_yuan": -129960,
        "pressure_delta_yuan": 778730,
    }
    assert result.changes == {
        "price": "PRICE_WEAKER",
        "amount": "VOLUME_EXPANDING",
        "order_book": "PRESSURE_IMPROVING",
        "breadth": "BREADTH_UNAVAILABLE",
        "theme": "THEME_UNAVAILABLE",
    }
    trace = result.as_trace()
    assert trace["state"] == "OBSERVE"
    assert trace["decision_status"] == "FACT_ONLY"
    assert trace["hash_contract_versions"] == {
        "semantic": "SemanticHashV1",
        "evidence": "EvidenceHashV1",
    }
    assert "BUY" not in trace
    assert result.evidence_refs
    assert result.evidence_hash


def test_fact_shadow_hashes_are_repeatable_and_exclude_evidence_refs():
    first, second = _segments()
    left = build_auction_fact_shadow(first, second)
    right = build_auction_fact_shadow(first, second)
    assert left.content_hash == right.content_hash
    assert left.evidence_hash == right.evidence_hash
    assert left.content_hash == semantic_hash(
        {
            "shadow_kind": "AUCTION_ADJACENT_FACT_V1",
            "scope_type": left.scope_type,
            "scope_id": left.scope_id,
            "previous_segment_id": left.previous_segment_id,
            "current_segment_id": left.current_segment_id,
            "status": left.status,
            "coverage_status": left.coverage_status,
            "quality_status": left.quality_status,
            "metrics": left.metrics,
            "changes": left.changes,
            "reason_codes": left.reason_codes,
            "comparison_hash": left.comparison_hash,
        }
    )


def test_fact_shadow_separates_semantics_from_evidence_refs():
    first, second = _segments()
    from dataclasses import replace

    changed_evidence = replace(second, evidence_refs=("fixture://different",))
    left = build_auction_fact_shadow(first, second)
    right = build_auction_fact_shadow(first, changed_evidence)
    assert left.content_hash == right.content_hash
    assert left.evidence_hash != right.evidence_hash


def test_fact_shadow_preserves_missing_order_book_without_conclusion():
    first, second = _segments()
    from dataclasses import replace

    second = replace(
        second,
        order_book=replace(
            second.order_book,
            directional_pressure_yuan=None,
            resting_bid_end_yuan=None,
            resting_ask_end_yuan=None,
            pressure_delta_yuan=None,
            status=FactStatus.UNAVAILABLE,
        ),
    )
    result = build_auction_fact_shadow(first, second)
    assert result.status.value == "PARTIAL"
    assert result.metrics["pressure_delta_yuan"] is None
    assert result.changes["order_book"] == "PRESSURE_UNKNOWN"
    assert result.as_trace()["decision_status"] == "FACT_ONLY"


def test_fact_shadow_preserves_missing_segment_status():
    first, second = _segments()
    from dataclasses import replace

    missing = replace(
        second,
        quality=replace(second.quality, status=FactStatus.MISSING),
    )
    result = build_auction_fact_shadow(first, missing)
    assert result.status is FactStatus.MISSING
    assert result.quality_status is FactStatus.MISSING


def test_fact_shadow_rejects_non_adjacent_segments():
    first, second = _segments()
    from dataclasses import replace

    non_adjacent = replace(second, start_time_ms=second.start_time_ms + 1)
    with pytest.raises(ValueError):
        build_auction_fact_shadow(first, non_adjacent)


def test_engine_boundary_adapter_composes_three_snapshots_without_new_formula():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    middle = _snapshot(fixture, "auction_0920")
    end = _snapshot(fixture, "auction_0924")

    direct_first, direct_second = _segments()
    direct = build_auction_fact_shadow(direct_first, direct_second)
    composed = build_auction_fact_shadow_from_snapshots(
        start,
        middle,
        end,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        previous_segment_id="auction_trial_600519",
        current_segment_id="auction_reprice_600519",
        amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN",
        previous_coverage_status="PARTIAL",
        current_coverage_status="READY",
        previous_observed_start_time_ms=1788398108000,
        previous_observed_end_time_ms=1788398403000,
        current_observed_start_time_ms=1788398403000,
        current_observed_end_time_ms=1788398650000,
    )

    assert composed.content_hash == direct.content_hash
    assert composed.evidence_hash == direct.evidence_hash
    assert composed.as_trace()["decision_status"] == "FACT_ONLY"


def test_engine_boundary_auction_price_uses_explicit_t1_anchor_fields_without_px_fallback():
    from dataclasses import replace

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    middle = _snapshot(fixture, "auction_0920")
    end = _snapshot(fixture, "auction_0924")

    def with_state(snapshot, *, latest_px, anchor_prices):
        symbol = fixture["symbol"]
        state = dict(snapshot.symbol_states[symbol])
        state["price_milli"] = latest_px
        state.update(anchor_prices)
        return replace(
            snapshot,
            symbol_states={symbol: state},
            content_hash=semantic_hash(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "trigger_id": snapshot.trigger_id,
                    "logical_time_ms": snapshot.logical_time_ms,
                    "state": state,
                }
            ),
        )

    start = with_state(
        start,
        latest_px=20_000,
        anchor_prices={
            "auction_anchor_0920_price_milli": 10_000,
            "auction_anchor_0924_price_milli": None,
            "auction_anchor_0925_price_milli": None,
        },
    )
    middle = with_state(
        middle,
        latest_px=19_000,
        anchor_prices={
            "auction_anchor_0920_price_milli": 10_000,
            "auction_anchor_0924_price_milli": 11_000,
            "auction_anchor_0925_price_milli": None,
        },
    )
    end = with_state(
        end,
        latest_px=18_000,
        anchor_prices={
            "auction_anchor_0920_price_milli": 10_000,
            "auction_anchor_0924_price_milli": 11_000,
            "auction_anchor_0925_price_milli": 12_500,
        },
    )

    result = build_auction_fact_shadow_from_snapshots(
        start,
        middle,
        end,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        previous_segment_id="anchor_0920_to_0924",
        current_segment_id="anchor_0924_to_0925",
        price_fields=(
            "auction_anchor_0920_price_milli",
            "auction_anchor_0924_price_milli",
            "auction_anchor_0925_price_milli",
        ),
    )

    assert result.metrics["price_delta_milli"] == 1_500
    assert result.metrics["price_delta_milli"] != (
        end.symbol_states[fixture["symbol"]]["price_milli"]
        - middle.symbol_states[fixture["symbol"]]["price_milli"]
    )

    missing_anchor_end = with_state(
        end,
        latest_px=18_000,
        anchor_prices={
            "auction_anchor_0920_price_milli": 10_000,
            "auction_anchor_0924_price_milli": 11_000,
            "auction_anchor_0925_price_milli": None,
        },
    )
    missing = build_auction_fact_shadow_from_snapshots(
        start,
        middle,
        missing_anchor_end,
        scope_type="SYMBOL",
        scope_id=fixture["symbol"],
        previous_segment_id="anchor_0920_to_0924",
        current_segment_id="anchor_0924_to_0925",
        price_fields=(
            "auction_anchor_0920_price_milli",
            "auction_anchor_0924_price_milli",
            "auction_anchor_0925_price_milli",
        ),
    )
    assert missing.metrics["price_delta_milli"] is None


def test_engine_boundary_adapter_rejects_cross_session_or_non_monotonic_snapshots():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    middle = _snapshot(fixture, "auction_0920")
    end = _snapshot(fixture, "auction_0924")

    with pytest.raises(ValueError, match="strictly increasing"):
        build_auction_fact_shadow_from_snapshots(
            middle,
            start,
            end,
            scope_type="SYMBOL",
            scope_id=fixture["symbol"],
            previous_segment_id="a",
            current_segment_id="b",
        )

    other_session = EngineSnapshot(
        snapshot_id=end.snapshot_id,
        trigger_id=end.trigger_id,
        logical_time_ms=end.logical_time_ms,
        session_id="2026-09-04",
        phase=end.phase,
        market_state_revision=end.market_state_revision,
        source_observation_metadata=end.source_observation_metadata,
        symbol_states=end.symbol_states,
        raw_market_cross_section=end.raw_market_cross_section,
        raw_theme_cross_section=end.raw_theme_cross_section,
        windows=end.windows,
        coverage=end.coverage,
        completeness=end.completeness,
        content_hash=end.content_hash,
        evidence_refs=end.evidence_refs,
    )
    with pytest.raises(ValueError, match="share a session"):
        build_auction_fact_shadow_from_snapshots(
            start,
            middle,
            other_session,
            scope_type="SYMBOL",
            scope_id=fixture["symbol"],
            previous_segment_id="a",
            current_segment_id="b",
        )
