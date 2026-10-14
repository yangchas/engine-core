from engine_core.stock_scenario import build_stock_scenarios


def _stock(symbol, *, auction_pct=0.01, open_pct=0.02, final="TICK_PX_FINAL",
           amount=100.0, open_amount=200.0, plate="主题", state=None, at_limit=False):
    return {
        "symbol": symbol, "primary_plate": plate,
        "times": {
            "0920": {"pct_vs_pre_close": auction_pct},
            "0924": {"pct_vs_pre_close": auction_pct},
            "0925": {"pct_vs_pre_close": auction_pct, "amount": amount,
                     "rest_bid": 5, "rest_ask": 6, "limit_state": state,
                     "price_source": final, "price_flags": []},
            "0932": {"pct_vs_pre_close": open_pct, "amount": open_amount,
                     "at_limit_price": at_limit},
            "0935": {"pct_vs_pre_close": open_pct, "amount": open_amount},
            "0940": {"pct_vs_pre_close": open_pct, "amount": open_amount},
            "0945": {"pct_vs_pre_close": open_pct, "amount": open_amount,
                     "at_limit_price": at_limit},
        },
    }


def test_bands_final_flag_missing_values_and_amount_ratio():
    stocks = [
        _stock("A", auction_pct=0.01, open_pct=0.014),
        _stock("B", auction_pct=-0.01, open_pct=-0.016),
        _stock("C", auction_pct=0.009, open_pct=None, final="TICK_BOOK1_INDICATIVE"),
        _stock("D", auction_pct=-0.011, amount=0, open_pct=-0.016),
    ]
    stocks[2]["times"]["0932"]["pct_vs_pre_close"] = None
    stocks[2]["times"]["0945"]["pct_vs_pre_close"] = None
    out = build_stock_scenarios(stocks, {}, primary_plate_of=lambda row: row["primary_plate"])
    assert out["A"]["path"]["auction_band"] == "HIGH"
    assert out["A"]["path"]["change_band"] == "SAME"
    assert out["B"]["path"]["auction_band"] == "LOW"
    assert out["B"]["path"]["change_band"] == "DOWN"
    assert out["C"]["auction"]["final_flag"] == "INDICATIVE"
    assert out["C"]["change"]["d_0945_vs_0925"] is None
    assert out["C"]["open_amount_multiple"] == 2
    assert out["D"]["open_amount_multiple"] is None
    assert out["D"]["path"]["change_band"] == "DOWN"


def test_relatives_and_minimum_tie_ranks_with_stage_member_counts():
    stocks = [_stock("A", auction_pct=0.1, amount=50, open_pct=0.2, open_amount=200),
              _stock("B", auction_pct=0.1, amount=80, open_pct=0.1, open_amount=200),
              _stock("C", auction_pct=0.0, amount=20, open_pct=0.2, open_amount=50)]
    out = build_stock_scenarios(stocks, {"主题": {"0925": 0.05, "0932": 0.1, "0945": 0.15}},
                                primary_plate_of=lambda row: row["primary_plate"])
    assert out["A"]["vs_plate"]["rel_0925"] == 5
    assert out["A"]["vs_plate"]["rel_0945"] == 5
    assert out["A"]["vs_plate"]["rank_pct_0925"] == 1
    assert out["B"]["vs_plate"]["rank_pct_0925"] == 1
    assert out["A"]["vs_plate"]["rank_amt_0925"] == 2
    assert out["A"]["vs_plate"]["rank_pct_0945"] == 1
    assert out["A"]["vs_plate"]["n_0925"] == out["A"]["vs_plate"]["n_0945"] == 3


def test_broke_limit_is_reported_and_missing_times_have_no_scenario():
    row = _stock("A", auction_pct=0.1, open_pct=0.08, state=1, at_limit=False)
    out = build_stock_scenarios([row, {"symbol": "B", "times": {}}], {},
                                primary_plate_of=lambda stock: stock.get("primary_plate"))
    assert out["A"]["path"]["label"].startswith("LIMIT_UP:")
    assert out["A"]["path"]["broke_limit"] is True
    assert out["B"] is None


def test_exact_threshold_edges_and_missing_path_reason():
    stocks = [_stock("A", auction_pct=0.01, open_pct=0.015),
              _stock("B", auction_pct=-0.01, open_pct=-0.015),
              _stock("C", auction_pct=0, open_pct=0.0049)]
    out = build_stock_scenarios(stocks, {}, primary_plate_of=lambda row: row["primary_plate"])
    assert out["A"]["path"]["change_band"] == "UP"
    assert out["B"]["path"]["change_band"] == "DOWN"
    assert out["C"]["path"]["change_band"] == "SAME"
    missing = _stock("M", auction_pct=None, open_pct=0.02)
    scenario = build_stock_scenarios([missing], {}, primary_plate_of=lambda row: row["primary_plate"])["M"]
    assert scenario["path"] is None
    assert scenario["path_missing_reason"]
