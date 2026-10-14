from engine_core.plate_stock_view import (
    build_plate_stock_view,
    continuation_state,
    select_focus_plates,
)


def test_focus_selection_deduplicates_sources_caps_and_excludes_small_or_generic():
    plates = [
        {"plate": f"p{i}", "kind": "THEME", "times": {"0925": {
            "effective_member_count": 6, "pct_equal_weight": i / 100,
            "amount_sum": 1000 if i == 34 else 100 - i, "yest_pool_member_count": i,
        }}} for i in range(35)
    ]
    plates += [
        {"plate": "small", "kind": "THEME", "times": {"0925": {"effective_member_count": 4}}},
        {"plate": "generic", "kind": "GENERIC", "generic": True,
         "times": {"0925": {"effective_member_count": 9}}},
    ]
    chosen = select_focus_plates(plates, "0925", independent=[{"plate": "p34", "direction": "STRONG"}])
    by_name = {row["plate"]: row for row in chosen}
    assert len(chosen) <= 30
    assert "small" not in by_name and "generic" not in by_name
    assert by_name["p34"]["sources"] == ["独立", "涨幅", "额", "昨板"]


def test_member_selection_fills_top_amount_then_top_three_return_without_duplicates_and_keeps_missing():
    stocks = []
    for i in range(10):
        stocks.append({"symbol": f"00000{i}", "name": f"s{i}", "plates": [{"plate": "主题"}],
                       "times": {"0925": {"pct_vs_pre_close": i / 100,
                                            "amount": None if i == 9 else float(i),
                                            "rest_bid": None, "rest_ask": None}},
                       "in_yest_pool": False, "scenario": {"auction": {"final_flag": "FINAL",
                           "matched_amount_0925": None if i == 9 else float(i)}}})
    plates = [{"plate": "主题", "kind": "THEME", "times": {"0925": {
        "effective_member_count": 9, "pct_equal_weight": 0.04, "amount_sum": 36,
        "up_count": 9, "yest_pool_member_count": 0,
    }}}]
    view = build_plate_stock_view(stocks, plates)
    rows = view["auction"]["plates"][0]["members"]
    assert [row["symbol"] for row in rows[:4]] == ["000008", "000007", "000006", "000005"]
    assert len({row["symbol"] for row in rows}) == len(rows)
    missing = next(row for row in rows if row["symbol"] == "000009")
    assert missing["matched_amount_yuan"] is None


def test_continuation_threshold_boundaries():
    assert continuation_state(1.0, 0.7) == "延续"
    assert continuation_state(1.0, 0.699) == "转弱"
    assert continuation_state(1.0, 0.0) == "反转"
    assert continuation_state(-1.0, 0.0) == "反转"


def test_opening_view_marks_breakouts_and_keeps_missing_deltas_missing():
    stocks = [
        {"symbol": "000001", "name": "甲", "primary_plate": "主题A",
         "plates": [{"plate": "主题A"}, {"plate": "主题B"}], "in_yest_pool": True, "lb_days": 2,
         "times": {"0925": {"pct_vs_pre_close": 0.02, "amount": 100, "limit_state": 1,
                              "rest_bid": 10},
                   "0932": {"pct_vs_pre_close": 0.05, "amount": 200, "price": 10.5,
                              "at_limit_price": False}}},
        {"symbol": "000002", "name": "乙", "plates": [{"plate": "主题A"}],
         "times": {"0925": {"pct_vs_pre_close": None, "amount": None},
                   "0932": {"pct_vs_pre_close": 0.01, "amount": None, "price": 10.1}}},
    ]
    stages = {"effective_member_count": 5, "pct_equal_weight": 0.03, "amount_sum": 300,
              "yest_pool_member_count": 1}
    plates = [{"plate": name, "kind": "THEME", "times": {
        "0925": {**stages, "pct_equal_weight": 0.02},
        "0932": {**stages, "pct_equal_weight": 0.03},
        "0935": {**stages}, "0940": {**stages}, "0945": {**stages},
    }} for name in ("主题A", "主题B")]
    view = build_plate_stock_view(stocks, plates)
    row = next(item for item in view["opening"]["0932"]["plates"][0]["members"]
               if item["symbol"] == "000001")
    assert row["newly_emerged"] is True
    assert row["opened_limit"] is True
    missing = next(item for item in view["opening"]["0932"]["plates"][0]["members"]
                   if item["symbol"] == "000002")
    assert missing["change_vs_0925_pp"] is None


def test_first_focus_fixture_values_trace_back_to_source_times():
    stocks = []
    plates = []
    for plate_index, plate_name in enumerate(("主题甲", "主题乙")):
        member_ids = []
        for rank in range(5):
            symbol = f"0000{plate_index}{rank}"
            member_ids.append(symbol)
            pct = (plate_index + 1) / 100 + rank / 1000
            amount = (plate_index + 1) * 1000 + rank
            stocks.append({"symbol": symbol, "name": f"股{plate_index}{rank}",
                           "plates": [{"plate": plate_name, "rank": 1}],
                           "times": {"0925": {"pct_vs_pre_close": pct, "amount": amount,
                                                "rest_bid": amount / 10, "rest_ask": amount / 20,
                                                "price_source": "TICK_PX_FINAL"}},
                           "in_yest_pool": rank == 0, "lb_days": 2 if rank == 0 else None,
                           "scenario": {"auction": {"final_flag": "FINAL"}}})
        plates.append({"plate": plate_name, "kind": "THEME", "times": {"0925": {
            "effective_member_count": 5, "pct_equal_weight": (plate_index + 1) / 100,
            "amount_sum": sum((plate_index + 1) * 1000 + rank for rank in range(5)),
            "yest_pool_member_count": 1,
        }}})
    view = build_plate_stock_view(stocks, plates)
    assert [row["plate"] for row in view["auction"]["plates"]] == ["主题乙", "主题甲"]
    assert all(len(row["members"]) == 5 for row in view["auction"]["plates"])
    source_by_symbol = {row["symbol"]: row["times"]["0925"] for row in stocks}
    for focus in view["auction"]["plates"][:2]:
        for member in focus["members"][:3]:
            source = source_by_symbol[member["symbol"]]
            assert member["pct"] == source["pct_vs_pre_close"]
            assert member["matched_amount_yuan"] == source["amount"]
            assert member["rest_bid_yuan"] == source["rest_bid"]
            assert member["rest_ask_yuan"] == source["rest_ask"]
