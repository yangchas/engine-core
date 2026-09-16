import json
from dataclasses import replace
from pathlib import Path

import pytest

from engine_core import (
    DataContext,
    DataRequest,
    DataStatus,
    FixturePreviousDayStatsProvider,
    PreviousDayStatsFunction,
    ProviderResult,
    ReadyDataStore,
    RedisPreviousDayStatsProvider,
    TDPreviousDayStatsProvider,
    TemporalDataGuard,
    build_calendar_snapshot,
    build_frozen_bundle,
    normalize_previous_day_stats_rows,
    prefetch_ready_data,
    provider_result_from_previous_day_rows,
)
from engine_core.contracts import DataResult


TEST_CALENDAR = build_calendar_snapshot(
    ["2026-09-02", "2026-09-03", "2026-09-04"],
    version="test-calendar-v1",
    declared_valid_from="2026-01-01",
    declared_valid_to="2026-12-31",
    source_guard_valid_from="2025-12-01",
    source_guard_valid_to="2027-01-31",
)


class StaticProvider:
    def __init__(self, physical):
        self.physical = physical

    def fetch(self, request, *, previous_trade_date):
        return self.physical


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
            "2026-09-03": {
                "previous_trade_date": "2026-09-03",
                "close_by_symbol": {"000001": 1000},
                "amount_by_symbol": {"000001": 100},
                "row_count": 1,
            }
        },
        observed_at_ms=1788484800000,
        available_at_ms=1788480000000,
    )
    result = PreviousDayStatsFunction(provider, TEST_CALENDAR).execute(
        DataContext(
            evaluation_id="eval-1",
            phase="AUCTION",
            observed_at_ms=1788484800000,
        ),
        _request(),
    )
    assert result.status is DataStatus.READY
    assert result.actual_trade_date == "2026-09-03"
    assert any(
        "derived_previous_trade_date=2026-09-03" in note
        for item in result.provenance
        for note in item.notes
    )


def test_calendar_provenance_uses_snapshot_observation_time():
    calendar = build_calendar_snapshot(
        ["2026-09-03", "2026-09-04"],
        version="observed-calendar-v1",
        declared_valid_from="2026-01-01",
        declared_valid_to="2026-12-31",
        source_guard_valid_from="2025-12-01",
        source_guard_valid_to="2027-01-31",
        observed_at_ms=1234,
        evidence_ref="calendar://observed",
    )
    provider = FixturePreviousDayStatsProvider(
        {
            "2026-09-03": {
                "previous_trade_date": "2026-09-03",
                "close_by_symbol": {"000001": 1000},
                "amount_by_symbol": {"000001": 100},
                "row_count": 1,
            }
        },
        observed_at_ms=1788484800000,
        available_at_ms=1788480000000,
    )
    result = PreviousDayStatsFunction(provider, calendar).execute(
        DataContext("eval-calendar-proof", "AUCTION", 1788484800000),
        _request(),
    )
    calendar_provenance = [item for item in result.provenance if item.source_kind == "calendar"]
    assert len(calendar_provenance) == 1
    assert calendar_provenance[0].observed_at_ms == 1234


def test_previous_day_wrong_date_is_stale_not_ready():
    provider = FixturePreviousDayStatsProvider(
        {"2026-09-03": {"previous_trade_date": "2026-09-02", "close_by_symbol": {"000001": 1}, "amount_by_symbol": {"000001": 1}, "row_count": 1}},
        observed_at_ms=1788484800000,
        available_at_ms=1788480000000,
    )
    result = PreviousDayStatsFunction(provider, TEST_CALENDAR).execute(
        DataContext(
            evaluation_id="eval-1",
            phase="AUCTION",
            observed_at_ms=1788484800000,
        ),
        _request(),
    )
    assert result.status is DataStatus.STALE
    assert result.actual_trade_date == "2026-09-02"


