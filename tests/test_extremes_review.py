import pytest

from engine_core.extremes_review import (
    derive_extremes_review,
    limit_percent,
    normalize_extreme,
    _prior_streaks,
)


def test_limit_bands_and_new_listing_exclusion():
    assert limit_percent("600001") == 10
    assert limit_percent("688001") == 20
    assert limit_percent("300001") == 20
    assert limit_percent("920001") == 30
    assert limit_percent("430001") == 30
    assert limit_percent("N00123") is None
    assert limit_percent("C00123") is None
    assert normalize_extreme(8, "688001") == pytest.approx(0.4)


def test_big_meat_and_big_loss_threshold_edges():
    assert normalize_extreme(5, "600001") == pytest.approx(0.5)
    assert normalize_extreme(-5, "600001") == pytest.approx(-0.5)


def test_path_classifications_include_near_reversals_and_streaks():
    rows = [
        {"symbol": "600001", "name": "地天", "limit_pct": 10, "high_pct": 10, "low_pct": -10,
         "close_pct": 10, "prior_limit_streak": 2, "prior_down_streak": 0},
        {"symbol": "600002", "name": "近地天", "limit_pct": 10, "high_pct": 2, "low_pct": -10,
         "close_pct": 1, "prior_limit_streak": 0, "prior_down_streak": 0},
        {"symbol": "600003", "name": "开板", "limit_pct": 10, "high_pct": -4, "low_pct": -10,
         "close_pct": -9, "prior_limit_streak": 0, "prior_down_streak": 2},
        {"symbol": "600004", "name": "天地", "limit_pct": 10, "high_pct": 10, "low_pct": -10,
         "close_pct": -10, "prior_limit_streak": 0, "prior_down_streak": 0},
    ]
    result = derive_extremes_review(rows, trade_date="2026-10-14")
    paths = result["paths"]
    assert [r["symbol"] for r in paths["地天"]] == ["600001"]
    assert [r["symbol"] for r in paths["近地天"]] == ["600002"]
    assert [r["symbol"] for r in paths["天地"]] == ["600004"]
    assert [r["symbol"] for r in paths["连续跌停后开板"]] == ["600003"]


def test_review_keeps_hand_calculable_delta_and_fraction():
    rows = [
        {"symbol": "600001", "auction_pct": 2, "close_pct": 7, "limit_pct": 10},
        {"symbol": "300001", "auction_pct": -2, "close_pct": 8, "limit_pct": 20},
        {"symbol": "920001", "auction_pct": 1, "close_pct": -5, "limit_pct": 30},
    ]
    result = derive_extremes_review(rows, trade_date="2026-10-14")
    by_symbol = {row["symbol"]: row for row in result["stocks"]}
    assert (by_symbol["600001"]["delta_pp"], by_symbol["600001"]["delta_over_limit"]) == (5, 0.5)
    assert (by_symbol["300001"]["delta_pp"], by_symbol["300001"]["delta_over_limit"]) == (10, 0.5)
    assert (by_symbol["920001"]["delta_pp"], by_symbol["920001"]["delta_over_limit"]) == (-6, -0.2)


def test_daily_history_counts_only_consecutive_limit_days():
    history = [
        {"date": "2026-10-09", "pre_close": 10, "close": 11},
        {"date": "2026-10-12", "pre_close": 11, "close": 12.1},
        {"date": "2026-10-13", "pre_close": 12.1, "close": 11},
    ]
    assert _prior_streaks(history, "600001") == (0, 0)
    assert _prior_streaks(history[:2], "600001") == (2, 0)
    assert _prior_streaks(history[:2], "920001") == (0, 0)
