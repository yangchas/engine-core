"""Differential vectors captured from the deployed ``engine_next`` reader.

The old module is not imported by the core test suite.  These vectors are
the verified oracle for the first migration slice and keep the parity claim
explicit and reviewable.
"""

from __future__ import annotations

import pytest

from engine_core import (
    build_open_fact,
    classify_delta,
    classify_sign_state,
    compute_change_delta_bp,
    compute_delta,
)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            {
                "symbol": "600519",
                "timestamp_ms": 1788996600123,
                "price_milli": 10500,
                "previous_close_milli": 10000,
                "amount_2m_yuan": 1200000,
                "limit_state": 1,
                "name": "fixture",
                "speed_1m": 2.5,
            },
            {
                "symbol": "600519",
                "timestamp_ms": 1788996600123,
                "change_pct": 5.000000000000004,
                "amount_2m_yuan": 1200000.0,
                "limit_state": 1,
                "limit_state_status": "available",
                "name": "fixture",
                "speed_1m": 2.5,
                "status": "available",
            },
        ),
        (
            {
                "symbol": "000001",
                "price_milli": 12000,
                "previous_close_milli": 10000,
                "limit_state": "unknown",
            },
            {
                "symbol": "000001",
                "timestamp_ms": None,
                "change_pct": 19.999999999999996,
                "amount_2m_yuan": None,
                "limit_state": "unknown",
                "limit_state_status": "invalid",
                "name": "",
                "speed_1m": None,
                "status": "available",
            },
        ),
        (
            {
                "symbol": "x",
                "price_milli": 0,
                "previous_close_milli": 10000,
                "limit_state": None,
            },
            {
                "symbol": "x",
                "timestamp_ms": None,
                "change_pct": None,
                "amount_2m_yuan": None,
                "limit_state": None,
                "limit_state_status": "unavailable",
                "name": "",
                "speed_1m": None,
                "status": "unavailable",
            },
        ),
        (
            {
                "symbol": "y",
                "price_milli": "bad",
                "previous_close_milli": 1,
                "limit_state": "0",
            },
            {
                "symbol": "y",
                "timestamp_ms": None,
                "change_pct": None,
                "amount_2m_yuan": None,
                "limit_state": 0,
                "limit_state_status": "available",
                "name": "",
                "speed_1m": None,
                "status": "unavailable",
            },
        ),
    ],
)
def test_open_fact_matches_deployed_legacy_vectors(row, expected):
    actual = build_open_fact(row)
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        if isinstance(value, float):
            assert actual[key] == pytest.approx(value)
        else:
            assert actual[key] == value


@pytest.mark.parametrize(
    ("after", "before", "expected_delta", "expected_state", "expected_sign"),
    [
        (5, 2, 3.0, "expanded", "contracted"),
        (2, 5, -3.0, "contracted", "expanded"),
        (0, 0, 0.0, "unchanged", "unchanged"),
        (-1, 0, -1.0, "contracted", "expanded"),
        (0, 1, -1.0, "contracted", "expanded"),
        (-1, 1, -2.0, "contracted", "reversed"),
        (None, 1, None, "unavailable", "unavailable"),
    ],
)
def test_delta_and_sign_functions_match_deployed_legacy_vectors(
    after, before, expected_delta, expected_state, expected_sign
):
    assert compute_delta(after, before) == expected_delta
    assert classify_delta(expected_delta) == expected_state
    assert classify_sign_state(after, before) == expected_sign


@pytest.mark.parametrize(
    ("opening", "auction", "expected"),
    [(5.0, 2.0, 300), (1.234, 1.0, 23), (None, 1.0, None)],
)
def test_change_delta_bp_matches_deployed_legacy_conversion(opening, auction, expected):
    assert compute_change_delta_bp(opening, auction) == expected
