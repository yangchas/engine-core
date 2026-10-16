from engine_core.extremes_board import build_extremes_board


def _rows():
    return [
        {"symbol": "600001", "name": "甲", "main_plate": "通信", "auction_pct": 8,
         "auction_amount_yuan": 40_000_000, "auction_price_status": "FINAL", "limit_pct": 10,
         "p0932_pct": 5, "p0945_pct": 2, "close_pct": 1, "high_pct": 9, "low_pct": -1},
        {"symbol": "600002", "name": "乙", "main_plate": "芯片", "auction_pct": -7,
         "auction_amount_yuan": 10_000_000, "auction_price_status": "INDICATIVE", "limit_pct": 10,
         "p0932_pct": -3, "p0945_pct": 0, "close_pct": 2, "high_pct": 4, "low_pct": -8},
        {"symbol": "600003", "name": "丙", "main_plate": "医药", "auction_pct": 2,
         "auction_amount_yuan": 50_000_000, "auction_price_status": "FINAL", "limit_pct": 10,
         "p0932_pct": None, "p0945_pct": -2, "close_pct": None},
        {"symbol": "600004", "name": "丁", "main_plate": "银行", "auction_pct": 1,
         "auction_amount_yuan": 29_000_000, "auction_price_status": "FINAL", "limit_pct": 10},
        {"symbol": "301001", "name": "新股", "auction_pct": 30,
         "auction_amount_yuan": 100_000_000, "auction_price_status": "FINAL", "limit_pct": None,
         "is_new_listing": True},
    ]


def test_selection_uses_0925_threshold_union_and_reason_without_lookahead():
    rows = _rows()
    base = build_extremes_board(rows, k_abs=1, k_amount=1, min_auction_amount_yuan=30_000_000)
    selected = {r["symbol"]: r for r in base["auction"]["rows"]}
    assert set(selected) == {"600001", "600003"}
    assert selected["600001"]["selection_reason"] == "绝对值#1"
    assert selected["600003"]["selection_reason"] == "额#1"
    assert base["auction"]["excluded_unlimited"][0]["name"] == "新股"
    unknown_band = {"symbol": "600005", "name": "戊", "auction_pct": -5,
                    "auction_amount_yuan": 40_000_000, "auction_price_status": "FINAL"}
    unknown_result = build_extremes_board([unknown_band], k_abs=1, k_amount=0,
                                          min_auction_amount_yuan=0)
    assert unknown_result["auction"]["selected_count"] == 1
    assert unknown_result["auction"]["excluded_unlimited"] == []
    mutated = _rows()
    mutated[1]["close_pct"] = 99999
    mutated[1]["p0932_pct"] = -99999
    assert [r["symbol"] for r in build_extremes_board(
        mutated, k_abs=1, k_amount=1, min_auction_amount_yuan=30_000_000)["auction"]["rows"]] == list(selected)


def test_unlimited_listing_is_never_called_limit_price_or_buyable():
    row = {"symbol": "301569", "name": "新股", "auction_pct": 285.7,
           "auction_amount_yuan": 100_000_000, "is_new_listing": True,
           "limit_pct": None}
    board = build_extremes_board([row], k_abs=1, k_amount=1,
                                 min_auction_amount_yuan=0)
    assert board["auction"]["excluded_unlimited"][0]["auction_state"] == "新股无涨跌停限制"
    assert "绝对值#" in board["rank_legend"] and "额#" in board["rank_legend"]


def test_stage_tracking_keeps_missing_missing_and_indicative_label():
    board = build_extremes_board(_rows(), k_abs=2, k_amount=1, min_auction_amount_yuan=0)
    indicative = next(r for r in board["open"]["rows"] if r["symbol"] == "600002")
    assert indicative["auction_price_status"] == "INDICATIVE"
    assert indicative["tracking"]["0932_pct"] == -3
    assert indicative["tracking"]["0935_pct"] is None
    assert indicative["tracking"]["close_pct"] == 2
    assert indicative["tracking"]["delta_close_pp"] == 9
    assert board["auction"]["rows"][0].get("tracking") is None
    assert board["close"]["missing_close_count"] > 0


def test_outcome_boards_rank_only_the_fixed_cohort_and_summaries_are_descriptive():
    board = build_extremes_board(_rows(), stage="close", k_abs=2, k_amount=2, min_auction_amount_yuan=0,
                                 threshold_pct=3)
    close = board["close"]
    assert all(row["symbol"] in {r["symbol"] for r in board["auction"]["rows"]}
               for row in close["rebound_top8"] + close["weak_top8"])
    assert close["high_open_group"]["n"] == 1
    assert close["low_open_group"]["n"] == 1
    assert close["amount_top20"]["n"] == 1
    assert close["sample_label"] == "当日描述"

    rows = _rows()[:3]
    rows[2]["close_pct"] = 2
    weighted = build_extremes_board(rows, stage="close", k_abs=0, k_amount=2,
                                    min_auction_amount_yuan=0)["close"]["amount_top20"]
    assert weighted["amount_weighted_pct"] == (1 * 40_000_000 + 2 * 50_000_000) / 90_000_000
