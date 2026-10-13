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
        "seal_basis": "KLINE_CLOSE_AT_LIMIT_NO_SEAL_EVIDENCE",
        "lb_days_seal_basis": "KLINE_ONLY",
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


def test_streak_skips_weekends_and_holiday_dates_without_any_rows():
    result = derive_limit_up_pool_from_daily_klines({
        "600001": [_row("2026-09-30"), _row("2026-10-08")],
        "600002": [_row("2026-09-30"), _row("2026-10-08")],
    }, "2026-10-08")
    assert result.rows[0]["lb_days"] == 2


def test_missing_symbol_row_on_an_inferred_trading_date_breaks_streak():
    result = derive_limit_up_pool_from_daily_klines({
        "600001": [_row("2026-09-29"), _row("2026-10-08")],
        "600002": [_row("2026-09-30"), _row("2026-10-08")],
    }, "2026-10-08")
    assert result.rows[0]["lb_days"] == 1


def test_explicit_trading_dates_override_cross_section_inference():
    result = derive_limit_up_pool_from_daily_klines({
        "600001": [_row("2026-09-30"), _row("2026-10-08")],
        "600002": [_row("2026-09-30"), _row("2026-10-08")],
    }, "2026-10-08", trading_dates=["2026-09-30", "2026-10-01", "2026-10-08"])
    assert result.rows[0]["lb_days"] == 1

    outside_calendar = derive_limit_up_pool_from_daily_klines({
        "600001": [_row("2026-09-30"), _row("2026-10-08")],
    }, "2026-10-08", trading_dates=["2026-09-30"])
    assert outside_calendar.rows[0]["lb_days"] == 1
    assert outside_calendar.notes["trade_date_not_in_trading_dates"] is True


def test_inferred_trading_date_threshold_is_at_least_half_the_symbols():
    below_half = {f"600{i:03d}": [_row("2026-10-08")] for i in range(100)}
    for i in range(49):
        below_half[f"600{i:03d}"].append(_row("2026-09-30"))
    below_half_result = derive_limit_up_pool_from_daily_klines(below_half, "2026-10-08")
    assert below_half_result.rows[0]["lb_days"] == 1

    at_half = {f"600{i:03d}": [_row("2026-10-08")] for i in range(100)}
    for i in range(50):
        at_half[f"600{i:03d}"].append(_row("2026-09-30"))
    at_half_result = derive_limit_up_pool_from_daily_klines(at_half, "2026-10-08")
    assert at_half_result.rows[0]["lb_days"] == 2


def test_q2_limit_state_proves_seal_and_marks_history_as_kline_only():
    result = derive_limit_up_pool_from_daily_klines(
        {"600001": [_row("2026-10-09"), _row("2026-10-12")]},
        "2026-10-12",
        close_limit_state_by_symbol={"600001": 1},
    )
    assert result.rows[0]["seal_basis"] == "Q2_LS_AT_CLOSE"
    assert result.rows[0]["lb_days_seal_basis"] == "TODAY_Q2_LS_HISTORY_KLINE"
    assert result.seal_evidence_mode == "Q2_LS"


def test_unsealed_or_missing_q2_limit_state_is_not_treated_as_seal():
    klines = {
        "600001": [_row("2026-10-12")],
        "600002": [_row("2026-10-12")],
    }
    unsealed = derive_limit_up_pool_from_daily_klines(
        klines, "2026-10-12", close_limit_state_by_symbol={"600001": 0}
    )
    assert [row["symbol"] for row in unsealed.rows] == ["600002"]
    assert unsealed.excluded_counts["limit_price_without_seal"] == 1
    assert unsealed.notes["limit_price_without_seal_symbols"] == ["600001"]

    missing_symbol = derive_limit_up_pool_from_daily_klines(
        klines, "2026-10-12", close_limit_state_by_symbol={"600001": 1}
    )
    assert [row["symbol"] for row in missing_symbol.rows] == ["600001", "600002"]
    assert missing_symbol.rows[1]["seal_basis"] == "KLINE_CLOSE_AT_LIMIT_NO_SEAL_EVIDENCE"
    assert missing_symbol.rows[1]["lb_days_seal_basis"] == "KLINE_ONLY"
    assert missing_symbol.seal_evidence_mode == "Q2_LS"


def test_no_seal_mapping_marks_all_rows_as_unverified():
    result = derive_limit_up_pool_from_daily_klines(
        {"600001": [_row("2026-10-12")]}, "2026-10-12"
    )
    assert result.rows[0]["seal_basis"] == "KLINE_CLOSE_AT_LIMIT_NO_SEAL_EVIDENCE"
    assert result.rows[0]["lb_days_seal_basis"] == "KLINE_ONLY"
    assert result.seal_evidence_mode == "NONE"
