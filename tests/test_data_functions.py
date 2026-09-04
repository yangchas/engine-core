import json
from pathlib import Path

import pytest

from engine_core import (
    DataContext,
    DataRequest,
    DataStatus,
    FixturePreviousDayStatsProvider,
    PreviousDayStatsFunction,
    ProviderResult,
    TDPreviousDayStatsProvider,
    TemporalDataGuard,
    build_frozen_bundle,
    normalize_previous_day_stats_rows,
    provider_result_from_previous_day_rows,
)
from engine_core.contracts import DataResult


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
        {"2026-09-03": {"previous_trade_date": "2026-09-02"}}
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
        {"2026-09-03": {"previous_trade_date": "2026-09-03"}}
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
        def fetch(self, request, *, previous_trade_date):
            return ProviderResult(
                raw_data={"previous_trade_date": "2026-09-03"},
                source_id="fixture",
                source_schema="PreviousDayStatsV1",
                effective_at_ms=request.effective_as_of_ms,
                available_at_ms=request.knowledge_as_of_ms + 1,
                observed_at_ms=request.knowledge_as_of_ms,
                availability_status="VERIFIED",
            )

    result = PreviousDayStatsFunction(FutureProvider()).execute(
        DataContext("eval-1", "AUCTION", 1788484800000, expected_previous_trade_date="2026-09-03"),
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
        evidence_ref="probe/td/daily_kline",
    )
    result = PreviousDayStatsFunction(provider).execute(
        DataContext(
            "eval-1",
            "AUCTION",
            1788484800000,
            expected_previous_trade_date="2026-09-03",
        ),
        _request(),
    )
    assert result.status is DataStatus.UNAVAILABLE
    assert result.actual_source == "tdengine_daily_kline"
    assert result.provenance[0].evidence_ref == "probe/td/daily_kline"
    assert "available_at_unknown" in result.missing_fields


def test_observed_provider_does_not_promote_availability_to_runtime():
    def observed_rows(previous_trade_date, symbols):
        return [{"symbol": "000001", "close": 11.59, "amount": 100}]

    result = PreviousDayStatsFunction(
        TDPreviousDayStatsProvider(
            observed_rows,
            observed_at_ms=lambda: 1788484800000,
            source_id="network_oracle",
            source_schema="historical_result",
        )
    ).execute(
        DataContext("eval-1", "AUCTION", 1788484800000, expected_previous_trade_date="2026-09-03"),
        _request(),
    )
    assert result.status is DataStatus.UNAVAILABLE


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
    left = PreviousDayStatsFunction(StaticProvider(physical)).execute(
        DataContext("eval-identity", "AUCTION", 1788484800000, expected_previous_trade_date="2026-09-03"),
        _request(),
    )
    right = PreviousDayStatsFunction(StaticProvider(other)).execute(
        DataContext("eval-identity", "AUCTION", 1788484800000, expected_previous_trade_date="2026-09-03"),
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
        }
    })
    result = PreviousDayStatsFunction(provider).execute(
        DataContext(
            "eval-real-fixture",
            "AUCTION",
            1788484800000,
            expected_previous_trade_date="2026-09-03",
        ),
        _request(),
    )
    assert result.status is DataStatus.READY
    assert result.data["close_by_symbol"]["000001"] == 11.880000114440918
    assert result.data["amount_by_symbol"]["000001"] == 1324230272.0


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
        StaticProvider(physical)
    ).execute(
        DataContext(
            "eval-rows",
            "AUCTION",
            1788484800000,
            expected_previous_trade_date="2026-09-03",
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
        StaticProvider(physical)
    ).execute(
        DataContext(
            "eval-empty-rows",
            "AUCTION",
            1788484800000,
            expected_previous_trade_date="2026-09-03",
        ),
        _request(),
    )
    assert result.status is DataStatus.MISSING
    assert result.completeness == 0.0
