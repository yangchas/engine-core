import pytest

from engine_core import (
    AUCTION_REFERENCE_FUNCTION_ORDER,
    DataContext,
    DataStatus,
    FixturePreviousDayStatsProvider,
    HotPlatesFunction,
    PreviousDayLimitPoolFunction,
    PreviousDayStatsFunction,
    PendingEvaluationRequest,
    RedisHotPlatesProvider,
    RedisPreviousDayLimitPoolProvider,
    DeterministicEngine,
    EngineSignal,
    EvaluationNode,
    EvaluationPlan,
    MarketStateReducer,
    ProbeStrategy,
    SignalKind,
    WindowManager,
    WindowSpec,
    assess_startup_readiness,
    build_a_share_session_plan,
    build_auction_reference_bundle,
    build_calendar_snapshot,
    local_datetime_ms,
    prepare_auction_references,
)


TRADE_DATE = "2026-09-16"
PREVIOUS_DATE = "2026-09-15"
CUTOFF = local_datetime_ms(TRADE_DATE, "09:19:00")


def _calendar():
    return build_calendar_snapshot(
        (PREVIOUS_DATE, TRADE_DATE),
        version="auction-reference-test-v1",
        declared_valid_from=TRADE_DATE,
        declared_valid_to=TRADE_DATE,
        source_guard_valid_from=PREVIOUS_DATE,
        source_guard_valid_to=TRADE_DATE,
    )


def _functions(calendar, *, available_at_ms=CUTOFF - 1):
    stats = PreviousDayStatsFunction(
        FixturePreviousDayStatsProvider(
            {
                PREVIOUS_DATE: {
                    "previous_trade_date": PREVIOUS_DATE,
                    "close_by_symbol": {"000001": 10.0},
                    "amount_by_symbol": {"000001": 1000},
                    "row_count": 1,
                }
            },
            observed_at_ms=CUTOFF + 10,
            available_at_ms=available_at_ms,
        ),
        calendar,
    )
    limit_pool = PreviousDayLimitPoolFunction(
        RedisPreviousDayLimitPoolProvider(
            lambda date: (
                {
                    "trade_date": PREVIOUS_DATE,
                    "symbol": "000001",
                    "name": "sample",
                    "lb_days": 1,
                    "turnover": 10.0,
                    "close_pct": 10.0,
                    "plate": "bank",
                    "reason": "sample",
                    "seal_time": "09:31:00",
                    "source": "kaipan",
                },
            ),
            observed_at_ms=lambda: CUTOFF + 20,
            available_at_ms=lambda: available_at_ms,
            verified_field_units={"turnover": "percent"},
        ),
        calendar,
    )
    hot = HotPlatesFunction(
        RedisHotPlatesProvider(
            lambda date: (
                {
                    "trade_date": TRADE_DATE,
                    "plate_name": "bank",
                    "rank": 1,
                    "strength": 10.0,
                    "hot": 10.0,
                    "change_pct": 1.0,
                    "net_inflow_yi": 1.0,
                    "source": "kaipan",
                },
            ),
            observed_at_ms=lambda: CUTOFF + 30,
            available_at_ms=lambda: available_at_ms,
            verified_field_units={
                "rank": "ordinal",
                "strength": "score",
                "hot": "score",
                "change_pct": "percent",
                "net_inflow_yi": "yi",
            },
        ),
        calendar,
    )
    return stats, limit_pool, hot


def test_prepare_auction_references_is_fixed_order_and_calendar_derived():
    calendar = _calendar()
    stats, limit_pool, hot = _functions(calendar)
    prepared = prepare_auction_references(
        trade_date=TRADE_DATE,
        knowledge_as_of_ms=CUTOFF,
        context=DataContext("auction-ref", "LIVE_SHADOW", CUTOFF + 100),
        calendar=calendar,
        previous_day_stats=stats,
        previous_day_limit_pool=limit_pool,
        hot_plates=hot,
        symbols=("000001",),
    )

    assert prepared.previous_trade_date == PREVIOUS_DATE
    assert tuple(prepared.as_mapping()) == AUCTION_REFERENCE_FUNCTION_ORDER
    assert all(result.status is DataStatus.READY for result in prepared.as_mapping().values())
    assert prepared.as_mapping()["previous_day_stats"].actual_trade_date == PREVIOUS_DATE
    assert prepared.as_mapping()["previous_day_limit_pool"].actual_trade_date == PREVIOUS_DATE
    assert prepared.as_mapping()["hot_plates"].actual_trade_date == TRADE_DATE
    assert prepared.content_hash


def test_prepared_references_feed_existing_startup_readiness_without_date_injection():
    calendar = _calendar()
    stats, limit_pool, hot = _functions(calendar)
    prepared = prepare_auction_references(
        trade_date=TRADE_DATE,
        knowledge_as_of_ms=CUTOFF,
        context=DataContext("auction-ref", "LIVE_SHADOW", CUTOFF + 100),
        calendar=calendar,
        previous_day_stats=stats,
        previous_day_limit_pool=limit_pool,
        hot_plates=hot,
    )
    readiness = assess_startup_readiness(
        TRADE_DATE,
        CUTOFF,
        calendar,
        build_a_share_session_plan(TRADE_DATE, calendar),
        q2=None,
        required_reference_functions=AUCTION_REFERENCE_FUNCTION_ORDER,
        reference_results=prepared.as_mapping(),
    )

    assert readiness.reference_statuses == tuple(
        (function_id, "READY") for function_id in AUCTION_REFERENCE_FUNCTION_ORDER
    )
    assert readiness.status == "BLOCKED"  # Q2 remains an independent gate.


