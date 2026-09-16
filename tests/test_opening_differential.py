from __future__ import annotations

import math
from types import SimpleNamespace

from engine_core import (
    build_open_fact,
    classify_delta,
    classify_sign_state,
    compute_change_delta_bp,
    compute_delta,
)
from examples.run_opening_differential import (
    _auction_change_from_rows,
    _auction_change_evidence_from_rows,
    _aggregate_exact,
    _comparison_status,
    compare_opening_rows,
)


def _legacy_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


LEGACY = SimpleNamespace(
    _open_fact=build_open_fact,
    _number=_legacy_number,
    _delta=compute_delta,
    _state=classify_delta,
    _sign_state=classify_sign_state,
    _change_bp=lambda value: compute_change_delta_bp(value, 0.0) if value is not None else None,
)


def _row():
    return {
        "symbol": "600519",
        "timestamp_ms": 1788996600123,
        "price_milli": 10500,
        "previous_close_milli": 10000,
        "amount_2m_yuan": 1200000,
        "limit_state": 1,
        "name": "fixture",
        "speed_1m": 2.5,
    }


def test_opening_differential_composes_exact_open_and_transition_comparisons():
    result = compare_opening_rows(LEGACY, (_row(),), auction_change_pct=2.0)
    assert result["opening_exact"] is True
    assert result["transition_exact"] is True
    assert result["comparisons"][0]["opening_exact"] is True
    assert result["comparisons"][0]["transition_exact"] is True


def test_opening_differential_can_compare_open_only_without_auction_input():
    result = compare_opening_rows(LEGACY, (_row(),))
    assert result["opening_exact"] is True
    assert result["transition_exact"] is None


def test_auction_change_uses_only_the_0925_row_and_keeps_missing_unknown():
    rows = [
        {"auction_tag": "0924", "chg_bp": 1},
        {"auction_tag": "0925", "chg_bp": 250},
    ]
    assert _auction_change_from_rows(rows) == 2.5
    assert _auction_change_from_rows([{ "auction_tag": "0925", "chg_bp": None }]) is None


def test_typed_td_chg_bp_matches_production_percentage_point_contract():
    assert _auction_change_from_rows([{ "auction_tag": "0925", "chg_bp": -8 }]) == -0.08
    assert _auction_change_evidence_from_rows([{ "auction_tag": "0925", "chg_bp": None }]) == (None, "0925_chg_bp_missing_or_invalid")
    assert _auction_change_evidence_from_rows([]) == (None, "0925_row_missing")


def test_differential_status_does_not_call_missing_transition_a_match():
    assert _comparison_status({"opening_exact": True, "transition_exact": None}, transition_requested=True) == "NON_COMPARABLE"
    assert _comparison_status({"opening_exact": True, "transition_exact": True}, transition_requested=True) == "MATCH"
    assert _comparison_status({"opening_exact": False, "transition_exact": True}, transition_requested=True) == "MISMATCH"


def test_differential_aggregate_keeps_non_comparable_distinct_from_mismatch():
    assert _aggregate_exact(
        ({"transition_exact": True}, {"transition_exact": True}),
        key="transition_exact",
    ) is True
    assert _aggregate_exact(
        ({"transition_exact": True}, {"transition_exact": None}),
        key="transition_exact",
    ) is None
    assert _aggregate_exact(
        ({"transition_exact": False}, {"transition_exact": None}),
        key="transition_exact",
    ) is False
