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


def test_opening_shadow_wraps_verified_fact_only_without_reinterpreting_speed_units():
    snapshot = _snapshot(
        {
            "price_milli": 10500,
            "pre_close_milli": 10000,
            "amount_2m_yuan": 1200000,
            "limit_state": 1,
            "name": "fixture",
            "speed_1m_bp": 250,
        }
    )
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-eval", 2000)
    )

    assert result.state == "OBSERVE"
    assert result.trace["decision_status"] == "FACT_ONLY"
    assert result.trace["fact_status"] == "READY"
    assert result.trace["opening_fact"]["change_pct"] == 5.000000000000004
    assert result.trace["opening_fact"]["speed_1m"] is None
    assert result.evidence_refs == ("fixture://opening/600519",)


def test_opening_shadow_preserves_missing_symbol_state():
    snapshot = replace(_snapshot({}), symbol_states={})
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-missing", 2000)
    )

    assert result.trace["fact_status"] == "MISSING"
    assert result.trace["reason_codes"] == ("SYMBOL_STATE_MISSING",)


def test_opening_shadow_does_not_promote_partial_snapshot_to_ready():
    snapshot = replace(
        _snapshot(
            {
                "price_milli": 10500,
                "pre_close_milli": 10000,
                "amount_2m_yuan": 1200000,
                "limit_state": 0,
            }
        ),
        completeness="PARTIAL",
    )
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-partial", 2000)
    )

    assert result.trace["opening_fact"]["status"] == "available"
    assert result.trace["fact_status"] == "PARTIAL"