def test_previous_day_derives_date_from_calendar_and_rejects_non_trading_request():
    calls = []

    class CountingProvider:
        def fetch(self, request, *, previous_trade_date):
            calls.append(previous_trade_date)
            return ProviderResult(
                raw_data={
                    "previous_trade_date": previous_trade_date,
                    "close_by_symbol": {"000001": 1},
                    "amount_by_symbol": {"000001": 1},
                    "row_count": 1,
                },
                source_id="fixture",
                source_schema="PreviousDayStatsV1",
                effective_at_ms=None,
                available_at_ms=1,
                observed_at_ms=1,
                availability_status="VERIFIED",
            )

    request = DataRequest(
        request_id="holiday-request",
        function_id="previous_day_stats",
        trade_date="2026-09-05",
        effective_as_of_ms=1788484800000,
        knowledge_as_of_ms=1788484800000,
    )
    result = PreviousDayStatsFunction(
        CountingProvider(), TEST_CALENDAR
    ).execute(DataContext("eval-holiday", "AUCTION", 1), request)
    assert result.status is DataStatus.INVALID
    assert calls == []

    valid = PreviousDayStatsFunction(
        CountingProvider(), TEST_CALENDAR
    ).execute(DataContext("eval-valid", "AUCTION", 1), _request())
    assert valid.status is DataStatus.READY
    assert calls == ["2026-09-03"]


def test_frozen_bundle_hash_does_not_depend_on_async_completion_order():
    provider = FixturePreviousDayStatsProvider(
        {"2026-09-03": {"previous_trade_date": "2026-09-03", "close_by_symbol": {"000001": 1}, "amount_by_symbol": {"000001": 1}, "row_count": 1}},
        observed_at_ms=1788484800000,
        available_at_ms=1788480000000,
    )
    function = PreviousDayStatsFunction(provider, TEST_CALENDAR)
    result_a = function.execute(
        DataContext("eval-1", "AUCTION", 1788484800000),
        _request(),
    )
    result_b = function.execute(
        DataContext("eval-1", "AUCTION", 1788484800000),
        _request(),
    )
    left = build_frozen_bundle(
        "eval-1",
        1788484800000,
        ("previous_day_stats",),
        {"previous_day_stats": result_a},
    )
    right = build_frozen_bundle(
        "eval-1",
        1788484800000,
        ("previous_day_stats",),
        {"previous_day_stats": result_b},
    )
    assert left.function_order == ("previous_day_stats",)
    assert left.content_hash == right.content_hash


def _ready_result(*, effective_at_ms=None, available_at_ms=None, observed_at_ms=None):
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
        observed_at_ms=(
            1788484800000 if observed_at_ms is None else observed_at_ms
        ),
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


def test_temporal_guard_rejects_unknown_availability_even_when_preobserved():
    result = TemporalDataGuard.check(
        _ready_result(effective_at_ms=None, available_at_ms=None),
        _request(),
    )
    assert result.status is DataStatus.UNAVAILABLE
    assert "available_at_unknown" in result.missing_fields


def test_temporal_guard_does_not_use_observed_at_as_knowledge_cutoff():
    request = _request()
    result = TemporalDataGuard.check(
        _ready_result(
            effective_at_ms=None,
            available_at_ms=request.knowledge_as_of_ms,
            observed_at_ms=request.knowledge_as_of_ms + 1,
        ),
        request,
    )
    assert result.status is DataStatus.READY
    assert "observed_at_after_knowledge_cutoff" not in result.missing_fields


def test_previous_day_function_never_promotes_temporally_unavailable_result():
    class FutureProvider:
        def fetch(self, request, *, previous_trade_date):
            return ProviderResult(
                raw_data={"previous_trade_date": "2026-09-03", "close_by_symbol": {"000001": 1}, "amount_by_symbol": {"000001": 1}, "row_count": 1},
                source_id="fixture",
                source_schema="PreviousDayStatsV1",
                effective_at_ms=request.effective_as_of_ms,
                available_at_ms=request.knowledge_as_of_ms + 1,
                observed_at_ms=request.knowledge_as_of_ms,
                availability_status="VERIFIED",
            )

    result = PreviousDayStatsFunction(FutureProvider(), TEST_CALENDAR).execute(
        DataContext("eval-1", "AUCTION", 1788484800000),
        _request(),
    )
    assert result.status is DataStatus.UNAVAILABLE


