import pytest

from engine_core import (
    DeterministicEngine,
    EngineSignal,
    EvaluationNode,
    EvaluationPlan,
    MarketStateReducer,
    ProbeStrategy,
    SignalKind,
    WindowManager,
    WindowSpec,
)


def _plan(*, version="v1", facts=(), strategies=("probe",), requirements=()):
    return EvaluationPlan(
        "auction-evaluation",
        version,
        (
            EvaluationNode(
                "auction-0920",
                "AUCTION_0920",
                data_requirements=requirements,
                fact_functions=facts,
                strategies=strategies,
            ),
        ),
    )


def _engine(plan):
    return DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction", 0, 1000),)),
        ProbeStrategy(),
        session_id="trace-1",
        phase="AUCTION",
        evaluation_plan=plan,
    )


def _timer(payload=None):
    return EngineSignal(
        "timer-0920",
        100,
        1,
        SignalKind.TIMER,
        payload or {"trigger_id": "AUCTION_0920"},
    )


def test_evaluation_plan_supplies_fixed_data_requirement_order():
    engine = _engine(_plan(requirements=("previous_day_stats", "theme_members")))
    engine.submit(_timer())
    result = engine.run_until_empty()
    assert not result.strategy_results
    pending = next(iter(engine._pending_evaluations.values()))
    assert pending.function_order == ("previous_day_stats", "theme_members")
    assert pending.snapshot.trigger_id == "AUCTION_0920"


def test_plan_and_signal_cannot_both_define_data_requirements():
    engine = _engine(_plan(requirements=("previous_day_stats",)))
    engine.submit(
        _timer(
            {
                "trigger_id": "AUCTION_0920",
                "close_windows": ("auction",),
                "data_requirements": ("theme_members",),
            }
        )
    )
    with pytest.raises(ValueError, match="configured EvaluationPlan"):
        engine.run_until_empty()
    assert engine._windows.views()["auction"].finality == "OPEN"


def test_unknown_planned_trigger_fails_before_window_mutation():
    engine = _engine(_plan())
    engine.submit(
        _timer({"trigger_id": "UNKNOWN", "close_windows": ("auction",)})
    )
    with pytest.raises(KeyError, match="unknown evaluation trigger"):
        engine.run_until_empty()
    assert engine._windows.views()["auction"].finality == "OPEN"


def test_plan_with_no_data_runs_the_bound_probe_strategy():
    engine = _engine(_plan())
    engine.submit(_timer())
    result = engine.run_until_empty()
    assert result.strategy_results[-1].strategy_id == "probe"
    assert result.snapshots[-1].trigger_id == "AUCTION_0920"


def test_engine_rejects_plan_capabilities_it_cannot_execute():
    with pytest.raises(TypeError, match="must be an EvaluationPlan"):
        _engine(object())
    with pytest.raises(ValueError, match="fact_functions"):
        _engine(_plan(facts=("build_segment_frame",)))
    with pytest.raises(ValueError, match="bound strategy"):
        _engine(_plan(strategies=("auction_shadow",)))


def test_evaluation_identity_binds_plan_content():
    first = _engine(_plan(version="v1", requirements=("previous_day_stats",)))
    second = _engine(_plan(version="v2", requirements=("previous_day_stats",)))
    first.submit(_timer())
    second.submit(_timer())
    first.run_until_empty()
    second.run_until_empty()
    assert set(first._pending_evaluations) != set(second._pending_evaluations)
