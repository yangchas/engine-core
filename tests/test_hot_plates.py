from __future__ import annotations

import pytest

from engine_core import (
    DataContext,
    DataRequest,
    DataStatus,
    HotPlatesFunction,
    RedisHotPlatesProvider,
    build_calendar_snapshot,
    normalize_hot_plates_rows,
)


CALENDAR = build_calendar_snapshot(
    ["2026-09-09", "2026-09-10", "2026-09-11"],
    version="fixture-hot-plates-v1",
    declared_valid_from="2026-09-09",
    declared_valid_to="2026-09-11",
    source_guard_valid_from="2026-09-09",
    # Guard coverage includes the weekend so the function can distinguish a
    # non-trading request from an out-of-coverage request before touching the
    # provider.  Declared decision coverage remains the three trading dates.
    source_guard_valid_to="2026-09-13",
    source_id="fixture-calendar",
    observed_at_ms=1789000000000,
)


def _rows():
    return [
        {
            "trade_date": "2026-09-10",
            "plate_name": "消费电子",
            "rank": 2,
            "strength": 120.0,
            "hot": 120.0,
            "change_pct": 1.2,
            "net_inflow_yi": 3.5,
            "source": "kaipan",
        },
        {
            "trade_date": "2026-09-10",
            "plate_name": "机器人",
            "rank": 1,
            "strength": 200.0,
            "hot": 200.0,
            "change_pct": -0.4,
            "net_inflow_yi": -1.25,
            "source": "kaipan",
        },
    ]


def _request(*, trade_date="2026-09-10", symbols=(), knowledge_as_of_ms=1789080000000):
    return DataRequest(
        request_id="hot-plates-test",
        function_id="hot_plates",
        trade_date=trade_date,
        effective_as_of_ms=knowledge_as_of_ms,
        knowledge_as_of_ms=knowledge_as_of_ms,
        symbols=tuple(symbols),
    )


def _context():
    return DataContext("eval", "READ_ONLY", 1789080000000)


def _verified_metadata(available_at_ms=1789070000000):
    return {
        "schema_version": "HotPlatesV1",
        "available_at_ms": available_at_ms,
        "field_units": {
            "rank": "ordinal",
            "strength": "score",
            "hot": "score",
            "change_pct": "percent",
            "net_inflow_yi": "yi",
        },
    }


def test_normalizer_sorts_and_keeps_provider_scope_without_inference():
    result = normalize_hot_plates_rows(
        reversed(_rows()), actual_trade_date="2026-09-10"
    )

    assert [row["plate_name"] for row in result["rows"]] == ["机器人", "消费电子"]
    assert result["by_plate"]["机器人"]["rank"] == 1
    assert result["scope"] == "provider_declared_top_plates"
    assert result["field_units"] == {
        "rank": "ordinal",
        "strength": "UNKNOWN",
        "hot": "UNKNOWN",
        "change_pct": "percent",
        "net_inflow_yi": "UNKNOWN",
    }
    assert result["unit_uncertainties"] == ("hot", "net_inflow_yi", "strength")


@pytest.mark.parametrize(
    "mutator, message",
    [
        (lambda rows: rows[0].update({"trade_date": "2026-09-09"}), "trade_date mismatch"),
        (lambda rows: rows[0].update({"plate_name": ""}), "plate_name"),
        (lambda rows: rows.append(dict(rows[0])), "duplicate hot-plate name"),
        (lambda rows: rows[0].update({"rank": True}), "rank"),
        (lambda rows: rows[0].update({"strength": float("nan")}), "strength"),
    ],
)
def test_normalizer_rejects_ambiguous_rows(mutator, message):
    rows = _rows()
    mutator(rows)
    with pytest.raises((TypeError, ValueError), match=message):
        normalize_hot_plates_rows(rows, actual_trade_date="2026-09-10")


def test_function_is_unavailable_without_historical_availability_evidence():
    provider = RedisHotPlatesProvider(
        lambda trade_date: _rows(),
        observed_at_ms=lambda: 1789080000000,
    )
    result = HotPlatesFunction(provider, CALENDAR).execute(_context(), _request())

    assert result.status is DataStatus.UNAVAILABLE
    assert result.missing_fields == ("available_at_unknown",)
    assert result.actual_trade_date == "2026-09-10"