def test_td_provider_wraps_existing_access_without_reimplementing_connection():
    def legacy_rows(previous_trade_date, symbols):
        assert previous_trade_date == "2026-09-03"
        assert symbols == ()
        return [{"symbol": "000001", "close": 11.59, "amount": 100}]

    provider = TDPreviousDayStatsProvider(
        legacy_rows,
        observed_at_ms=lambda: 1788484800000,
        available_at_ms=lambda: 1788480000000,
        evidence_ref="probe/td/daily_kline",
    )
    result = PreviousDayStatsFunction(provider, TEST_CALENDAR).execute(
        DataContext(
            "eval-1",
            "AUCTION",
            1788484800000,
        ),
        _request(),
    )
    assert result.status is DataStatus.READY
    assert result.actual_source == "tdengine_daily_kline"
    assert result.provenance[0].evidence_ref == "probe/td/daily_kline"
    assert result.available_at_ms == 1788480000000


def test_td_provider_samples_observed_time_after_legacy_fetch_returns():
    clock = [100]

    def legacy_rows(previous_trade_date, symbols):
        assert previous_trade_date == "2026-09-03"
        clock[0] = 200
        return [{"symbol": "000001", "close": 11.59, "amount": 100}]

    provider = TDPreviousDayStatsProvider(
        legacy_rows,
        observed_at_ms=lambda: clock[0],
        source_id="td-observation-order",
    )
    result = PreviousDayStatsFunction(provider, TEST_CALENDAR).execute(
        DataContext("eval-observation-order", "AUCTION", 200),
        _request(),
    )
    assert result.observed_at_ms == 200


def test_td_provider_samples_observed_time_after_lazy_rows_are_consumed():
    clock = [100]

    def lazy_rows(previous_trade_date, symbols):
        yield {"symbol": "000001", "close": 11.59, "amount": 100}
        clock[0] = 300

    provider = TDPreviousDayStatsProvider(
        lazy_rows,
        observed_at_ms=lambda: clock[0],
        source_id="td-lazy-observation-order",
    )
    result = PreviousDayStatsFunction(provider, TEST_CALENDAR).execute(
        DataContext("eval-lazy-observation-order", "AUCTION", 300),
        _request(),
    )
    assert result.observed_at_ms == 300


def test_td_provider_access_error_remains_error_not_missing():
    def failing_rows(previous_trade_date, symbols):
        raise TimeoutError("legacy TD timeout")

    provider = TDPreviousDayStatsProvider(
        failing_rows,
        observed_at_ms=lambda: 1788484800000,
        source_id="td-timeout",
    )
    result = PreviousDayStatsFunction(provider, TEST_CALENDAR).execute(
        DataContext("eval-timeout", "AUCTION", 1788484800000),
        _request(),
    )
    assert result.status is DataStatus.ERROR
    assert "error" in result.missing_fields


def test_redis_previous_day_provider_normalizes_real_cache_shape():
    def cache_rows(previous_trade_date, symbols):
        assert previous_trade_date == "2026-09-03"
        assert symbols == ("000001",)
        return [
            {
                "symbol": "000001",
                "trade_date": "2026-09-03",
                "preclose": 11.40,
                "close": 11.59,
                "pct_chg": 1.67,
                "amount": 123456789.5,
                "source": "baostock",
            }
        ]

    result = PreviousDayStatsFunction(
        RedisPreviousDayStatsProvider(
            cache_rows,
            observed_at_ms=lambda: 1788484800000,
            available_at_ms=lambda: 1788480000000,
            evidence_ref="redis://cache:kline_ready/2026-09-03",
        ),
        TEST_CALENDAR,
    ).execute(
        DataContext("eval-redis-cache", "AUCTION", 1788484800000),
        replace(_request(), symbols=("000001",)),
    )

    assert result.status is DataStatus.READY
    assert result.actual_source == "redis_daily_kline_cache"
    assert result.data["close_by_symbol"]["000001"] == 11.59
    assert result.data["amount_by_symbol"]["000001"] == 123456789.5
    assert result.provenance[0].evidence_ref == "redis://cache:kline_ready/2026-09-03"


def test_redis_previous_day_provider_unknown_availability_fails_closed():
    provider = RedisPreviousDayStatsProvider(
        lambda previous, symbols: [
            {"symbol": "000001", "close": 11.59, "amount": 100}
        ],
        observed_at_ms=lambda: 1788484800000,
    )
    result = PreviousDayStatsFunction(provider, TEST_CALENDAR).execute(
        DataContext("eval-redis-observed", "AUCTION", 1788484800000),
        replace(_request(), symbols=("000001",)),
    )

    assert result.status is DataStatus.UNAVAILABLE
    assert result.available_at_ms is None
    assert "available_at_unknown" in result.missing_fields


