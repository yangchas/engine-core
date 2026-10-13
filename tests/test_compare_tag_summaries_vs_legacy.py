from examples.compare_task008_tag_summaries_vs_legacy import compare
from examples.compare_task008_td_rows_vs_legacy import compare as compare_td_rows

LEGACY = {"projections": [
    {"tag": "0920", "summary": {
        "ts": 1000, "total_stocks": 10, "valid_stock_count": 4, "unavailable_stock_count": 6,
        "high_open_count": 2, "low_open_count": 1, "flat_open_count": 1,
        "total_auction_amount_yuan": 500, "limit_up_count": 0, "limit_down_count": 1,
        "total_limit_up_bid_amount_yuan": 0}},
    {"tag": "0925", "summary": {}},
]}


def _core(valid, t, poll):
    return {"poll": poll, "source_time_max": t, "0920": {
        "stock_count": 10, "valid_stock_count": valid, "unavailable_stock_count": 10 - valid,
        "positive_count": 2, "negative_count": 1, "flat_count": 1,
        "auction_amount_yuan": 500, "limit_up_count": 0, "limit_down_count": 1,
        "limit_up_seal_amount_yuan": 0}}


def test_picks_nearest_poll_and_reports_gap_and_field_differences():
    polls = [_core(9, 4000, 0), _core(4, 1003, 1), _core(5, 900, 2)]
    out = compare(polls, LEGACY)
    assert "0925" not in out  # empty legacy summary is skipped, not compared
    r = out["0920"]
    assert r["core_poll"] == 1 and r["time_gap_ms"] == 3
    assert r["status"] == "ALL_EQUAL" and r["fields_equal"] == 10


def test_reports_differences_without_repairing():
    out = compare([_core(7, 1000, 0)], LEGACY)["0920"]
    assert out["status"] == "DIFFERENT"
    assert out["differences"]["valid_stock_count"] == {"legacy": 4, "core": 7, "delta": 3}


def test_no_poll_for_tag():
    assert compare([], LEGACY)["0920"]["status"] == "NO_COMPARABLE_POLL"


def test_far_poll_is_not_time_aligned_even_when_equal():
    out = compare([_core(4, 1000 + 60_000, 0)], LEGACY)["0920"]
    assert out["status"] == "NOT_TIME_ALIGNED" and out["time_gap_ms"] == 60_000


def test_td_comparison_does_not_treat_matching_unknown_fields_as_equal():
    rows = [{"symbol": "000001", "auction_tag": "0920", "px_milli": 10000,
             "chg_bp": 0, "match_amt_yuan": 100, "rest_bid_amt_yuan": None,
             "rest_ask_amt_yuan": 0, "limit_state": 1}]
    legacy = {"projections": [{"tag": "0920", "summary": {
        "total_stocks": 1, "valid_stock_count": 1, "unavailable_stock_count": 0,
        "high_open_count": 0, "low_open_count": 0, "flat_open_count": 1,
        "total_auction_amount_yuan": 100, "limit_up_count": 1,
        "limit_down_count": 0, "total_limit_up_bid_amount_yuan": None}}]}
    result = compare_td_rows(rows, legacy)["0920"]
    assert result["status"] != "ALL_EQUAL"
    assert result["differences"]["total_limit_up_bid_amount_yuan"] == {
        "legacy": None, "td": None, "unknown": True}