def test_function_accepts_only_explicit_metadata_contract():
    provider = RedisHotPlatesProvider(
        lambda trade_date: _rows(),
        observed_at_ms=lambda: 1789080000000,
        metadata=lambda trade_date: _verified_metadata(),
    )
    result = HotPlatesFunction(provider, CALENDAR).execute(_context(), _request())

    assert result.status is DataStatus.READY
    assert result.available_at_ms == 1789070000000
    assert result.data["row_count"] == 2
    assert result.data["field_units"]["net_inflow_yi"] == "yi"


def test_function_rejects_availability_after_knowledge_cutoff():
    provider = RedisHotPlatesProvider(
        lambda trade_date: _rows(),
        observed_at_ms=lambda: 1789090000000,
        metadata=lambda trade_date: _verified_metadata(1789090000001),
    )
    result = HotPlatesFunction(provider, CALENDAR).execute(
        _context(), _request(knowledge_as_of_ms=1789080000000)
    )

    assert result.status is DataStatus.UNAVAILABLE
    assert "available_at_after_knowledge_cutoff" in result.missing_fields


def test_provider_malformed_metadata_is_error_not_unknown_ready():
    provider = RedisHotPlatesProvider(
        lambda trade_date: _rows(),
        observed_at_ms=lambda: 1789080000000,
        metadata=lambda trade_date: {
            "schema_version": "legacy",
            "available_at_ms": 1789070000000,
        },
    )
    result = HotPlatesFunction(provider, CALENDAR).execute(_context(), _request())

    assert result.status is DataStatus.ERROR
    assert result.available_at_ms is None


def test_optional_numeric_values_stay_null_and_are_not_fabricated():
    rows = _rows()
    rows[0]["strength"] = None
    rows[0]["hot"] = None
    provider = RedisHotPlatesProvider(
        lambda trade_date: rows,
        observed_at_ms=lambda: 1789080000000,
        metadata=lambda trade_date: _verified_metadata(),
    )
    result = HotPlatesFunction(provider, CALENDAR).execute(_context(), _request())

    assert result.status is DataStatus.READY
    assert result.data["by_plate"]["消费电子"]["strength"] is None
    assert result.data["by_plate"]["消费电子"]["hot"] is None


def test_symbol_scoped_request_is_invalid_for_plate_snapshot():
    provider = RedisHotPlatesProvider(
        lambda trade_date: _rows(),
        observed_at_ms=lambda: 1789080000000,
    )
    result = HotPlatesFunction(provider, CALENDAR).execute(
        _context(), _request(symbols=("600519",))
    )

    assert result.status is DataStatus.INVALID
    assert result.missing_fields == ("symbol_scope_not_supported",)


def test_unknown_required_field_is_invalid():
    provider = RedisHotPlatesProvider(
        lambda trade_date: _rows(),
        observed_at_ms=lambda: 1789080000000,
    )
    request = _request()
    request = DataRequest(
        request_id=request.request_id,
        function_id=request.function_id,
        trade_date=request.trade_date,
        effective_as_of_ms=request.effective_as_of_ms,
        knowledge_as_of_ms=request.knowledge_as_of_ms,
        required_fields=("not_a_contract_field",),
    )
    result = HotPlatesFunction(provider, CALENDAR).execute(_context(), request)

    assert result.status is DataStatus.INVALID
    assert result.missing_fields == ("unknown_required_field:not_a_contract_field",)


def test_non_trading_request_is_rejected_before_provider_access():
    calls = []

    def fetch_rows(trade_date):
        calls.append(trade_date)
        return _rows()

    provider = RedisHotPlatesProvider(
        fetch_rows,
        observed_at_ms=lambda: 1789080000000,
        metadata=lambda trade_date: _verified_metadata(),
    )
    result = HotPlatesFunction(provider, CALENDAR).execute(
        _context(), _request(trade_date="2026-09-12")
    )

    assert result.status is DataStatus.INVALID
    assert result.missing_fields == ("trade_date",)
    assert calls == []


def test_empty_verified_snapshot_is_missing_not_ready():
    provider = RedisHotPlatesProvider(
        lambda trade_date: [],
        observed_at_ms=lambda: 1789080000000,
        metadata=lambda trade_date: _verified_metadata(),
    )
    result = HotPlatesFunction(provider, CALENDAR).execute(_context(), _request())

    assert result.status is DataStatus.MISSING
    assert result.completeness == 0.0
