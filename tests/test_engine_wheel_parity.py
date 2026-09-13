import json
from pathlib import Path

from engine_core import (
    DeterministicEngine,
    EngineSignal,
    EngineSnapshot,
    FrozenDataBundle,
    MarketStateReducer,
    SignalKind,
    WindowManager,
    WindowSpec,
    build_segment_frame,
    compare_adjacent_segments,
    semantic_hash,
)
from engine_core.auction_shadow import build_auction_fact_shadow_from_snapshots
from engine_core.contracts import StrategyResult


FIXTURE = Path(__file__).parent / "fixtures/facts/auction_600519_20260903.json"


def _snapshot(fixture, name) -> EngineSnapshot:
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


class WheelParityStrategy:
    """Test-only strategy that composes the already-frozen fact wheels."""

    strategy_id = "wheel-parity"

    def __init__(self):
        self._snapshots = {}

    def evaluate(self, snapshot, bundle):
        self._snapshots[snapshot.trigger_id] = snapshot
        trace = {"trigger_id": snapshot.trigger_id}
        if {"PRE_AUCTION_0915", "AUCTION_0920", "AUCTION_0924"}.issubset(
            self._snapshots
        ):
            start = self._snapshots["PRE_AUCTION_0915"]
            at_0920 = self._snapshots["AUCTION_0920"]
            at_0924 = self._snapshots["AUCTION_0924"]
            segment_a = build_segment_frame(
                "auction_trial_600519",
                start,
                at_0920,
                scope_type="SYMBOL",
                scope_id="600519",
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
                scope_id="600519",
                amount_semantics="OBSERVED_STATE",
                volume_semantics="UNKNOWN",
                coverage_status="READY",
                observed_start_time_ms=1788398403000,
                observed_end_time_ms=1788398650000,
            )
            comparison = compare_adjacent_segments(segment_a, segment_b)
            trace.update(
                {
                    "segment_a_hash": segment_a.content_hash,
                    "segment_b_hash": segment_b.content_hash,
                    "comparison_hash": comparison.content_hash,
                }
            )
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=trace,
            evidence_refs=snapshot.evidence_refs,
            content_hash=semantic_hash(trace),
        )


class AuctionShadowObserver:
    """Test-only Engine observer; it emits facts, never a trading decision."""

    strategy_id = "auction-shadow-observer"

    def __init__(self):
        self._snapshots = {}

    def evaluate(self, snapshot, bundle):
        self._snapshots[snapshot.trigger_id] = snapshot
        trace = {"trigger_id": snapshot.trigger_id, "state": "OBSERVE"}
        required = {"PRE_AUCTION_0915", "AUCTION_0920", "AUCTION_0924"}
        if required.issubset(self._snapshots):
            shadow = build_auction_fact_shadow_from_snapshots(
                self._snapshots["PRE_AUCTION_0915"],
                self._snapshots["AUCTION_0920"],
                self._snapshots["AUCTION_0924"],
                scope_type="SYMBOL",
                scope_id="600519",
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
            trace["auction_fact_shadow"] = shadow.as_trace()
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=trace,
            evidence_refs=snapshot.evidence_refs,
            content_hash=semantic_hash(trace),
        )


def test_engine_composition_preserves_foundation_wheel_hashes():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    at_0920 = _snapshot(fixture, "auction_0920")
    at_0924 = _snapshot(fixture, "auction_0924")

    segment_a = build_segment_frame(
        "auction_trial_600519",
        start,
        at_0920,
        scope_type="SYMBOL",
        scope_id="600519",
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
        scope_id="600519",
        amount_semantics="OBSERVED_STATE",
        volume_semantics="UNKNOWN",
        coverage_status="READY",
        observed_start_time_ms=1788398403000,
        observed_end_time_ms=1788398650000,
    )
    comparison = compare_adjacent_segments(segment_a, segment_b)

    strategy = WheelParityStrategy()
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        strategy,
        session_id=fixture["trade_date"],
        phase="AUCTION",
    )
    empty_bundles = {
        "PRE_AUCTION_0915": FrozenDataBundle.empty("eval-start", start.logical_time_ms),
        "AUCTION_0920": FrozenDataBundle.empty("eval-0920", at_0920.logical_time_ms),
        "AUCTION_0924": FrozenDataBundle.empty("eval-0924", at_0924.logical_time_ms),
    }
    for evaluation_id, snapshot in (
        ("eval-start", start),
        ("eval-0920", at_0920),
        ("eval-0924", at_0924),
    ):
        engine._register_evaluation(evaluation_id, snapshot, ())
    # Deliberately submit out of order. The engine queue orders by logical time.
    for sequence, snapshot in enumerate((at_0924, start, at_0920), start=1):
        engine.submit(
            EngineSignal(
                "parity-" + snapshot.trigger_id,
                    snapshot.logical_time_ms,
                    sequence,
                    SignalKind.DATA_READY,
                    {
                        "evaluation_id": empty_bundles[snapshot.trigger_id].evaluation_id,
                        "bundle": empty_bundles[snapshot.trigger_id],
                    },
            )
        )
    result = engine.run_until_empty()
    trace = result.strategy_results[-1].trace
    assert trace["segment_a_hash"] == segment_a.content_hash
    assert trace["segment_b_hash"] == segment_b.content_hash
    assert trace["comparison_hash"] == comparison.content_hash


def test_engine_read_only_auction_shadow_composition_uses_same_wheels():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = _snapshot(fixture, "pre_auction_0915")
    at_0920 = _snapshot(fixture, "auction_0920")
    at_0924 = _snapshot(fixture, "auction_0924")
    strategy = AuctionShadowObserver()
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        strategy,
        session_id=fixture["trade_date"],
        phase="AUCTION",
    )
    evaluations = {
        "PRE_AUCTION_0915": FrozenDataBundle.empty("eval-start", start.logical_time_ms),
        "AUCTION_0920": FrozenDataBundle.empty("eval-0920", at_0920.logical_time_ms),
        "AUCTION_0924": FrozenDataBundle.empty("eval-0924", at_0924.logical_time_ms),
    }
    for snapshot in (start, at_0920, at_0924):
        engine._register_evaluation(
            evaluations[snapshot.trigger_id].evaluation_id,
            snapshot,
            (),
        )
    for sequence, snapshot in enumerate((at_0924, start, at_0920), start=1):
        engine.submit(
            EngineSignal(
                "shadow-" + snapshot.trigger_id,
                snapshot.logical_time_ms,
                sequence,
                SignalKind.DATA_READY,
                {
                    "evaluation_id": evaluations[snapshot.trigger_id].evaluation_id,
                    "bundle": evaluations[snapshot.trigger_id],
                },
            )
        )

    result = engine.run_until_empty()
    trace = result.strategy_results[-1].trace
    shadow = trace["auction_fact_shadow"]
    expected = build_auction_fact_shadow_from_snapshots(
        start,
        at_0920,
        at_0924,
        scope_type="SYMBOL",
        scope_id="600519",
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
    assert shadow["state"] == "OBSERVE"
    assert shadow["decision_status"] == "FACT_ONLY"
    assert shadow["content_hash"] == expected.content_hash
    assert shadow["evidence_hash"] == expected.evidence_hash
