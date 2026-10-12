import math
from datetime import datetime, timezone

from engine_core.previous_day_limit_current_rows import build_current_0925_rows

DATE = "2026-10-09"


def _row(symbol, **overrides):
    row = {
        "auction_tag": "0925",
        "chg_bp": 250,
        "match_amt_yuan": 1_000_000,
        "symbol": symbol,
        "trade_date": "20261009",
        "ts": "2026-10-09T09:25:06.248000",
    }
    row.update(overrides)
    return row


def test_converts_units_and_keeps_missing_as_none_not_zero():
    result = build_current_0925_rows(
        DATE,
        [
            _row("000001"),
            _row("000002", chg_bp=None, match_amt_yuan=None),
            _row("000003", chg_bp=math.nan, match_amt_yuan=-5),
        ],
    )
    by_symbol = {row["symbol"]: row for row in result.rows}
    assert by_symbol["000001"]["change_pct"] == 2.5
    assert by_symbol["000001"]["auction_amount_yuan"] == 1_000_000
    assert by_symbol["000001"]["trade_date"] == DATE and by_symbol["000001"]["tag"] == "0925"
    for symbol in ("000002", "000003"):
        assert by_symbol[symbol]["change_pct"] is None
        assert by_symbol[symbol]["auction_amount_yuan"] is None


def test_other_tag_and_other_date_are_counted_not_repaired():
    result = build_current_0925_rows(
        DATE,
        [
            _row("000001"),
            _row("000002", auction_tag="0924"),
            _row("000003", trade_date="20261008"),
            ["not", "a", "row"],
        ],
    )
    assert [row["symbol"] for row in result.rows] == ["000001"]
    assert (result.other_tag_count, result.date_mismatch_count, result.invalid_row_count) == (1, 1, 1)


def test_duplicate_symbol_keeps_latest_source_time_and_reports_it():
    result = build_current_0925_rows(
        DATE,
        [
            _row("000001", chg_bp=100, ts="2026-10-09T09:25:07"),
            _row("000001", chg_bp=300, ts="2026-10-09T09:25:09"),
            _row("000001", chg_bp=200, ts="2026-10-09T09:25:08"),
        ],
    )
    assert len(result.rows) == 1 and result.rows[0]["change_pct"] == 3.0
    assert result.duplicate_symbols == ("000001",)


def test_string_and_datetime_timestamps_agree_and_naive_means_shanghai():
    expected = int(datetime(2026, 10, 9, 1, 25, 6, tzinfo=timezone.utc).timestamp() * 1000)
    from_text = build_current_0925_rows(DATE, [_row("000001", ts="2026-10-09T09:25:06")])
    from_dt = build_current_0925_rows(DATE, [_row("000001", ts=datetime(2026, 10, 9, 9, 25, 6))])
    assert from_text.rows[0]["source_record_time_ms"] == expected
    assert from_dt.content_hash == from_text.content_hash


def test_current_rows_from_q2_truncates_and_keeps_missing_none():
    from types import SimpleNamespace
    from engine_core.previous_day_limit_current_rows import build_current_0925_rows_from_q2

    quotes = {
        "000001.SZ": SimpleNamespace(
            auction_anchor_0925_price_milli=10_999, pre_close_milli=10_000,
            auction_amount_yuan=5_000_000, source_record_time_ms=1,
        ),
        "000002.SZ": SimpleNamespace(
            auction_anchor_0925_price_milli=None, pre_close_milli=10_000,
            auction_amount_yuan=None, source_record_time_ms=None,
        ),
        "000003.SZ": SimpleNamespace(
            auction_anchor_0925_price_milli=9_001, pre_close_milli=10_000,
            auction_amount_yuan=0, source_record_time_ms=2,
        ),
    }
    result = build_current_0925_rows_from_q2("2026-10-09", quotes)
    by = {row["symbol"]: row for row in result.rows}
    assert by["000001.SZ"]["change_pct"] == 9.99
    assert by["000002.SZ"]["change_pct"] is None
    assert by["000002.SZ"]["auction_amount_yuan"] is None
    assert by["000003.SZ"]["change_pct"] == -9.99
