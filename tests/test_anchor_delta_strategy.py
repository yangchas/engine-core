from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from engine_core import (
    AnchorDeltaShadowStrategy,
    DEFAULT_ANCHOR_PAIRS,
    DeterministicEngine,
    EngineSignal,
    FrozenDataBundle,
    MarketStateReducer,
    SignalKind,
    WindowManager,
    WindowSpec,
    build_anchor_delta_evidence,
    deep_freeze,
    semantic_hash,
)
from engine_core.contracts import EngineSnapshot


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


def test_anchor_delta_strategy_delegates_to_verified_wheel():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    previous = _snapshot(fixture, "auction_0920")
    current = _snapshot(fixture, "auction_0924")
    strategy = AnchorDeltaShadowStrategy(
        scope_id=fixture["symbol"],
        anchor_pairs=(("AUCTION_0920", "AUCTION_0924"),),
    )
    pending = strategy.evaluate(
        previous,
        FrozenDataBundle.empty("eval-0920", previous.logical_time_ms),
    )
    observed = strategy.evaluate(
        current,
        FrozenDataBundle.empty("eval-0924", current.logical_time_ms),
    )

    assert pending.trace["fact_status"] == "PENDING"
    assert observed.trace["fact_status"] == "OBSERVED"
    assert observed.trace["decision_status"] == "FACT_ONLY"
    fact = observed.trace["anchor_deltas"][0]["fact"]
    previous_state = previous.symbol_states[fixture["symbol"]]
    current_state = current.symbol_states[fixture["symbol"]]
    expected = build_anchor_delta_evidence(
        {
            "symbol": fixture["symbol"],
            "tag": "0920",
            "price_milli": previous_state["price_milli"],
            "auction_amount_yuan": previous_state["auction_amount_yuan"],
            "bid_amount_yuan": previous_state["auction_bid_amount_yuan"],
            "ask_amount_yuan": previous_state["auction_ask_amount_yuan"],
        },
        {
            "symbol": fixture["symbol"],
            "tag": "0924",
            "price_milli": current_state["price_milli"],
            "auction_amount_yuan": current_state["auction_amount_yuan"],
            "bid_amount_yuan": current_state["auction_bid_amount_yuan"],
            "ask_amount_yuan": current_state["auction_ask_amount_yuan"],
        },
        symbol=fixture["symbol"],
        from_tag="0920",
        to_tag="0924",
    )
    assert fact == deep_freeze(expected)
    assert "BUY" not in str(observed.trace)


def test_anchor_delta_strategy_allows_adjacent_pairs_to_share_endpoint():
    strategy = AnchorDeltaShadowStrategy(scope_id="600519", anchor_pairs=DEFAULT_ANCHOR_PAIRS)
    assert strategy.anchor_pairs == DEFAULT_ANCHOR_PAIRS


def test_anchor_delta_strategy_hash_is_semantic_and_evidence_separated():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    previous = _snapshot(fixture, "auction_0920")
    current = _snapshot(fixture, "auction_0924")

    def run(left: EngineSnapshot, right: EngineSnapshot):
        strategy = AnchorDeltaShadowStrategy(
            scope_id=fixture["symbol"],
            anchor_pairs=(("AUCTION_0920", "AUCTION_0924"),),
        )
        strategy.evaluate(left, FrozenDataBundle.empty("a", left.logical_time_ms))
        return strategy.evaluate(right, FrozenDataBundle.empty("b", right.logical_time_ms))

    left = run(previous, current)
    right = run(
        replace(previous, evidence_refs=("fixture://other/previous",)),
        replace(current, evidence_refs=("fixture://other/current",)),
    )
    assert left.content_hash == right.content_hash
    assert left.trace["evidence_hash"] != right.trace["evidence_hash"]


def test_anchor_delta_strategy_content_hash_excludes_submission_context():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    previous = _snapshot(fixture, "auction_0920")
    current = _snapshot(fixture, "auction_0924")

    def run(last_evaluation_id: str):
        strategy = AnchorDeltaShadowStrategy(
            scope_id=fixture["symbol"],
            anchor_pairs=(("AUCTION_0920", "AUCTION_0924"),),
        )
        strategy.evaluate(previous, FrozenDataBundle.empty("same-a", previous.logical_time_ms))
        return strategy.evaluate(
            current,
            FrozenDataBundle.empty(last_evaluation_id, current.logical_time_ms),
        )

    left = run("same-b")
    right = run("different-b")
    assert left.trace["submission_hash"] != right.trace["submission_hash"]
    assert left.content_hash == right.content_hash


def test_anchor_delta_strategy_runs_through_engine_data_ready_boundary():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshots = tuple(
        _snapshot(fixture, name) for name in ("auction_0920", "auction_0924")
    )
    strategy = AnchorDeltaShadowStrategy(
        scope_id=fixture["symbol"],
        anchor_pairs=(("AUCTION_0920", "AUCTION_0924"),),
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        strategy,
        session_id=fixture["trade_date"],
        phase="AUCTION",
    )
    for index, snapshot in enumerate(snapshots):
        evaluation_id = f"anchor-eval-{index}"
        engine._register_evaluation(evaluation_id, snapshot, ())
        engine.submit(
            EngineSignal(
                f"anchor-data-{index}",
                snapshot.logical_time_ms,
                index,
                SignalKind.DATA_READY,
                {
                    "evaluation_id": evaluation_id,
                    "bundle": FrozenDataBundle.empty(evaluation_id, snapshot.logical_time_ms),
                },
            )
        )
    result = engine.run_until_empty()
    assert len(result.strategy_results) == 2
    final = result.strategy_results[-1]
    assert final.trace["decision_status"] == "FACT_ONLY"
    assert final.trace["anchor_deltas"][0]["fact"]["amount_delta_yuan"] == 4407516.0
