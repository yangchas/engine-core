import pytest

from engine_core.behavior_tags import calculate_stock_tags, summarize


def _market():
    return {"0924": {"m": 1000}, "0925": {"m": 2000}}


def test_direction_uses_price_and_market_amount_share_signs_with_flat_band():
    market = _market()
    cases = [
        (1.0, 24.0, "价升份额升(抢筹候选)"),
        (-1.0, 24.0, "价降份额升(放量走弱)"),
        (1.0, 16.0, "价升份额降"),
        (-1.0, 16.0, "价降份额降"),
        (0.0, 10.0, "平"),
    ]
    for dpct, m25, expected in cases:
        row = {"0924": {"pct": 0.0, "m": 10.0},
               "0925": {"pct": dpct, "m": m25}}
        assert calculate_stock_tags(row, market)["direction"] == expected


def test_seal_ratios_use_unmatched_side_and_zero_denominators_are_missing():
    tags = calculate_stock_tags(
        {"0924": {"m": 100, "limit_state": 0},
         "0925": {"m": 0, "rb": 30, "ra": 0, "limit_state": 1},
         "prev_day_amount": 0},
        {"0924": {"m": 1000}, "0925": {"m": 2000}},
    )
    assert tags["seal_amount"] == 30
    assert tags["seal_over_match"] is None
    assert tags["seal_over_prev_day_amount"] is None
    assert tags["pressure_0925"] == 1


def test_sealed_state_continuity_and_last_minute_seal():
    base = {"0920": {"m": 1}, "0924": {"limit_state": 0},
            "0925": {"limit_state": 1, "rb": 5, "m": 2}}
    tags = calculate_stock_tags(base, _market())
    assert tags["was_sealed_0924"] is False
    assert tags["last_minute_seal"] is True
    assert calculate_stock_tags({**base, "0924": {"limit_state": 1}}, _market())["was_sealed_0924"] is True


def test_relative_to_strongest_plate_and_outrun_threshold():
    tags = calculate_stock_tags(
        {"0925": {"pct": 5.0}, "plates_0925": {"p1": 2.0, "p2": 3.0}}, _market())
    assert tags["rel_to_strongest_plate_pp"] == pytest.approx(2.0)
    assert tags["outrun"] is True


def test_stage_status_is_preserved_and_summary_counts_directions_and_seals():
    stocks = {
        "A": {"0924": {"pct": 1.0, "m": 10, "status": "INDICATIVE", "limit_state": 0},
              "0925": {"pct": -1.0, "m": 30, "status": "FINAL", "limit_state": -1, "ra": 4}},
    }
    row = calculate_stock_tags(stocks["A"], _market())
    assert row["status_0924"] == "INDICATIVE"
    assert row["status_0925"] == "FINAL"
    assert row["pct_0925"] == -1.0
    report = summarize(_market(), stocks)
    assert report["direction_counts"]["价降份额升(放量走弱)"] == 1
    assert report["seal_quality"]["count"] == 1
    assert report["seal_quality"]["seal_amount_median"] == 4
