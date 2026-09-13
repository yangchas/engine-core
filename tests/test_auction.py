from __future__ import annotations

import math

import pytest

from engine_core import normalize_auction_change_ratio


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

