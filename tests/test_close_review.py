from engine_core.close_review import derive_close_review


def test_big_meat_loss_visibility_and_missed_opportunities():
    rows = [
        {"symbol": "600001", "name": "甲", "auction_pct": 0, "p0932_pct": 3, "p0945_pct": 4,
         "close_pct": 6, "delta_pp": 6, "auction_amount_yuan": 200, "plates": ["芯片"]},
        {"symbol": "600002", "name": "乙", "auction_pct": 0, "p0932_pct": 0, "p0945_pct": 0,
         "close_pct": -6, "delta_pp": -6, "auction_amount_yuan": 300, "plates": ["化工"]},
        {"symbol": "600003", "name": "丙", "auction_pct": 1, "p0932_pct": 0, "p0945_pct": 0,
         "close_pct": 7, "delta_pp": 6, "auction_amount_yuan": 100, "plates": ["化工"]},
    ]
    report = derive_close_review(rows, trade_date="2026-01-01", live_report={"chapters": {"emerging": [{"symbol": "600001"}]}})
    assert report["big_meat"][0]["visibility"] == "0945前可见"
    assert report["big_loss"][0]["visibility"] == "盘中才出现"
    assert report["missed_opportunities"]["symbols"] == ["600003"]
    assert report["missed_opportunities"]["plate_counts"] == {"化工": 1}


def test_emerging_close_scorecard_hand_calculation():
    rows = [
        {"symbol": "600001", "auction_pct": 0, "p0945_pct": 3, "close_pct": 10, "limit_pct": 10, "auction_amount_yuan": 200},
        {"symbol": "600002", "auction_pct": 0, "p0945_pct": 4, "close_pct": -1, "limit_pct": 10, "auction_amount_yuan": 100},
        {"symbol": "600003", "auction_pct": 0, "p0945_pct": 2, "close_pct": 6, "limit_pct": 10},
    ]
    report = derive_close_review(rows, trade_date="2026-01-01", live_report={"chapters": {"emerging": [{"symbol": "600001"}, {"symbol": "600002"}]}})
    score = report["emerging_scorecard"]
    assert {k: score["emerging"][k] for k in ("n", "close_ge_5", "sealed_up", "turned_negative")} == {"n": 2, "close_ge_5": 1, "sealed_up": 1, "turned_negative": 1}
    assert score["emerging"]["close_ge_5_rate"] == 0.5
    assert score["amount_top_k"]["n"] == 2


def test_missing_inputs_are_downgraded_not_errors():
    report = derive_close_review([], trade_date="2026-01-01")
    assert report["missed_opportunities"]["status"] == "UNAVAILABLE_NO_LIVE_REPORT"
    assert report["yesterday_review"]["status"] == "UNAVAILABLE_NO_PREV_REPORT"
    assert report["plates"]["status"] == "UNAVAILABLE_NO_RELATION"
