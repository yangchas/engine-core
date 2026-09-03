from engine_core import (
    DataContext,
    DataRequest,
    DataStatus,
    FixturePreviousDayStatsProvider,
    PreviousDayStatsFunction,
    freeze_data_results,
)


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
