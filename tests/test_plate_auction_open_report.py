from engine_core.plate_auction_open_report import build_session_report


def _source():
    return {
        "trade_date": "2026-10-13",
        "snapshot": {
            "stock_plate": {"000001": "银行", "000002": "银行"},
            "s2p": {"000001": ["金融"], "000002": ["金融"]},
        },
        "pre_close_by_symbol": {"000001": 10.0, "000002": 20.0},
        "auction_by_tag": {
            "0920": {"000001": {"px_milli": 10100, "match_amt_yuan": 100, "rest_bid_amt_yuan": 5}},
            "0924": {"000001": {"px_milli": 10200, "match_amt_yuan": 200}},
            "0925": {"000001": {"px_milli": 10300, "match_amt_yuan": 300, "limit_state": 1}},
        },
        "opening_by_symbol": {
            "000001": {"ts": "2026-10-13 09:32:00", "px_milli": 11000, "amt_yuan": 900},
        },
        "yest_pool_by_symbol": {"000001": {"lb_days": 2}},
        "degradation": ["KAIPAN_INPUTS_MISSING"],
    }


def test_auction_match_and_opening_traded_amounts_are_separate():
    result = build_session_report(**_source())
    stock = next(row for row in result["stocks"] if row["symbol"] == "000001")
    assert stock["times"]["0925"]["amount"] == 300
    assert stock["times"]["0925"]["amount_kind"] == "MATCH"
    assert stock["times"]["0932"]["amount"] == 900
    assert stock["times"]["0932"]["amount_kind"] == "TRADED"
    assert stock["times"]["0932"]["amount"] != 900 + 300
    assert stock["times"]["0925"]["input_cutoff"] == "2026-10-13T09:25:00"
    assert stock["times"]["0932"]["input_cutoff"] == "2026-10-13T09:32:00"
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    assert bank["times"]["0932"]["input_cutoff"] == "2026-10-13T09:32:00"


def test_opening_tick_after_cutoff_is_not_used():
    source = _source()
    source["opening_by_symbol"]["000001"]["ts"] = "2026-10-13 09:32:01"
    result = build_session_report(**source)
    stock = next(row for row in result["stocks"] if row["symbol"] == "000001")
    assert stock["times"]["0932"]["amount"] is None
    assert "NO_0932_ASOF_ROW" in stock["flags"]


def test_membership_aggregation_deduplicates_members_and_keeps_missing_none():
    result = build_session_report(**_source())
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    assert bank["member_count"] == 2
    assert bank["times"]["0925"]["covered_count"] == 1
    assert bank["times"]["0925"]["amount_sum"] == 300
    stock = next(row for row in result["stocks"] if row["symbol"] == "000002")
    assert stock["times"]["0920"]["price"] is None
    assert stock["times"]["0920"]["amount"] is None


def test_yesterday_pool_marker_and_limit_state_are_preserved():
    result = build_session_report(**_source())
    stock = next(row for row in result["stocks"] if row["symbol"] == "000001")
    assert stock["in_yest_pool"] is True
    assert stock["lb_days"] == 2
    assert stock["times"]["0925"]["limit_state"] == 1
    assert stock["times"]["0932"]["limit_state"] is None
    assert stock["times"]["0932"]["at_limit_price"] is True


def test_report_contains_degradation_and_virtual_match_semantics():
    result = build_session_report(**_source())
    assert "KAIPAN_INPUTS_MISSING" in result["report_md"]
    assert "竞价金额为虚拟匹配额" in result["report_md"]
    assert "昨日涨停池成员" in result["report_md"]