def test_redis_previous_day_provider_access_error_is_not_empty_data():
    def broken_cache(previous_trade_date, symbols):
        raise TimeoutError("Redis read timeout")

    result = PreviousDayStatsFunction(
        RedisPreviousDayStatsProvider(
            broken_cache,
            observed_at_ms=lambda: 1788484800000,
        ),
        TEST_CALENDAR,
    ).execute(
        DataContext("eval-redis-error", "AUCTION", 1788484800000),
        replace(_request(), symbols=("000001",)),
    )

    assert result.status is DataStatus.ERROR
    assert "error" in result.missing_fields


def test_observed_provider_does_not_promote_availability_to_source_claim():
    def observed_rows(previous_trade_date, symbols):
        return [{"symbol": "000001", "close": 11.59, "amount": 100}]

    result = PreviousDayStatsFunction(
        TDPreviousDayStatsProvider(
            observed_rows,
            observed_at_ms=lambda: 1788484800000,
            source_id="network_oracle",
            source_schema="historical_result",
        ),
        TEST_CALENDAR,
    ).execute(
        DataContext("eval-1", "AUCTION", 1788484800000),
        _request(),
    )
    assert result.status is DataStatus.UNAVAILABLE
    assert result.available_at_ms is None


def test_prefetch_ready_data_reuses_preobserved_result_at_later_node():
    prefetch_time = 1788484740000
    node_time = 1788484800000

    def observed_rows(previous_trade_date, symbols):
        assert previous_trade_date == "2026-09-03"
        assert symbols == ("000001",)
        return [{"symbol": "000001", "close": 11.59, "amount": 100}]

    function = PreviousDayStatsFunction(
        TDPreviousDayStatsProvider(
            observed_rows,
            observed_at_ms=lambda: prefetch_time,
            available_at_ms=lambda: prefetch_time,
            source_id="td-prefetch-fixture",
        ),
        TEST_CALENDAR,
    )
    prefetch_request = DataRequest(
        request_id="prefetch-request",
        function_id="previous_day_stats",
        trade_date="2026-09-04",
        effective_as_of_ms=node_time,
        knowledge_as_of_ms=prefetch_time,
        symbols=("000001",),
    )
    context = DataContext(
        "eval-prefetch",
        "AUCTION",
        prefetch_time,
    )
    store = ReadyDataStore()
    result = prefetch_ready_data(function, context, prefetch_request, store)

    assert result.status is DataStatus.READY
    assert result.observed_at_ms == prefetch_time
    assert result.available_at_ms == prefetch_time
    assert len(store) == 1

    node_request = DataRequest(
        request_id="node-request",
        function_id="previous_day_stats",
        trade_date="2026-09-04",
        effective_as_of_ms=node_time,
        knowledge_as_of_ms=node_time,
        symbols=("000001",),
    )
    cached = store.get(node_request)
    assert cached is not None
    assert cached.status is DataStatus.READY
    assert cached.available_at_ms == prefetch_time
    bundle = build_frozen_bundle(
        "eval-prefetch",
        node_time,
        ("previous_day_stats",),
        {"previous_day_stats": cached},
    )
    assert bundle.completeness == 1.0


def test_ready_data_store_rejects_request_before_observation():
    prefetch_time = 1788484740000
    request = DataRequest(
        request_id="prefetch-request",
        function_id="previous_day_stats",
        trade_date="2026-09-04",
        effective_as_of_ms=1788484800000,
        knowledge_as_of_ms=prefetch_time,
        symbols=("000001",),
    )
    result = DataResult(
        request_id="prefetch-request",
        function_id="previous_day_stats",
        status=DataStatus.READY,
        data={"previous_trade_date": "2026-09-03"},
        actual_source="fixture",
        requested_trade_date="2026-09-04",
        actual_trade_date="2026-09-03",
        effective_at_ms=None,
        available_at_ms=prefetch_time,
        observed_at_ms=prefetch_time,
        schema_version=1,
        completeness=1.0,
    )
    store = ReadyDataStore()
    store.put(request, result)
    too_early = DataRequest(
        request_id="early-request",
        function_id="previous_day_stats",
        trade_date="2026-09-04",
        effective_as_of_ms=1788484800000,
        knowledge_as_of_ms=prefetch_time - 1,
        symbols=("000001",),
    )
    assert store.get(too_early) is None


