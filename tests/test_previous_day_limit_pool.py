from __future__ import annotations

from datetime import datetime, timezone

import pytest

from engine_core import (
    DataContext,
    DataRequest,
    DataStatus,
    PreviousDayLimitPoolFunction,
    RedisPreviousDayLimitPoolProvider,
    build_calendar_snapshot,
    canonical_previous_day_limit_pool_payload_hash,
    normalize_previous_day_limit_pool_rows,
)


CALENDAR = build_calendar_snapshot(
    ["2026-09-09", "2026-09-10", "2026-09-11"],
    version="fixture-limit-pool-v1",
    declared_valid_from="2026-09-09",
    declared_valid_to="2026-09-11",
    source_guard_valid_from="2026-09-09",
    # Guard coverage includes the weekend so the function can reject a
    # non-trading request without invoking the provider.
    source_guard_valid_to="2026-09-13",
    source_id="fixture-calendar",
    observed_at_ms=1789000000000,
)


def _rows():
    return [
        {
            "trade_date": "2026-09-10",
            "symbol": "600519",
            "name": "样本甲",
            "lb_days": 2,
            "plate": "消费",
            "seal_time": "09:31:22",
            "turnover": 61_814_324.0,
            "close_pct": 10.01,
            "source": "kaipan",
        },
        {
            "trade_date": "2026-09-10",
            "symbol": "000001",
            "name": "样本乙",
            "lb_days": 1,
            "plate": None,
            "seal_time": None,
            "turnover": 101_359_868.0,
            "close_pct": 9.9,
            "source": "kaipan",
        },
    ]


def test_normalizer_sorts_rows_and_maps_legacy_turnover_to_yuan():
    result = normalize_previous_day_limit_pool_rows(
        reversed(_rows()), actual_trade_date="2026-09-10"
    )
    assert [row["symbol"] for row in result["rows"]] == ["000001", "600519"]
    assert result["by_symbol"]["600519"]["turnover_yuan"] == 61_814_324.0
    assert result["field_units"] == {
        "lb_days": "boards",
        "turnover_yuan": "yuan",
        "close_pct": "percent",
    }
    assert result["unit_uncertainties"] == ()
    assert result["scope"] == "provider_declared_pool"


@pytest.mark.parametrize(
    "mutator, message",
    [
        (lambda rows: rows[0].update({"trade_date": "2026-09-09"}), "trade_date mismatch"),
        (lambda rows: rows[0].update({"symbol": "SH.600519"}), "invalid symbol"),
        (lambda rows: rows.append(dict(rows[0])), "duplicate limit-pool symbol"),
        (lambda rows: rows[0].update({"turnover": None}), "turnover"),
    ],
)
def test_normalizer_rejects_ambiguous_rows(mutator, message):
    rows = _rows()
    mutator(rows)
    with pytest.raises((TypeError, ValueError), match=message):
        normalize_previous_day_limit_pool_rows(rows, actual_trade_date="2026-09-10")


def test_normalizer_rejects_legacy_percent_override_for_canonical_yuan_field():
    with pytest.raises(ValueError, match="canonical field"):
        normalize_previous_day_limit_pool_rows(
            _rows(),
            actual_trade_date="2026-09-10",
            verified_field_units={"turnover_yuan": "percent"},
        )


def _request(symbols=()):
    return DataRequest(
        request_id="limit-pool-test",
        function_id="previous_day_limit_pool",
        trade_date="2026-09-11",
        effective_as_of_ms=1789080000000,
        knowledge_as_of_ms=1789080000000,
        symbols=tuple(symbols),
    )


def test_function_is_unavailable_without_historical_availability_evidence():
    provider = RedisPreviousDayLimitPoolProvider(
        lambda previous: _rows(),
        observed_at_ms=lambda: 1789080000000,
    )
    result = PreviousDayLimitPoolFunction(provider, CALENDAR).execute(
        DataContext("eval", "READ_ONLY", 1789080000000), _request()
    )
    assert result.status is DataStatus.UNAVAILABLE
    assert result.missing_fields == ("available_at_unknown",)
    assert result.actual_trade_date == "2026-09-10"


def test_function_supports_exact_symbol_scope_when_availability_is_verified():
    provider = RedisPreviousDayLimitPoolProvider(
        lambda previous: _rows(),
        observed_at_ms=lambda: 1789080000000,
        available_at_ms=lambda: 1789070000000,
        verified_field_units={"turnover_yuan": "yuan"},
    )
    result = PreviousDayLimitPoolFunction(provider, CALENDAR).execute(
        DataContext("eval", "READ_ONLY", 1789080000000), _request(("600519",))
    )
    assert result.status is DataStatus.READY
    assert result.completeness == 1.0
    assert result.missing_fields == ()
    assert result.data["row_count"] == 2
    assert result.data["scope"] == "provider_declared_pool"


