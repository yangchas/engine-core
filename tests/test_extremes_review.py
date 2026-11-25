import pytest

from engine_core.extremes_review import (
    classify_regime,
    build_daily_history,
    derive_extremes_review,
    limit_percent,
    normalize_extreme,
    render_extremes_markdown,
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


def test_regime_boundaries_and_priority():
    assert classify_regime(left_tail=300, right_tail=999, market_median=2, up_rate=.8) == "PANIC"
    assert classify_regime(left_tail=299, right_tail=200, market_median=.01, up_rate=.5) == "RECOVERY_STRONG"
    assert classify_regime(left_tail=0, right_tail=199, market_median=-1.2, up_rate=.5) == "WEAK"
    assert classify_regime(left_tail=0, right_tail=0, market_median=0, up_rate=.25) == "WEAK"
    assert classify_regime(left_tail=0, right_tail=0, market_median=0, up_rate=.26) == "NORMAL"
    assert classify_regime(left_tail=None, right_tail=None, market_median=-2, up_rate=.1) == "UNKNOWN_INSUFFICIENT_DELTA"


def test_move_0924_0925_bins_are_populated():
    rows = [
        {"symbol": "600001", "auction_pct": 0, "close_pct": 1, "move_0924_0925_pp": -2},
        {"symbol": "600002", "auction_pct": 0, "close_pct": 1, "move_0924_0925_pp": 0},
        {"symbol": "600003", "auction_pct": 0, "close_pct": 1, "move_0924_0925_pp": 2},
    ]
    bins = derive_extremes_review(rows, trade_date="2026-10-14")["bins"]["move_0924_0925_pp"]
    assert {k: bins[k]["n"] for k in ("<=-2", "-2~+2", ">=+2")} == {"<=-2": 1, "-2~+2": 1, ">=+2": 1}


def test_daily_csv_summaries_use_union_and_explain_missing_and_nc(tmp_path):
    path = tmp_path / "daily.csv"
    path.write_text(
        "date,symbol,name,open,high,low,close,volume,amount_yuan,turnover_pct,pct_chg\n"
        "2026-10-13,600001,甲,100,110,90,100,,,,0\n"
        "2026-10-13,600002,乙,100,101,99,99,,,, -1\n"
        "2026-10-14,600001,甲,100,100,90,90,,,, -10\n"
        "2026-10-14,600002,乙,99,109,99,108,,,,9.09\n"
        "2026-10-14,N00001,新股,10,20,10,20,,,,100\n"
        "2026-10-14,600003,缺价,,,,,,,,\n", encoding="utf-8")
    result = build_daily_history(path)
    day = next(d for d in result["days"] if d["trade_date"] == "2026-10-14")
    assert day["summary"]["n"] == 2
    assert day["summary"]["excluded_new_listing_count"] == 1
    assert day["summary"]["missing_price_count"] == 1
    assert day["regime"] == "NORMAL"


def test_regime_tables_mark_insufficient_and_include_hand_calculable_group(tmp_path):
    # Three NORMAL dates provide the minimum stable regime sample; a fourth
    # date is still emitted and marks a one-day regime as insufficient.
    path = tmp_path / "daily.csv"
    rows = ["date,symbol,open,high,low,close"]
    for day, close in (("2026-10-01", 101), ("2026-10-02", 102), ("2026-10-03", 103), ("2026-10-04", 104)):
        rows.append(f"{day},600001,100,105,99,{close}")
        rows.append(f"{day},600002,100,101,95,99")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    result = build_daily_history(path)
    assert result["regime_tables"]
    # The first date has no prior close, so its Δ regime is intentionally unknown.
    assert result["regime_tables"]["NORMAL"]["sample_days"] == 3
    assert "open_le_minus_3" in result["regime_tables"]["NORMAL"]["open_buckets"]


def test_panic_recovery_sequences_and_report_sign_format(tmp_path):
    path = tmp_path / "panic.csv"
    rows = ["date,symbol,open,high,low,close"]
    for i in range(300):
        code = f"{600000+i:06d}"
        rows.extend((f"2026-10-12,{code},100,100,100,100",
                     f"2026-10-13,{code},100,100,90,90",
                     f"2026-10-14,{code},90,100,81,100"))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    history = build_daily_history(path)
    assert [x["offset"] for x in history["panic_sequences"][0]["sessions"]] == [0, 1, 2, 3]
    assert history["panic_sequences"][0]["sessions"][1]["summary"]["panic_down_limit_again_count"] == 0
    assert history["days"][-1]["paths"]["地天"] == 300
    sample = derive_extremes_review([{"symbol": "600001", "auction_pct": 1, "close_pct": 1}], trade_date="2026-10-14")
    md = render_extremes_markdown(sample, {"sample_days": 1, "rows": [], "today_left_tail_historical_percentile": None})
    assert "上涨占比 +" not in md
    assert "limit_state | 昨板连板" in md