def test_unknown_availability_remains_unavailable_instead_of_using_observation_time():
    calendar = _calendar()
    stats, limit_pool, hot = _functions(calendar, available_at_ms=None)
    prepared = prepare_auction_references(
        trade_date=TRADE_DATE,
        knowledge_as_of_ms=CUTOFF,
        context=DataContext("auction-ref", "LIVE_SHADOW", CUTOFF + 100),
        calendar=calendar,
        previous_day_stats=stats,
        previous_day_limit_pool=limit_pool,
        hot_plates=hot,
    )

    assert all(
        result.status is DataStatus.UNAVAILABLE
        for result in prepared.as_mapping().values()
    )


def test_preparation_hash_is_stable_for_same_semantic_results():
    calendar = _calendar()
    first_functions = _functions(calendar)
    second_functions = _functions(calendar)
    first = prepare_auction_references(
        trade_date=TRADE_DATE,
        knowledge_as_of_ms=CUTOFF,
        context=DataContext("one", "LIVE_SHADOW", CUTOFF + 100),
        calendar=calendar,
        previous_day_stats=first_functions[0],
        previous_day_limit_pool=first_functions[1],
        hot_plates=first_functions[2],
    )
    second = prepare_auction_references(
        trade_date=TRADE_DATE,
        knowledge_as_of_ms=CUTOFF,
        context=DataContext("two", "LIVE_SHADOW", CUTOFF + 200),
        calendar=calendar,
        previous_day_stats=second_functions[0],
        previous_day_limit_pool=second_functions[1],
        hot_plates=second_functions[2],
    )

    assert first.content_hash == second.content_hash


def test_prepared_references_complete_public_engine_evaluation():
    calendar = _calendar()
    stats, limit_pool, hot = _functions(calendar)
    prepared = prepare_auction_references(
        trade_date=TRADE_DATE,
        knowledge_as_of_ms=CUTOFF,
        context=DataContext("auction-ref", "LIVE_SHADOW", CUTOFF + 100),
        calendar=calendar,
        previous_day_stats=stats,
        previous_day_limit_pool=limit_pool,
        hot_plates=hot,
        symbols=("000001",),
    )
    plan = EvaluationPlan(
        "auction-reference-plan",
        "v1",
        (
            EvaluationNode(
                "auction-0920",
                "AUCTION_0920",
                data_requirements=AUCTION_REFERENCE_FUNCTION_ORDER,
                strategies=("probe",),
            ),
        ),
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction", CUTOFF - 1000, CUTOFF + 1000),)),
        ProbeStrategy(),
        session_id=TRADE_DATE,
        phase="AUCTION",
        evaluation_plan=plan,
    )
    engine.submit(
        EngineSignal(
            "auction-0920-timer",
            CUTOFF,
            1,
            SignalKind.TIMER,
            {"trigger_id": "AUCTION_0920"},
        )
    )
    pending = engine.run_until_empty().pending_evaluations[0]
    bundle = build_auction_reference_bundle(pending, prepared)
    engine.submit(
        EngineSignal(
            "auction-0920-data-ready",
            CUTOFF + 100,
            2,
            SignalKind.DATA_READY,
            {"evaluation_id": pending.evaluation_id, "bundle": bundle},
        )
    )
    completed = engine.run_until_empty()
    assert completed.pending_evaluations == ()
    assert completed.strategy_results[-1].evaluation_id == pending.evaluation_id
    assert completed.strategy_results[-1].trace["bundle_hash"] == bundle.content_hash


def test_prefetch_before_evaluation_cutoff_can_complete_later_evaluation():
    calendar = _calendar()
    stats, limit_pool, hot = _functions(calendar)
    prepared = prepare_auction_references(
        trade_date=TRADE_DATE,
        knowledge_as_of_ms=CUTOFF,
        context=DataContext("auction-ref", "LIVE_SHADOW", CUTOFF + 100),
        calendar=calendar,
        previous_day_stats=stats,
        previous_day_limit_pool=limit_pool,
        hot_plates=hot,
    )
    pending = PendingEvaluationRequest(
        evaluation_id="evaluation:later",
        trigger_id="AUCTION_0924",
        knowledge_as_of_ms=CUTOFF + 60_000,
        function_order=AUCTION_REFERENCE_FUNCTION_ORDER,
        snapshot_content_hash="snapshot-hash",
    )

    bundle = build_auction_reference_bundle(pending, prepared)

    assert bundle.knowledge_as_of_ms == CUTOFF + 60_000
    assert tuple(bundle.results_by_function) == AUCTION_REFERENCE_FUNCTION_ORDER
    assert tuple(
        bundle.results_by_function[function_id].content_hash
        for function_id in AUCTION_REFERENCE_FUNCTION_ORDER
    ) == tuple(
        prepared.as_mapping()[function_id].content_hash
        for function_id in AUCTION_REFERENCE_FUNCTION_ORDER
    )


def test_reference_bundle_rejects_a_different_evaluation_cutoff():
    calendar = _calendar()
    stats, limit_pool, hot = _functions(calendar)
    prepared = prepare_auction_references(
        trade_date=TRADE_DATE,
        knowledge_as_of_ms=CUTOFF,
        context=DataContext("auction-ref", "LIVE_SHADOW", CUTOFF + 100),
        calendar=calendar,
        previous_day_stats=stats,
        previous_day_limit_pool=limit_pool,
        hot_plates=hot,
    )
    pending = PendingEvaluationRequest(
        evaluation_id="evaluation:mismatch",
        trigger_id="AUCTION_0920",
        knowledge_as_of_ms=CUTOFF - 1,
        function_order=AUCTION_REFERENCE_FUNCTION_ORDER,
        snapshot_content_hash="snapshot-hash",
    )
    with pytest.raises(ValueError, match="cutoff"):
        build_auction_reference_bundle(pending, prepared)
