import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from engine_core import (
    DeterministicEngine,
    EngineSignal,
    FrozenDataBundle,
    FreshnessPolicy,
    MarketStateReducer,
    OpeningShadowStrategy,
    ProbeStrategy,
    SignalKind,
    WindowManager,
    WindowSpec,
    build_q2_projection,
    normalize_q2,
)
from engine_core.contracts import EngineSnapshot, semantic_hash


def _snapshot(
    values,
    *,
    symbol="600519",
    logical_time_ms=2000,
    session_id="2026-09-16",
    source_time_range=None,
):
    metadata = source_time_range or {
        "oldest_source_time_ms": 1000,
        "newest_source_time_ms": 1100,
    }
    content = {
        "trigger_id": "OPENING_0932",
        "logical_time_ms": logical_time_ms,
        "symbol": symbol,
        "state": values,
    }
    return EngineSnapshot(
        snapshot_id="snapshot-opening",
        trigger_id="OPENING_0932",
        logical_time_ms=logical_time_ms,
        session_id=session_id,
        phase="OPENING",
        market_state_revision=1,
        source_observation_metadata=metadata,
        symbol_states={symbol: values},
        raw_market_cross_section={},
        raw_theme_cross_section={},
        windows={},
        coverage=1.0,
        completeness="READY",
        content_hash=semantic_hash(content),
        evidence_refs=(f"fixture://opening/{symbol}",),
    )


def test_real_q2frame_speed_basis_points_map_to_opening_ratio():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/q2frame_opening_consumer_real_20260929.json"
        ).read_text(encoding="utf-8")
    )
    raw = fixture["q2_update"]
    source = fixture["source"]
    quote = normalize_q2(
        raw["symbol"],
        {key: value for key, value in raw.items() if key != "symbol"},
    )
    snapshot = _snapshot(
        quote.to_mapping(),
        symbol=raw["symbol"],
        logical_time_ms=source["logical_ts_ms"],
        session_id=fixture["trade_date"],
        source_time_range={
            "oldest_source_time_ms": quote.source_record_time_ms,
            "newest_source_time_ms": quote.source_record_time_ms,
        },
    )

    result = OpeningShadowStrategy(scope_id=raw["symbol"]).evaluate(
        snapshot,
        FrozenDataBundle.empty("real-q2-speed-opening", source["logical_ts_ms"]),
    )

    assert raw["spd1m"] == 26  # source Q2 unit: basis points
    assert quote.speed_1m_bp == 26
    assert result.trace["opening_fact"]["speed_1m"] == 0.0026
    assert result.trace["opening_fact"]["speed_1m"] == fixture[
        "engine_next_q2_view"
    ]["speed_1m"]
    assert result.trace["opening_fact"]["speed_1m"] != raw["spd1m"]
    assert result.trace["opening_fact_field_status"]["speed_1m"] == "AVAILABLE"


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
    assert result.trace["symbol_source_quality"]["freshness_assessment"] == "STALE"


