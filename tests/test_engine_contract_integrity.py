from datetime import datetime, timezone

import pytest

from engine_core import (
    DataResult,
    DataStatus,
    DeterministicEngine,
    EngineSignal,
    FrozenDataBundle,
    MarketStateReducer,
    SignalKind,
    WindowManager,
    WindowSpec,
    semantic_hash,
)
from engine_core.contracts import StrategyResult


class RecordingStrategy:
    strategy_id = "recording"

    def __init__(self):
        self.triggers = []
        self.engine = None

    def evaluate(self, snapshot, bundle):
        self.triggers.append(snapshot.trigger_id)
        if snapshot.trigger_id == "PARENT":
            self.engine.submit(
                EngineSignal(
                    "child",
                    snapshot.logical_time_ms,
                    0,
                    SignalKind.TIMER,
                    {"trigger_id": "CHILD"},
                )
            )
        trace = {"trigger_id": snapshot.trigger_id}
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=trace,
            evidence_refs=(),
            content_hash=semantic_hash(trace),
        )


def _engine(strategy=None, **kwargs):
    return DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        strategy or RecordingStrategy(),
        session_id="2026-09-04",
        phase="AUCTION",
        **kwargs,
    )


def _unavailable_result(function_id="previous_day_stats"):
    return DataResult(
        request_id="r",
        function_id=function_id,
        status=DataStatus.UNAVAILABLE,
        data=None,
        actual_source=None,
        requested_trade_date="2026-09-04",
        actual_trade_date=None,
        effective_at_ms=None,
        available_at_ms=None,
        observed_at_ms=1,
        schema_version=1,
        completeness=0.0,
    )


def test_data_ready_must_reference_engine_owned_evaluation():
    engine = _engine()
    bundle = FrozenDataBundle.empty("unknown", 1)
    engine.submit(
        EngineSignal(
            "unknown-ready",
            1,
            1,
            SignalKind.DATA_READY,
            {"evaluation_id": "unknown", "bundle": bundle},
        )
    )
    with pytest.raises(ValueError, match="unknown evaluation_id"):
        engine.run_until_empty()


def test_evaluation_registration_is_once_and_terminal_tombstone_classifies_retries():
    engine = _engine(terminal_evaluation_limit=1)
    snapshot = engine._reducer.build_snapshot("TEST", logical_time_ms=1)
    engine._register_evaluation("eval-1", snapshot, ())
    bundle = FrozenDataBundle.empty("eval-1", 1)
    engine._complete_evaluation("eval-1", bundle, 1)
    with pytest.raises(ValueError, match="duplicate"):
        engine._complete_evaluation("eval-1", bundle, 1)
    with pytest.raises(ValueError, match="already registered"):
        engine._register_evaluation("eval-1", snapshot, ())

    engine._register_evaluation("eval-2", snapshot, ())
    engine._complete_evaluation("eval-2", FrozenDataBundle.empty("eval-2", 1), 1)
    with pytest.raises(ValueError, match="unknown terminal"):
        engine._complete_evaluation("eval-1", bundle, 1)


def test_evaluation_registration_ledger_allows_long_session_without_artificial_cap():
    engine = _engine(terminal_evaluation_limit=1)
    snapshot = engine._reducer.build_snapshot("LONG", logical_time_ms=1)
    for index in range(4097):
        evaluation_id = "long-eval-%04d" % index
        engine._register_evaluation(evaluation_id, snapshot, ())
        engine._complete_evaluation(
            evaluation_id,
            FrozenDataBundle.empty(evaluation_id, 1),
            1,
        )
    assert len(engine._registered_evaluation_ids) == 4097
    assert len(engine._terminal_evaluations) == 1
    assert not engine._pending_evaluations
    with pytest.raises(ValueError, match="already registered"):
        engine._register_evaluation("long-eval-0000", snapshot, ())


def test_submission_hash_contains_observation_context_but_content_hash_does_not():
    first = DataResult(
        request_id="r",
        function_id="f",
        status=DataStatus.READY,
        data={"value": 1},
        actual_source="a",
        requested_trade_date="2026-09-04",
        actual_trade_date="2026-09-03",
        effective_at_ms=1,
        available_at_ms=1,
        observed_at_ms=2,
        schema_version=1,
        completeness=1.0,
    )
    second = DataResult(
        request_id="r",
        function_id="f",
        status=DataStatus.READY,
        data={"value": 1},
        actual_source="b",
        requested_trade_date="2026-09-04",
        actual_trade_date="2026-09-03",
        effective_at_ms=1,
        available_at_ms=1,
        observed_at_ms=3,
        schema_version=1,
        completeness=1.0,
    )
    left = FrozenDataBundle.from_results("eval", 10, ("f",), {"f": first})
    right = FrozenDataBundle.from_results("eval", 10, ("f",), {"f": second})
    assert left.content_hash == right.content_hash
    assert left.submission_hash != right.submission_hash


def test_same_time_child_signal_cannot_overtake_current_generation():
    strategy = RecordingStrategy()
    engine = _engine(strategy)
    strategy.engine = engine
    logical = 100
    engine.submit(EngineSignal("parent", logical, 1, SignalKind.TIMER, {"trigger_id": "PARENT"}))
    engine.submit(EngineSignal("sibling", logical, 2, SignalKind.TIMER, {"trigger_id": "SIBLING"}))
    engine.run_until_empty()
    assert strategy.triggers == ["PARENT", "SIBLING", "CHILD"]


def test_ready_data_uses_available_at_not_observed_at_for_cutoff():
    engine = _engine()
    snapshot = engine._reducer.build_snapshot("ASYNC", logical_time_ms=100)
    engine._register_evaluation("eval-ready", snapshot, ("f",))
    result = DataResult(
        request_id="r",
        function_id="f",
        status=DataStatus.READY,
        data={"value": 1},
        actual_source="fixture",
        requested_trade_date="2026-09-04",
        actual_trade_date="2026-09-03",
        effective_at_ms=1,
        available_at_ms=100,
        observed_at_ms=101,
        schema_version=1,
        completeness=1.0,
    )
    bundle = FrozenDataBundle.from_results("eval-ready", 100, ("f",), {"f": result})
    engine.submit(
        EngineSignal(
            "ready",
            101,
            1,
            SignalKind.DATA_READY,
            {"evaluation_id": "eval-ready", "bundle": bundle},
        )
    )
    outcome = engine.run_until_empty()
    assert outcome.strategy_results[-1].evaluation_id == "eval-ready"
