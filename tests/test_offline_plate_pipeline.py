import json

import pytest

from examples.run_offline_plate_pipeline import build_day, ensure_empty_output_dir


def _kline(symbol, day, close, pct):
    return [day, 1, close, close, close, 10, 100, 1, pct]


def test_day_pipeline_tier_priority_and_auction_aggregation():
    # 2026-01-02 is a 10% limit-up close, so it is the prior-day tier-1 pool.
    klines = {"a": [_kline("a", "2026-01-01", 10, 0), _kline("a", "2026-01-02", 11, 10),
                     _kline("a", "2026-01-05", 11, 0)],
              "b": [_kline("b", "2026-01-01", 10, 0), _kline("b", "2026-01-02", 10, 0),
                     _kline("b", "2026-01-05", 10, 0)]}
    snapshot = {"stock_plate": {"a": "静态板", "b": "乙板"}, "s2p": {"a": ["静态板", "乙板"], "b": ["乙板"]}}
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol=klines,
        auction_rows={"a": {"px_milli": 11000, "match_amt_yuan": 300, "limit_state": 1},
                      "b": {"px_milli": 10000, "match_amt_yuan": 200, "limit_state": 0}},
        snapshot=snapshot, reasons_by_symbol={}, reason_asof_unknown=False,
    )
    memberships = result["memberships"]
    assert memberships.by_symbol["a"][0].tier == 3
    assert memberships.by_symbol["a"][0].plate == "静态板"
    stats = {row.plate: row for row in result["aggregation"].stats}
    assert stats["静态板"].amount_sum == 300
    assert stats["静态板"].limit_up_count == 1
    assert result["quality"]["yest_pool_member_count"] == 1


def test_no_plate_is_counted_and_never_filled_with_other():
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol={},
        auction_rows={"a": {"px_milli": 10000, "match_amt_yuan": None, "limit_state": None}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={}, reason_asof_unknown=False,
        min_auction_rows=1,
    )
    assert result["quality"]["unmapped_auction_symbol_count"] == 1
    assert "其他" not in {row.plate for rows in result["memberships"].by_symbol.values() for row in rows}
    assert result["quality"]["missing_counts"]["match_amt_yuan"] == 1


def test_reason_without_date_is_flagged_unknown():
    result = build_day(
        trade_date="2026-01-05", klines_by_symbol={},
        auction_rows={"a": {"px_milli": 10000, "match_amt_yuan": 100, "limit_state": 0}},
        snapshot={"stock_plate": {}, "s2p": {}},
        reasons_by_symbol={"a": [{"reason": "人工智能"}]}, reason_asof_unknown=True,
        min_auction_rows=1,
    )
    assert result["reason_asof_unknown"] is True
    assert any(row.tier == 2 for row in result["memberships"].by_symbol["a"])


def test_output_directory_must_be_empty(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "existing.json").write_text("{}")
    with pytest.raises(ValueError, match="empty"):
        ensure_empty_output_dir(output)


def test_insufficient_day_is_recorded_as_skipped(tmp_path):
    from examples.run_offline_plate_pipeline import run_pipeline

    days = run_pipeline(
        dates=["2026-01-05"], klines_by_symbol={}, auction_by_date={"2026-01-05": {}},
        snapshot={"stock_plate": {}, "s2p": {}}, reasons_by_symbol={},
        start_date="2026-01-05", end_date="2026-01-05", min_auction_rows=2,
    )
    assert days["skipped"] == [{"date": "2026-01-05", "reason": "auction_row_count_below_minimum", "auction_row_count": 0}]



def test_kline_by_day_accepts_datetime_ts():
    import datetime as _dt
    from examples.run_offline_plate_pipeline import _kline_by_day

    row = (_dt.datetime(2026, 10, 12), 1, 1, 1, 1, 1, 1, 1, 0)
    assert _kline_by_day({"000001": [row]}) == {"2026-10-12": {"000001": row}}
