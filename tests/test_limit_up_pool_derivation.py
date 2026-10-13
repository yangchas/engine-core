from __future__ import annotations

from inspect import signature

from engine_core.limit_up_pool_derivation import derive_limit_up_pool_from_daily_klines


def _row(day, *, high=11.0, close=11.0, pct=10.0, amount=123.0):
    return [day + " 15:00:00", 10.0, high, 9.0, close, 100.0, amount, 1.0, pct]


def test_main_board_counts_consecutive_limit_ups_and_uses_legacy_turnover_field():
    result = derive_limit_up_pool_from_daily_klines(
        {"600001": [_row("2026-10-09"), _row("2026-10-12", pct=9.5)]},
        "2026-10-12",
        names_by_symbol={"600001": "样本"},
    )
    assert result.rows == ({
        "trade_date": "2026-10-12", "symbol": "600001", "name": "样本",
        "lb_days": 2, "plate": None, "seal_time": None, "turnover": 123.0,
        "close_pct": 9.5, "source": "core_td_kline",
    },)
    assert result.content_hash


def test_growth_board_limit_and_st_name_uses_normal_board_limit():
    result = derive_limit_up_pool_from_daily_klines({
        "300001": [_row("2026-10-12", high=12, close=12, pct=19.5)],
        "600002": [_row("2026-10-12", high=11, close=11, pct=10.0)],
    }, "2026-10-12", names_by_symbol={"600002": "ST样本"})
    assert [r["symbol"] for r in result.rows] == ["300001", "600002"]


def test_five_percent_st_name_is_not_a_limit_up():
    result = derive_limit_up_pool_from_daily_klines(
        {"600002": [_row("2026-10-12", high=11, close=11, pct=5.0)]},
        "2026-10-12",
        names_by_symbol={"600002": "ST样本"},
    )
    assert result.rows == ()


def test_derivation_api_has_no_st_symbols_override():
    assert "st_symbols" not in signature(derive_limit_up_pool_from_daily_klines).parameters


def test_30_percent_board_is_supported():
    result = derive_limit_up_pool_from_daily_klines(
        {"830001": [_row("2026-10-12", high=13, close=13, pct=29.5)]}, "2026-10-12"
    )
    assert len(result.rows) == 1


def test_missing_prior_day_breaks_streak_and_none_is_unknown():
    result = derive_limit_up_pool_from_daily_klines({
        "600001": [_row("2026-10-08"), _row("2026-10-12")],
        "600002": [["2026-10-12 15:00:00", 10, None, 9, 10, 1, 2, 3, None]],
        "600003": [_row("2026-10-09")],
    }, "2026-10-12")
    assert result.rows[0]["lb_days"] == 1
    assert result.excluded_counts["unknown_pct_or_price"] == 1
    assert result.excluded_counts["no_row_for_trade_date"] == 1
    assert result.notes["name_fallback_symbols"] == 1
