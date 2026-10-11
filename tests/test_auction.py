from __future__ import annotations

import math

import pytest

from engine_core import (
    classify_td_auction_inactivity,
    normalize_auction_change_bp_to_pct,
    normalize_auction_change_ratio,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.0997, 0.0997),
        (-0.0997, -0.0997),
        (9.97, 0.0997),
        (-9.97, -0.0997),
        (997, 0.0997),
        (-997, -0.0997),
        (0, 0.0),
    ],
)
def test_normalize_auction_change_ratio_preserves_legacy_units(raw, expected):
    assert normalize_auction_change_ratio(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "bad", True, False, math.nan, math.inf, -math.inf])
def test_normalize_auction_change_ratio_keeps_unknown_values_unavailable(raw):
    assert normalize_auction_change_ratio(raw) is None


def test_normalize_auction_change_ratio_does_not_create_zero_from_missing():
    assert normalize_auction_change_ratio(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(-8, -0.08), (0, 0.0), (100, 1.0), (250, 2.5)],
)
def test_normalize_typed_td_change_bp_to_percentage_points(raw, expected):
    assert normalize_auction_change_bp_to_pct(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "bad", True, False, math.nan, math.inf, -math.inf])
def test_normalize_typed_td_change_bp_keeps_unknown_as_missing(raw):
    assert normalize_auction_change_bp_to_pct(raw) is None


def test_td_auction_inactivity_requires_all_four_explicit_zero_fields():
    row = {
        "match_amt_yuan": 0,
        "chg_bp": 0,
        "rest_bid_amt_yuan": 0,
        "rest_ask_amt_yuan": 0,
    }
    assert classify_td_auction_inactivity(row) == "INACTIVE"
    assert classify_td_auction_inactivity({**row, "rest_bid_amt_yuan": 1}) == "NON_INACTIVE"


@pytest.mark.parametrize(
    "field_update",
    [
        {"match_amt_yuan": None},
        {"chg_bp": "invalid"},
        {"rest_bid_amt_yuan": math.nan},
        {"rest_ask_amt_yuan": math.inf},
    ],
)
def test_td_auction_inactivity_keeps_missing_or_invalid_evidence_unknown(field_update):
    row = {
        "match_amt_yuan": 0,
        "chg_bp": 0,
        "rest_bid_amt_yuan": 0,
        "rest_ask_amt_yuan": 0,
    }
    assert classify_td_auction_inactivity({**row, **field_update}) == "UNKNOWN"