def test_function_reports_partial_requested_symbol_scope_without_fabricating_rows():
    provider = RedisPreviousDayLimitPoolProvider(
        lambda previous: _rows(),
        observed_at_ms=lambda: 1789080000000,
        available_at_ms=lambda: 1789070000000,
        verified_field_units={"turnover_yuan": "yuan"},
    )
    result = PreviousDayLimitPoolFunction(provider, CALENDAR).execute(
        DataContext("eval", "READ_ONLY", 1789080000000), _request(("600519", "300750"))
    )
    assert result.status is DataStatus.PARTIAL
    assert result.missing_symbols == ("300750",)
    assert result.completeness == 0.5


def test_provider_rejects_malformed_source_metadata():
    provider = RedisPreviousDayLimitPoolProvider(
        lambda previous: _rows(),
        observed_at_ms=lambda: 1789080000000,
        metadata=lambda previous: {
            "available_at_ms": "1789070000000",
            "field_units": {"turnover_yuan": "yuan"},
        },
    )
    result = PreviousDayLimitPoolFunction(provider, CALENDAR).execute(
        DataContext("eval", "READ_ONLY", 1789080000000), _request()
    )
    assert result.status is DataStatus.ERROR
    assert result.available_at_ms is None


def test_provider_accepts_only_metadata_bound_to_the_same_payload():
    provider = RedisPreviousDayLimitPoolProvider(
        lambda previous: _rows(),
        observed_at_ms=lambda: 1789080000000,
        metadata=lambda previous: {
            "schema_version": "PreviousDayLimitPoolV1",
            "available_at_ms": 1789070000000,
            "field_units": {
                "lb_days": "boards",
                "turnover_yuan": "yuan",
                "close_pct": "percent",
            },
            "payload_sha256": canonical_previous_day_limit_pool_payload_hash(_rows()),
        },
    )
    ready = PreviousDayLimitPoolFunction(provider, CALENDAR).execute(
        DataContext("eval", "READ_ONLY", 1789080000000), _request()
    )
    assert ready.status is DataStatus.READY

    mismatched = RedisPreviousDayLimitPoolProvider(
        lambda previous: _rows(),
        observed_at_ms=lambda: 1789080000000,
        metadata=lambda previous: {
            "schema_version": "PreviousDayLimitPoolV1",
            "available_at_ms": 1789070000000,
            "field_units": {"turnover_yuan": "yuan"},
            "payload_sha256": "0" * 64,
        },
    )
    rejected = PreviousDayLimitPoolFunction(mismatched, CALENDAR).execute(
        DataContext("eval", "READ_ONLY", 1789080000000), _request()
    )
    assert rejected.status is DataStatus.ERROR


def test_non_trading_request_is_rejected_before_provider_access():
    calls = []

    def fetch_rows(previous_trade_date):
        calls.append(previous_trade_date)
        return _rows()

    provider = RedisPreviousDayLimitPoolProvider(
        fetch_rows,
        observed_at_ms=lambda: 1789080000000,
        available_at_ms=lambda: 1789070000000,
        verified_field_units={"turnover_yuan": "yuan"},
    )
    result = PreviousDayLimitPoolFunction(provider, CALENDAR).execute(
        DataContext("eval", "READ_ONLY", 1789080000000),
        _request(),
    )
    # The request date in _request is a trading day; explicitly use the
    # Saturday within the same source guard to exercise the fail-closed path.
    invalid_request = DataRequest(
        request_id="limit-pool-weekend",
        function_id="previous_day_limit_pool",
        trade_date="2026-09-12",
        effective_as_of_ms=1789080000000,
        knowledge_as_of_ms=1789080000000,
    )
    invalid = PreviousDayLimitPoolFunction(provider, CALENDAR).execute(
        DataContext("eval", "READ_ONLY", 1789080000000), invalid_request
    )

    assert result.status is DataStatus.READY
    assert invalid.status is DataStatus.INVALID
    assert invalid.missing_fields == ("trade_date",)
    assert calls == ["2026-09-10"]


def test_empty_verified_pool_is_missing_not_ready():
    provider = RedisPreviousDayLimitPoolProvider(
        lambda previous_trade_date: [],
        observed_at_ms=lambda: 1789080000000,
        available_at_ms=lambda: 1789070000000,
        verified_field_units={"turnover_yuan": "yuan"},
    )
    result = PreviousDayLimitPoolFunction(provider, CALENDAR).execute(
        DataContext("eval", "READ_ONLY", 1789080000000), _request()
    )

    assert result.status is DataStatus.MISSING
    assert result.completeness == 0.0
