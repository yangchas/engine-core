from __future__ import annotations

import pytest

from engine_core import infer_legacy_theme_delta_signal


def _signal(**overrides: float) -> str:
    values = {
        "amount_delta_24_25": 1.0,
        "bid_amount_delta_24_25": 0.0,
        "change_pct_delta_avg": 0.0,
        "amount_ratio_avg": 0.0,
    }
    values.update(overrides)
    return infer_legacy_theme_delta_signal(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"amount_delta_24_25": 50_000_000, "change_pct_delta_avg": 1.0}, "增量转强"),
        ({"amount_delta_24_25": 50_000_000, "change_pct_delta_avg": -2.0}, "放量回落"),
        ({"amount_delta_24_25": 0.0, "bid_amount_delta_24_25": 10_000_000}, "封单增强"),
        ({"amount_delta_24_25": -20_000_000, "bid_amount_delta_24_25": 20_000_000}, "竞价降温"),
        ({"amount_delta_24_25": 1.0, "amount_ratio_avg": 1.5}, "温和放量"),
        ({"amount_delta_24_25": 0.0, "amount_ratio_avg": 1.5}, "平稳"),
    ],
)
def test_legacy_signal_preserves_exact_thresholds_and_precedence(overrides, expected):
    assert _signal(**overrides) == expected


@pytest.mark.parametrize("field", [
    "amount_delta_24_25",
    "bid_amount_delta_24_25",
    "change_pct_delta_avg",
    "amount_ratio_avg",
])
def test_legacy_signal_rejects_missing_or_non_finite_values(field):
    values = {
        "amount_delta_24_25": 1.0,
        "bid_amount_delta_24_25": 0.0,
        "change_pct_delta_avg": 0.0,
        "amount_ratio_avg": 0.0,
    }
    values[field] = None
    with pytest.raises(ValueError, match=field):
        infer_legacy_theme_delta_signal(**values)

    values[field] = float("nan")
    with pytest.raises(ValueError, match=field):
        infer_legacy_theme_delta_signal(**values)