def test_ready_data_store_rejects_unobserved_put():
    request = DataRequest(
        request_id="late-request",
        function_id="previous_day_stats",
        trade_date="2026-09-04",
        effective_as_of_ms=1788484800000,
        knowledge_as_of_ms=1788484800000,
        symbols=("000001",),
    )
    late = _ready_result(
        effective_at_ms=None,
        available_at_ms=None,
        observed_at_ms=request.knowledge_as_of_ms + 1,
    )
    with pytest.raises(ValueError, match="available_at_unknown"):
        ReadyDataStore().put(request, late)


def test_ready_data_store_does_not_reuse_result_missing_later_required_field():
    prefetch_request = DataRequest(
        request_id="prefetch-narrow",
        function_id="previous_day_stats",
        trade_date="2026-09-04",
        effective_as_of_ms=1788484800000,
        knowledge_as_of_ms=1788484800000,
        required_fields=("previous_trade_date",),
    )
    result = DataResult(
        request_id="prefetch-narrow",
        function_id="previous_day_stats",
        status=DataStatus.READY,
        data={"previous_trade_date": "2026-09-03"},
        actual_source="fixture",
        requested_trade_date="2026-09-04",
        actual_trade_date="2026-09-03",
        effective_at_ms=None,
        available_at_ms=1788480000000,
        observed_at_ms=1788484800000,
        schema_version=1,
        completeness=1.0,
    )
    store = ReadyDataStore()
    store.put(prefetch_request, result)
    stricter_request = DataRequest(
        request_id="node-strict",
        function_id="previous_day_stats",
        trade_date="2026-09-04",
        effective_as_of_ms=1788484800000,
        knowledge_as_of_ms=1788484800000,
        required_fields=("previous_trade_date", "volume_by_symbol"),
    )
    assert store.get(stricter_request) is None


def test_ready_data_store_rejects_result_for_different_requested_trade_date():
    request = DataRequest(
        request_id="date-mismatch",
        function_id="previous_day_stats",
        trade_date="2026-09-04",
        effective_as_of_ms=1788484800000,
        knowledge_as_of_ms=1788484800000,
    )
    result = DataResult(
        request_id="date-mismatch",
        function_id="previous_day_stats",
        status=DataStatus.READY,
        data={"previous_trade_date": "2026-09-02"},
        actual_source="fixture",
        requested_trade_date="2026-09-03",
        actual_trade_date="2026-09-02",
        effective_at_ms=None,
        available_at_ms=1788480000000,
        observed_at_ms=1788484800000,
        schema_version=1,
        completeness=1.0,
    )
    with pytest.raises(ValueError, match="requested_trade_date"):
        ReadyDataStore().put(request, result)


def test_data_result_semantic_hash_excludes_provider_identity():
    physical = provider_result_from_previous_day_rows(
        [{"symbol": "000001", "close": 11.59, "amount": 100}],
        actual_trade_date="2026-09-03",
        source_id="source-a",
        source_schema="schema-a",
        effective_at_ms=1788393600000,
        available_at_ms=1788480000000,
        observed_at_ms=1788484800000,
        availability_status="VERIFIED",
    )
    other = provider_result_from_previous_day_rows(
        [{"symbol": "000001", "close": 11.59, "amount": 100}],
        actual_trade_date="2026-09-03",
        source_id="source-b",
        source_schema="schema-b",
        effective_at_ms=1788393600000,
        available_at_ms=1788480000000,
        observed_at_ms=1788484801000,
        availability_status="VERIFIED",
    )
    left = PreviousDayStatsFunction(StaticProvider(physical), TEST_CALENDAR).execute(
        DataContext("eval-identity", "AUCTION", 1788484800000),
        _request(),
    )
    right = PreviousDayStatsFunction(StaticProvider(other), TEST_CALENDAR).execute(
        DataContext("eval-identity", "AUCTION", 1788484800000),
        _request(),
    )
    assert left.content_hash == right.content_hash
    assert left.actual_source != right.actual_source