def test_opening_shadow_keeps_sampled_real_stale_q2_facts_partial():
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/q2/real_stale_cohort_20261009.json"
        ).read_text(encoding="utf-8")
    )
    provenance = fixture["provenance"]
    rows = fixture["rows"]
    symbols = tuple(row["symbol"] for row in rows)
    raw_hashes = {row["symbol"]: row["raw_hash"] for row in rows}

    assert fixture["fixture_contract"] == "RealQ2StaleCohortSampleV1"
    assert provenance["source_capture_file_sha256"] == (
        "7806d6d1dfd26daa4807ae0512708b21b37bf6c6d351104be364a70fd8d02003"
    )
    assert provenance["source_input_canonical_sha256"] == (
        "672304b645e8cbf5902ff9b7e301e3191e45086e57580da67db8bb3db7845abd"
    )
    canonical_rows = json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert hashlib.sha256(canonical_rows).hexdigest() == provenance[
        "rows_canonical_sha256"
    ]
    limitations = provenance["limitations"]
    assert any("first 50 lexically sorted" in item for item in limitations)
    assert any(
        "newest source-record age was 183.128 seconds" in item
        for item in limitations
    )
    assert any("not NORMAL opening acceptance" in item for item in limitations)
    assert any("does not recompute" in item for item in limitations)
    assert any("not representative of Shanghai or ChiNext" in item for item in limitations)
    assert any("does not cover missing core numeric fields" in item for item in limitations)
    assert provenance["full_observed_cohort"] == {
        "expected_count": 5227,
        "quote_count": 5227,
        "missing_count": 0,
        "stale_count": 5227,
        "coverage": 1.0,
        "status": "STALE",
        "consistency": "BEST_EFFORT_STALE",
    }
    assert len(rows) == 50
    assert symbols == tuple(sorted(set(symbols)))

    observed_at = datetime.fromisoformat(provenance["read_completed_at"])
    projection = build_q2_projection(
        provenance["trade_date"],
        observed_at,
        symbols,
        raw_hashes,
        freshness_policy=FreshnessPolicy(
            stale_after_ms=provenance["freshness_policy_stale_after_ms"]
        ),
    )
    assert projection.status.value == "STALE"
    assert projection.consistency_status == "BEST_EFFORT_STALE"
    # Coverage here is relative only to the sampled 50-symbol input universe.
    assert projection.coverage == 1.0
    assert len(projection.quotes) == len(symbols) == 50
    assert len(projection.missing_symbols) == 0
    assert len(projection.stale_symbols) == 50

    now = projection.envelope.observed_time_ms
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("opening-real-stale", now, now + 1),)),
        ProbeStrategy(),
        session_id=projection.trade_date,
        phase="READ_ONLY_PROBE",
    )
    engine.submit(
        EngineSignal(
            "opening-real-stale-frame",
            now,
            1,
            SignalKind.MARKET_UPDATE,
            projection,
        )
    )
    # Reuse the existing window trigger contract; this fixture is a 09:42 read,
    # not an OPENING_0932 clock-time evaluation.
    engine.submit(
        EngineSignal(
            "opening-real-stale-close",
            now + 1,
            2,
            SignalKind.TIMER,
            {
                "trigger_id": "OPENING_0932",
                "close_windows": ("opening-real-stale",),
            },
        )
    )
    engine_run = engine.run_until_empty()
    assert len(engine_run.snapshots) == 1
    snapshot = engine_run.snapshots[0]
    assert snapshot.completeness == "STALE"
    # The engine snapshot has the same sample-local coverage scope.
    assert snapshot.coverage == 1.0
    assert len(snapshot.symbol_states) == 50

    bundle = FrozenDataBundle.empty("opening-real-stale-cohort", now)
    results = []
    for symbol in symbols:
        strategy = OpeningShadowStrategy(scope_id=symbol)
        result = strategy.evaluate(snapshot, bundle)
        repeated = strategy.evaluate(snapshot, bundle)
        assert result.content_hash == repeated.content_hash
        results.append(result)

    assert len(results) == 50
    assert sum(result.trace["fact_status"] == "PARTIAL" for result in results) == 50
    assert sum(result.trace["fact_status"] == "READY" for result in results) == 0
    assert sum(
        result.trace["symbol_source_quality"]["freshness_assessment"] == "STALE"
        for result in results
    ) == 50
    assert sum(result.trace["opening_fact"] is not None for result in results) == 50

    for row, result in zip(rows, results):
        quote = projection.quotes[row["symbol"]]
        fact = result.trace["opening_fact"]
        assert result.state == "OBSERVE"
        assert result.trace["decision_status"] == "FACT_ONLY"
        assert result.trace["snapshot_quality"]["stale_symbol_count"] == 50
        assert result.trace["symbol_source_quality"]["field_errors"] == ("stale",)
        assert fact["status"] == "available"
        assert fact["change_pct"] == pytest.approx(
            (quote.price_milli / quote.pre_close_milli - 1.0) * 100.0
        )
        assert fact["amount_2m_yuan"] == quote.amount_2m_yuan
        assert fact["limit_state"] == quote.limit_state


def test_ready_opening_fact_reports_age_without_claiming_freshness():
    snapshot = _snapshot(
        {
            "price_milli": 10500,
            "pre_close_milli": 10000,
            "amount_2m_yuan": 1200000,
            "limit_state": 0,
            "source_record_time_ms": 9000,
            "field_errors": (),
        },
        source_time_range={
            "observation_time_ms": 100000,
            "oldest_source_time_ms": 9000,
            "newest_source_time_ms": 9000,
        },
    )
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-age-unassessed", 100000)
    )

    # READY is scoped to fact/source-time availability. Without an explicit
    # freshness policy, the value remains available but age is diagnostic.
    assert result.trace["fact_status"] == "READY"
    assert result.trace["opening_fact"]["status"] == "available"
    assert result.trace["symbol_source_quality"] == {
        "source_record_time_ms": 9000,
        "source_time_age_ms_at_observation": 91000,
        "freshness_assessment": "UNASSESSED",
        "field_errors": (),
        "time_quality_errors": (),
    }


def test_future_source_time_is_invalid_even_without_precomputed_field_error():
    snapshot = _snapshot(
        {
            "price_milli": 10500,
            "pre_close_milli": 10000,
            "amount_2m_yuan": 1200000,
            "limit_state": 0,
            "source_record_time_ms": 110000,
            "field_errors": (),
        },
        source_time_range={"observation_time_ms": 100000},
    )
    result = OpeningShadowStrategy(scope_id="600519").evaluate(
        snapshot, FrozenDataBundle.empty("opening-future-source-time", 100000)
    )

    assert result.trace["fact_status"] == "PARTIAL"
    assert result.trace["opening_fact"]["status"] == "available"
    assert (
        result.trace["symbol_source_quality"]["source_time_age_ms_at_observation"]
        == -10000
    )
    assert result.trace["symbol_source_quality"]["freshness_assessment"] == "INVALID"


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
