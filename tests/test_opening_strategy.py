from dataclasses import replace

from engine_core import FrozenDataBundle, OpeningShadowStrategy
from engine_core.contracts import EngineSnapshot, semantic_hash


def _snapshot(values):
    metadata = {
        "oldest_source_time_ms": 1000,
        "newest_source_time_ms": 1100,
    }
    content = {
        "trigger_id": "OPENING_0932",
        "logical_time_ms": 2000,
        "symbol": "600519",
        "state": values,
    }
    return EngineSnapshot(
        snapshot_id="snapshot-opening",
        trigger_id="OPENING_0932",
        logical_time_ms=2000,
        session_id="2026-09-16",
        phase="OPENING",
        market_state_revision=1,
        source_observation_metadata=metadata,
        symbol_states={"600519": values},
        raw_market_cross_section={},
        raw_theme_cross_section={},
        windows={},
        coverage=1.0,
        completeness="READY",
        content_hash=semantic_hash(content),
        evidence_refs=("fixture://opening/600519",),
    )


def test_opening_shadow_maps_q2_basis_points_to_legacy_speed_ratio():
    snapshot = _snapshot(
        {
            "price_milli": 10500,
            "pre_close_milli": 10000,
            "amount_2m_yuan": 1200000,
            "limit_state": 1,
            "name": "fixture",
            "speed_1m_bp": 250,
            "source_record_time_ms": 1050,
        }
    )
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-eval", 2000)
    )

    assert result.state == "OBSERVE"
    assert result.trace["decision_status"] == "FACT_ONLY"
    assert result.trace["fact_status"] == "READY"
    assert result.trace["opening_fact"]["change_pct"] == 5.000000000000004
    assert result.trace["opening_fact"]["speed_1m"] == 0.025
    assert result.trace["opening_fact"]["timestamp_ms"] == 1050
    assert result.trace["fact_status_scope"] == "change_pct_and_source_time"
    assert result.trace["opening_fact_field_status"] == {
        "change_pct": "AVAILABLE",
        "amount_2m_yuan": "AVAILABLE",
        "limit_state": "AVAILABLE",
        "speed_1m": "AVAILABLE",
    }
    assert result.evidence_refs == ("fixture://opening/600519",)


def test_opening_shadow_preserves_missing_symbol_state():
    snapshot = replace(_snapshot({}), symbol_states={})
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-missing", 2000)
    )

    assert result.trace["fact_status"] == "MISSING"
    assert result.trace["reason_codes"] == ("SYMBOL_STATE_MISSING",)


def test_opening_shadow_keeps_fresh_symbol_fact_ready_when_other_symbols_are_partial():
    snapshot = replace(
        _snapshot(
            {
                "price_milli": 10500,
                "pre_close_milli": 10000,
                "amount_2m_yuan": 1200000,
                "limit_state": 0,
                "source_record_time_ms": 1050,
                "field_errors": (),
            }
        ),
        completeness="PARTIAL",
        source_observation_metadata={
            "oldest_source_time_ms": 1000,
            "newest_source_time_ms": 1100,
            "stale_symbols": ("000001",),
            "missing_symbols": (),
        },
    )
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-partial", 2000)
    )

    assert result.trace["opening_fact"]["status"] == "available"
    assert result.trace["fact_status"] == "READY"
    assert result.trace["snapshot_quality"] == {
        "completeness": "PARTIAL",
        "coverage": 1.0,
        "stale_symbol_count": 1,
        "missing_symbol_count": 0,
    }


def test_opening_shadow_marks_stale_target_symbol_partial_without_erasing_its_fact():
    snapshot = _snapshot(
        {
            "price_milli": 10500,
            "pre_close_milli": 10000,
            "amount_2m_yuan": 1200000,
            "limit_state": 0,
            "source_record_time_ms": 900,
            "field_errors": ("stale",),
        }
    )
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-stale", 2000)
    )

    assert result.trace["opening_fact"]["status"] == "available"
    assert result.trace["fact_status"] == "PARTIAL"


def test_opening_primary_ready_does_not_hide_unavailable_or_invalid_auxiliary_fields():
    snapshot = _snapshot(
        {
            "price_milli": 10500,
            "pre_close_milli": 10000,
            "amount_2m_yuan": None,
            "limit_state": 7,
            "source_record_time_ms": 1050,
            "field_errors": ("amt2m",),
        }
    )
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-aux-quality", 2000)
    )

    assert result.trace["fact_status"] == "READY"
    assert result.trace["fact_status_scope"] == "change_pct_and_source_time"
    assert result.trace["opening_fact"]["change_pct"] == 5.000000000000004
    assert result.trace["opening_fact_field_status"] == {
        "change_pct": "AVAILABLE",
        "amount_2m_yuan": "INVALID",
        "limit_state": "INVALID",
        "speed_1m": "UNAVAILABLE",
    }


def test_opening_shadow_marks_invalid_q2_speed_without_failing_primary_fact():
    snapshot = _snapshot(
        {
            "price_milli": 10500,
            "pre_close_milli": 10000,
            "amount_2m_yuan": 1200000,
            "limit_state": 0,
            "speed_1m_bp": None,
            "source_record_time_ms": 1050,
            "field_errors": ("spd1m",),
        }
    )
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-invalid-speed", 2000)
    )

    assert result.trace["fact_status"] == "READY"
    assert result.trace["opening_fact"]["speed_1m"] is None
    assert result.trace["opening_fact_field_status"]["speed_1m"] == "INVALID"
