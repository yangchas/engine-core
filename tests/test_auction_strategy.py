import json
from pathlib import Path
from dataclasses import replace

import pytest

from engine_core import (
    DeterministicEngine,
    EngineSignal,
    FrozenDataBundle,
    MarketStateReducer,
    SignalKind,
    WindowManager,
    WindowSpec,
)
from engine_core.auction_strategy import AuctionShadowStrategy
from engine_core.contracts import EngineSnapshot, semantic_hash


FIXTURE = Path(__file__).parent / "fixtures/facts/auction_600519_20260903.json"


def _snapshot(fixture, name) -> EngineSnapshot:
    item = fixture["snapshots"][name]
    state = item["state"]
    metadata = {
        "source_table": item["source_table"],
        "business_anchor": item["trigger_id"],
        "business_anchor_time_ms": item["business_anchor_time_ms"],
        "source_record_time_ms": item["source_record_time_ms"],
    }
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
        source_observation_metadata=metadata,
        symbol_states={fixture["symbol"]: state},
        raw_market_cross_section={},
        raw_theme_cross_section={},
        windows={},
        coverage=1.0,
        completeness="READY",
        content_hash=semantic_hash(content),
        evidence_refs=tuple(fixture["evidence_refs"]),
    )


def test_strategy_is_pending_until_all_auction_anchors_exist():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        previous_coverage_status="PARTIAL",
        current_coverage_status="READY",
    )
    start = _snapshot(fixture, "pre_auction_0915")
    bundle = FrozenDataBundle.empty("eval-start", start.logical_time_ms)

    result = strategy.evaluate(start, bundle)

    assert result.state == "OBSERVE"
    assert result.trace["decision_status"] == "FACT_ONLY"
    assert result.trace["fact_status"] == "PENDING"
    assert result.trace["missing_trigger_ids"] == (
        "AUCTION_0920",
        "AUCTION_0924",
    )
    assert "BUY" not in str(result.trace)


def test_strategy_engine_boundary_composes_same_foundation_fact():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    strategy = AuctionShadowStrategy(scope_id=fixture["symbol"])
    snapshots = (
        _snapshot(fixture, "pre_auction_0915"),
        _snapshot(fixture, "auction_0920"),
        _snapshot(fixture, "auction_0924"),
    )
    result = None
    for index, snapshot in enumerate(snapshots):
        result = strategy.evaluate(
            snapshot,
            FrozenDataBundle.empty("eval-%d" % index, snapshot.logical_time_ms),
        )

    assert result is not None
    trace = result.trace
    shadow = trace["auction_fact_shadow"]
    assert trace["decision_status"] == "FACT_ONLY"
    assert trace["fact_status"] == "PARTIAL"
    assert shadow["scope_id"] == fixture["symbol"]
    assert shadow["status"] == "PARTIAL"
    assert shadow["metrics"]["price_delta_milli"] == -2060
    assert shadow["metrics"]["amount_delta_yuan"] == 4407516
    assert shadow["metrics"]["pressure_delta_yuan"] == 778730


def test_strategy_repeated_execution_is_deterministic():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def run_once():
        strategy = AuctionShadowStrategy(
            scope_id=fixture["symbol"],
            previous_coverage_status="PARTIAL",
            current_coverage_status="READY",
        )
        result = None
        for index, name in enumerate(
            ("pre_auction_0915", "auction_0920", "auction_0924")
        ):
            snapshot = _snapshot(fixture, name)
            result = strategy.evaluate(
                snapshot,
                FrozenDataBundle.empty("eval-%d" % index, snapshot.logical_time_ms),
            )
        return result

    left = run_once()
    right = run_once()
    assert left.content_hash == right.content_hash
    assert left.trace["auction_fact_shadow"]["content_hash"] == right.trace[
        "auction_fact_shadow"
    ]["content_hash"]
    assert left.trace["auction_fact_shadow"]["evidence_hash"] == right.trace[
        "auction_fact_shadow"
    ]["evidence_hash"]


def test_engine_uses_production_strategy_without_changing_wheel_result():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshots = tuple(
        _snapshot(fixture, name)
        for name in ("pre_auction_0915", "auction_0920", "auction_0924")
    )
    strategy = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        previous_coverage_status="PARTIAL",
        current_coverage_status="READY",
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        strategy,
        session_id=fixture["trade_date"],
        phase="AUCTION",
    )
    for index, snapshot in enumerate(snapshots):
        evaluation_id = "auction-eval-%d" % index
        engine._register_evaluation(evaluation_id, snapshot, ())
        engine.submit(
            EngineSignal(
                "auction-data-%d" % index,
                snapshot.logical_time_ms,
                index,
                SignalKind.DATA_READY,
                {
                    "evaluation_id": evaluation_id,
                    "bundle": FrozenDataBundle.empty(
                        evaluation_id, snapshot.logical_time_ms
                    ),
                },
            )
        )

    result = engine.run_until_empty()
    assert len(result.strategy_results) == 3
    final_trace = result.strategy_results[-1].trace
    assert final_trace["strategy_id"] == "auction-shadow-v1"
    assert final_trace["decision_status"] == "FACT_ONLY"
    assert final_trace["auction_fact_shadow"]["status"] == "PARTIAL"
    assert final_trace["auction_fact_shadow"]["metrics"]["amount_delta_yuan"] == 4407516

    direct = AuctionShadowStrategy(
        scope_id=fixture["symbol"],
        previous_coverage_status="PARTIAL",
        current_coverage_status="READY",
    )
    direct_result = None
    for index, snapshot in enumerate(snapshots):
        direct_result = direct.evaluate(
            snapshot,
            FrozenDataBundle.empty("direct-%d" % index, snapshot.logical_time_ms),
        )
    assert direct_result is not None
    assert (
        final_trace["auction_fact_shadow"]["content_hash"]
        == direct_result.trace["auction_fact_shadow"]["content_hash"]
    )
    assert (
        final_trace["auction_fact_shadow"]["evidence_hash"]
        == direct_result.trace["auction_fact_shadow"]["evidence_hash"]
    )


def test_strategy_rejects_cross_session_and_conflicting_anchor_snapshots():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    strategy = AuctionShadowStrategy(scope_id=fixture["symbol"])
    strategy.evaluate(start, FrozenDataBundle.empty("eval-1", start.logical_time_ms))

    with pytest.raises(ValueError, match="conflicting snapshot"):
        changed = replace(start, content_hash=semantic_hash({"changed": True}))
        strategy.evaluate(
            changed,
            FrozenDataBundle.empty("eval-2", changed.logical_time_ms),
        )

    with pytest.raises(ValueError, match="mix sessions"):
        other = replace(start, session_id="2026-09-04")
        strategy.evaluate(
            other,
            FrozenDataBundle.empty("eval-3", other.logical_time_ms),
        )


def test_strategy_retains_only_declared_anchor_snapshots():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    strategy = AuctionShadowStrategy(scope_id=fixture["symbol"])
    strategy.evaluate(start, FrozenDataBundle.empty("eval-start", start.logical_time_ms))

    for index in range(100):
        unrelated = replace(
            start,
            trigger_id="UNRELATED_%03d" % index,
            snapshot_id="unrelated-%03d" % index,
        )
        strategy.evaluate(
            unrelated,
            FrozenDataBundle.empty("eval-unrelated-%03d" % index, unrelated.logical_time_ms),
        )

    assert tuple(strategy._snapshots) == ("PRE_AUCTION_0915",)
