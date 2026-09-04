from engine_core import (
    DataContext,
    DataRequest,
    DataStatus,
    CallablePreviousDayStatsProvider,
    FixturePreviousDayStatsProvider,
    PreviousDayStatsFunction,
    ProviderResult,
    TemporalDataGuard,
    freeze_data_results,
)
from engine_core.contracts import DataResult


def _request(function_id="previous_day_stats"):
    return DataRequest(
        request_id=function_id + "-request",
        function_id=function_id,
        trade_date="2026-09-04",
        effective_as_of_ms=1788484800000,
        knowledge_as_of_ms=1788484800000,
    )


def test_previous_day_function_preserves_business_date_semantics():
    provider = FixturePreviousDayStatsProvider(
        {
            "2026-09-04": {
                "previous_trade_date": "2026-09-03",
                "close_by_symbol": {"000001": 1000},
            }
        }
    )
    result = PreviousDayStatsFunction(provider).execute(
        DataContext(
            evaluation_id="eval-1",
            phase="AUCTION",
            observed_at_ms=1788484800000,
            expected_previous_trade_date="2026-09-03",
        ),
        _request(),
    )
    assert result.status is DataStatus.READY
    assert result.actual_trade_date == "2026-09-03"


def test_previous_day_wrong_date_is_stale_not_ready():
    provider = FixturePreviousDayStatsProvider(
        {"2026-09-04": {"previous_trade_date": "2026-09-02"}}
    )
    result = PreviousDayStatsFunction(provider).execute(
        DataContext(
            evaluation_id="eval-1",
            phase="AUCTION",
            observed_at_ms=1788484800000,
            expected_previous_trade_date="2026-09-03",
        ),
        _request(),
    )
    assert result.status is DataStatus.STALE
    assert result.actual_trade_date == "2026-09-02"


def test_frozen_bundle_hash_does_not_depend_on_async_completion_order():
    provider = FixturePreviousDayStatsProvider(
        {"2026-09-04": {"previous_trade_date": "2026-09-03"}}
    )
    function = PreviousDayStatsFunction(provider)
    result_a = function.execute(
        DataContext("eval-1", "AUCTION", 1788484800000),
        _request(),
    )
    result_b = function.execute(
        DataContext("eval-1", "AUCTION", 1788484800000),
        _request(),
    )
    left = freeze_data_results(
        "eval-1",
        1788484800000,
        ("previous_day_stats",),
        {"previous_day_stats": result_a},
    )
    right = freeze_data_results(
        "eval-1",
        1788484800000,
        ("previous_day_stats",),
        {"previous_day_stats": result_b},
    )
    assert left.function_order == ("previous_day_stats",)
    assert left.content_hash == right.content_hash


def _ready_result(*, effective_at_ms=None, available_at_ms=None):
    return DataResult(
        request_id="r",
        function_id="previous_day_stats",
        status=DataStatus.READY,
        data={"previous_trade_date": "2026-09-03"},
        actual_source="fixture",
        requested_trade_date="2026-09-04",
        actual_trade_date="2026-09-03",
        effective_at_ms=effective_at_ms,
        available_at_ms=available_at_ms,
        observed_at_ms=1788484800000,
        schema_version=1,
        completeness=1.0,
    )


def test_temporal_guard_rejects_future_effective_and_availability_times():
    request = _request()
    future = _ready_result(
        effective_at_ms=request.effective_as_of_ms + 1,
        available_at_ms=request.knowledge_as_of_ms + 1,
    )
    guarded = TemporalDataGuard.check(future, request)
    assert guarded.status is DataStatus.UNAVAILABLE
    assert set(guarded.missing_fields) == {
        "effective_at_after_cutoff",
        "available_at_after_knowledge_cutoff",
    }


def test_temporal_guard_rejects_unknown_availability_for_runtime_data():
    result = TemporalDataGuard.check(
        _ready_result(effective_at_ms=None, available_at_ms=None),
        _request(),
    )
    assert result.status is DataStatus.UNAVAILABLE
    assert "available_at_unknown" in result.missing_fields


def test_previous_day_function_never_promotes_temporally_unavailable_result():
    class FutureProvider:
        def fetch(self, request):
            return _ready_result(
                effective_at_ms=request.effective_as_of_ms,
                available_at_ms=request.knowledge_as_of_ms + 1,
            )

    result = PreviousDayStatsFunction(FutureProvider()).execute(
        DataContext("eval-1", "AUCTION", 1788484800000),
        _request(),
    )
    assert result.status is DataStatus.UNAVAILABLE


def test_callable_provider_wraps_existing_access_without_reimplementing_connection():
    def legacy_access(request):
        assert request.trade_date == "2026-09-04"
        return ProviderResult(
            raw_data={
                "previous_trade_date": "2026-09-03",
                "close_by_symbol": {"000001": 1159},
            },
            source_id="tdengine_daily_kline",
            source_schema="legacy_daily_kline",
            effective_at_ms=1788393600000,
            available_at_ms=1788480000000,
            observed_at_ms=1788484800000,
            availability_status="VERIFIED",
            evidence_ref="probe/td/daily_kline",
        )

    provider = CallablePreviousDayStatsProvider(legacy_access)
    result = PreviousDayStatsFunction(provider).execute(
        DataContext(
            "eval-1",
            "AUCTION",
            1788484800000,
            expected_previous_trade_date="2026-09-03",
        ),
        _request(),
    )
    assert result.status is DataStatus.READY
    assert result.actual_source == "tdengine_daily_kline"
    assert result.provenance[0].evidence_ref == "probe/td/daily_kline"


def test_callable_provider_does_not_promote_observed_availability_to_runtime():
    def observed_access(request):
        return ProviderResult(
            raw_data={"previous_trade_date": "2026-09-03"},
            source_id="network_oracle",
            source_schema="historical_result",
            effective_at_ms=request.effective_as_of_ms,
            available_at_ms=request.knowledge_as_of_ms,
            observed_at_ms=request.knowledge_as_of_ms,
            availability_status="OBSERVED",
        )

    result = PreviousDayStatsFunction(
        CallablePreviousDayStatsProvider(observed_access)
    ).execute(
        DataContext("eval-1", "AUCTION", 1788484800000),
        _request(),
    )
    assert result.status is DataStatus.UNAVAILABLE
