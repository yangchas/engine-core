from __future__ import annotations

import pytest

from engine_core import (
    OPENING_FACT_CONTRACT_VERSION,
    build_open_fact,
    classify_delta,
    classify_sign_state,
    compute_change_delta_bp,
    compute_delta,
    compute_open_change_pct,
)


def test_open_change_pct_uses_percentage_points_and_positive_prices():
    assert compute_open_change_pct(10500, 10000) == pytest.approx(5.0)
    assert compute_open_change_pct(10000, 0) is None
    assert compute_open_change_pct(-1, 10000) is None
    assert compute_open_change_pct("bad", 10000) is None


@pytest.mark.parametrize(
    ("after", "before", "expected"),
    [(5, 2, 3.0), (2, 5, -3.0), (0, 0, 0.0), (None, 1, None)],
)
def test_compute_delta_matches_legacy_order(after, before, expected):
    assert compute_delta(after, before) == expected


def test_delta_and_sign_state_zero_is_not_reversal():
    assert classify_delta(0) == "unchanged"
    assert classify_sign_state(-1, 0) == "expanded"
    assert classify_sign_state(0, 1) == "expanded"
    assert classify_sign_state(-1, 1) == "reversed"
    assert classify_sign_state(None, 1) == "unavailable"


@pytest.mark.parametrize(
    ("opening", "auction", "expected"),
    [(5.0, 2.0, 300), (1.234, 1.0, 23), (None, 1.0, None)],
)
def test_change_delta_bp_matches_legacy_percent_to_bp_conversion(
    opening, auction, expected
):
    assert compute_change_delta_bp(opening, auction) == expected


def test_build_open_fact_keeps_independent_statuses_and_units():
    result = build_open_fact(
        {
            "symbol": "600519",
            "timestamp_ms": 1788996600123,
            "price_milli": 10500,
            "previous_close_milli": 10000,
            "amount_2m_yuan": 1200000,
            "limit_state": 1,
            "name": "fixture",
            "speed_1m": 2.5,
        }
    )
    assert result == {
        "symbol": "600519",
        "timestamp_ms": 1788996600123,
        "change_pct": pytest.approx(5.0),
        "amount_2m_yuan": 1200000.0,
        "limit_state": 1,
        "limit_state_status": "available",
        "name": "fixture",
        "speed_1m": 2.5,
        "status": "available",
    }
    assert OPENING_FACT_CONTRACT_VERSION == "OpeningFactV1"


def test_build_open_fact_does_not_infer_limit_state_from_change():
    result = build_open_fact(
        {
            "symbol": "000001",
            "price_milli": 12000,
            "previous_close_milli": 10000,
            "limit_state": "unknown",
        }
    )
    assert result["change_pct"] == pytest.approx(20.0)
    assert result["status"] == "available"
    assert result["limit_state"] == "unknown"
    assert result["limit_state_status"] == "invalid"
