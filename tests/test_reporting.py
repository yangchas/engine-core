from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from engine_core import (
    AuctionFactReportArtifact,
    EngineSnapshot,
    build_auction_fact_report,
    build_segment_frame,
    semantic_hash,
)
from engine_core.auction_shadow import build_auction_fact_shadow
from engine_core.facts import FactStatus
from engine_core.market_summary import normalize_auction_market_summary


FIXTURE = Path(__file__).parent / "fixtures/facts/auction_600519_20260903.json"


def _snapshot(fixture: dict, name: str) -> EngineSnapshot:
    item = fixture["snapshots"][name]
    state = item["state"]
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
        content_hash=semantic_hash({"snapshot": item["snapshot_id"], "state": state}),
        evidence_refs=tuple(fixture["evidence_refs"]),
    )


def _fact():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    middle = _snapshot(fixture, "auction_0920")
    end = _snapshot(fixture, "auction_0924")
    first = build_segment_frame(
        "auction_trial_600519", start, middle, scope_type="SYMBOL",
        scope_id=fixture["symbol"], amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN", coverage_status="PARTIAL",
        observed_start_time_ms=1788398108000, observed_end_time_ms=1788398403000,
    )
    second = build_segment_frame(
        "auction_reprice_600519", middle, end, scope_type="SYMBOL",
        scope_id=fixture["symbol"], amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN", coverage_status="READY",
        observed_start_time_ms=1788398403000, observed_end_time_ms=1788398650000,
    )
    return build_auction_fact_shadow(first, second)


def _market_summary():
    raw = json.loads(
        (Path(__file__).parent / "fixtures/facts/auction_market_summary_20260914.json")
        .read_text(encoding="utf-8")
    )
    return normalize_auction_market_summary(
        raw,
        trade_date="2026-09-03",
        source_id="fixture://auction-summary",
        observation_time_ms=1789348803146,
        evidence_refs=("fixture://auction-summary",),
    )


def test_build_only_fact_report_is_deterministic_and_fact_only():
    fact = _fact()
    left = build_auction_fact_report(
        fact, trade_date="2026-09-03", event_id="AUCTION_0925",
        data_origin="production_capture", source_time_min_ms=1788398108000,
        source_time_max_ms=1788398650000,
    )
    right = build_auction_fact_report(
        fact, trade_date="2026-09-03", event_id="AUCTION_0925",
        data_origin="production_capture", source_time_min_ms=1788398108000,
        source_time_max_ms=1788398650000,
    )
    assert isinstance(left, AuctionFactReportArtifact)
    assert left.status == "PARTIAL"
    assert left.semantic_hash == right.semantic_hash
    assert left.evidence_hash == right.evidence_hash
    assert left.text_body == right.text_body
    assert "BUY" not in left.text_body
    assert "买入" not in left.text_body
    assert left.provenance["fact_content_hash"] == fact.content_hash


def test_report_can_include_explicit_a2_summary_without_strategy_interpretation():
    report = build_auction_fact_report(
        _fact(), trade_date="2026-09-03", event_id="AUCTION_0925",
        data_origin="production_capture", market_summary=_market_summary(),
    )
    assert report.market_summary is not None
    assert "stock_count: 4914" in report.text_body
    assert "auction_amount_yuan: 2528071637" in report.text_body
    assert "BUY" not in report.text_body
    assert report.as_mapping()["market_summary"]["status"] is FactStatus.READY


def test_report_semantics_exclude_source_observation_range_but_evidence_keeps_it():
    fact = _fact()
    left = build_auction_fact_report(
        fact, trade_date="2026-09-03", event_id="AUCTION_0925",
        data_origin="production_capture", source_time_min_ms=1,
        source_time_max_ms=2,
    )
    right = build_auction_fact_report(
        fact, trade_date="2026-09-03", event_id="AUCTION_0925",
        data_origin="production_capture", source_time_min_ms=3,
        source_time_max_ms=4,
    )
    assert left.semantic_hash == right.semantic_hash
    assert left.evidence_hash != right.evidence_hash


def test_report_rejects_unsupported_origin_and_bad_date():
    fact = _fact()
    with pytest.raises(ValueError, match="data_origin"):
        build_auction_fact_report(
            fact, trade_date="2026-09-03", event_id="AUCTION_0925",
            data_origin="network_now",
        )
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        build_auction_fact_report(
            fact, trade_date="20260903", event_id="AUCTION_0925",
            data_origin="production_capture",
        )


def test_report_artifact_containers_are_frozen():
    report = build_auction_fact_report(
        _fact(), trade_date="2026-09-03", event_id="AUCTION_0925",
        data_origin="replay_fixture_only",
    )
    with pytest.raises(TypeError):
        report.metrics["price_delta_milli"] = 1
    with pytest.raises(TypeError):
        report.provenance["data_origin"] = "current_cache_only"


def test_unavailable_fact_is_not_promoted_to_partial_or_complete():
    fact = replace(_fact(), status=FactStatus.UNAVAILABLE)
    report = build_auction_fact_report(
        fact, trade_date="2026-09-03", event_id="AUCTION_0925",
        data_origin="current_cache_only",
    )
    assert report.status == "DATA_UNAVAILABLE"