def test_captured_td_previous_day_fixture_runs_through_data_function():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/data/previous_day_stats_20260903.json").read_text(
            encoding="utf-8"
        )
    )
    provider = FixturePreviousDayStatsProvider({
        fixture["previous_trade_date"]: {
            "previous_trade_date": fixture["previous_trade_date"],
            "close_by_symbol": {
                symbol: row["close"] for symbol, row in fixture["rows"].items()
            },
            "amount_by_symbol": {
                symbol: row["amount"] for symbol, row in fixture["rows"].items()
            },
            "volume_by_symbol": {
                symbol: row["volume"] for symbol, row in fixture["rows"].items()
            },
            "row_count": len(fixture["rows"]),
        }
    }, observed_at_ms=1788484800000, available_at_ms=1788480000000)
    result = PreviousDayStatsFunction(provider, TEST_CALENDAR).execute(
        DataContext(
            "eval-real-fixture",
            "AUCTION",
            1788484800000,
        ),
        _request(),
    )
    assert result.status is DataStatus.READY
    assert result.data["close_by_symbol"]["000001"] == 11.880000114440918
    assert result.data["amount_by_symbol"]["000001"] == 1324230272.0


def test_fixture_provider_deep_freezes_constructor_input():
    values = {
        "2026-09-03": {
            "previous_trade_date": "2026-09-03",
            "close_by_symbol": {"000001": 1},
            "amount_by_symbol": {"000001": 2},
            "row_count": 1,
        }
    }
    provider = FixturePreviousDayStatsProvider(
        values,
        observed_at_ms=1788484800000,
        available_at_ms=1788480000000,
    )
    values["2026-09-03"]["close_by_symbol"]["000001"] = 99
    result = PreviousDayStatsFunction(provider, TEST_CALENDAR).execute(
        DataContext("eval-frozen", "AUCTION", 1788484800000), _request()
    )
    assert result.data["close_by_symbol"]["000001"] == 1


def test_legacy_daily_rows_normalize_without_symbol_or_zero_repair():
    payload = normalize_previous_day_stats_rows(
        [
            {"symbol": "600000", "close": 9.27, "amount": 0, "volume": 0},
            {"symbol": "000001", "close": 11.88, "amount": 10, "volume": 2},
        ],
        actual_trade_date="2026-09-03",
    )
    assert tuple(payload["close_by_symbol"]) == ("000001", "600000")
    assert payload["amount_by_symbol"]["600000"] == 0
    assert payload["volume_by_symbol"]["600000"] == 0


def test_legacy_daily_rows_fail_closed_on_qualified_or_incomplete_symbol():
    with pytest.raises(ValueError):
        normalize_previous_day_stats_rows(
            [{"symbol": "600000.SH", "close": 1, "amount": 1}],
            actual_trade_date="2026-09-03",
        )
    with pytest.raises(ValueError):
        normalize_previous_day_stats_rows(
            [{"symbol": "600000", "close": 1}],
            actual_trade_date="2026-09-03",
        )


def test_provider_result_from_legacy_rows_preserves_temporal_metadata():
    physical = provider_result_from_previous_day_rows(
        [{"symbol": "000001", "close": 11.88, "amount": 100}],
        actual_trade_date="2026-09-03",
        source_id="tdengine_daily_kline",
        source_schema="daily_kline",
        effective_at_ms=1788393600000,
        available_at_ms=1788480000000,
        observed_at_ms=1788484800000,
        availability_status="VERIFIED",
        evidence_ref="probe/td/daily_kline",
    )
    result = PreviousDayStatsFunction(
        StaticProvider(physical),
        TEST_CALENDAR,
    ).execute(
        DataContext(
            "eval-rows",
            "AUCTION",
            1788484800000,
        ),
        _request(),
    )
    assert result.status is DataStatus.READY
    assert result.actual_source == "tdengine_daily_kline"
    assert result.data["close_by_symbol"]["000001"] == 11.88
    assert result.provenance[0].evidence_ref == "probe/td/daily_kline"


def test_empty_legacy_daily_rows_are_missing_not_ready():
    physical = provider_result_from_previous_day_rows(
        [],
        actual_trade_date="2026-09-03",
        source_id="tdengine_daily_kline",
        source_schema="daily_kline",
        effective_at_ms=1788393600000,
        available_at_ms=1788480000000,
        observed_at_ms=1788484800000,
        availability_status="VERIFIED",
    )
    result = PreviousDayStatsFunction(
        StaticProvider(physical),
        TEST_CALENDAR,
    ).execute(
        DataContext(
            "eval-empty-rows",
            "AUCTION",
            1788484800000,
        ),
        _request(),
    )
    assert result.status is DataStatus.MISSING
    assert result.completeness == 0.0
