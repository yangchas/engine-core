from __future__ import annotations

import pytest

from engine_core import (
    build_legacy_theme_delta_shadow_trace,
    infer_legacy_theme_delta_signal,
)


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


def test_legacy_theme_delta_shadow_trace_recomputes_signal_and_hashes_lineage():
    facts = [
        {
            "theme_id": "theme-a",
            "symbol_count": 2,
            "amount_0925": 100.0,
            "amount_delta_24_25": 60_000_000.0,
            "amount_ratio_avg": 2.0,
            "bid_amount_delta_24_25": 1.0,
            "change_pct_delta_avg": 1.2,
            "positive_delta_count": 2,
            "content_hash": "fact-hash",
            "evidence_hash": "fact-evidence-hash",
            "evidence_refs": ("redis://theme/a",),
            "signal": "增量转强",
        }
    ]
    trace = build_legacy_theme_delta_shadow_trace(facts)
    assert trace["status"] == "OBSERVED"
    assert trace["decision_status"] == "FACT_ONLY"
    assert trace["signal_counts"] == {"增量转强": 1}
    assert trace["evidence_refs"] == ("redis://theme/a",)
    assert trace["content_hash"]
    assert trace["evidence_hash"]


def test_legacy_theme_delta_shadow_trace_rejects_supplied_signal_mismatch():
    fact = {
        "theme_id": "theme-a",
        "symbol_count": 1,
        "amount_0925": 1.0,
        "amount_delta_24_25": 1.0,
        "amount_ratio_avg": 2.0,
        "bid_amount_delta_24_25": 0.0,
        "change_pct_delta_avg": 0.0,
        "positive_delta_count": 1,
        "signal": "增量转强",
    }
    with pytest.raises(ValueError, match="signal does not match"):
        build_legacy_theme_delta_shadow_trace((fact,))


def test_legacy_theme_delta_shadow_trace_is_order_invariant():
    base = {
        "symbol_count": 1,
        "amount_0925": 1.0,
        "amount_delta_24_25": 1.0,
        "amount_ratio_avg": 2.0,
        "bid_amount_delta_24_25": 0.0,
        "change_pct_delta_avg": 0.0,
        "positive_delta_count": 1,
        "evidence_refs": ("fixture://theme",),
    }
    left = build_legacy_theme_delta_shadow_trace(
        (dict(base, theme_id="b"), dict(base, theme_id="a"))
    )
    right = build_legacy_theme_delta_shadow_trace(
        (dict(base, theme_id="a"), dict(base, theme_id="b"))
    )
    assert left["content_hash"] == right["content_hash"]
    assert left["evidence_hash"] == right["evidence_hash"]
