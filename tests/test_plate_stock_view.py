from engine_core.plate_stock_view import (
    build_plate_stock_view,
    continuation_state,
    render_plate_stock_view,
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


def test_opening_limit_count_falls_back_to_scenario_and_top1_has_no_sign():
    stocks = [{
        "symbol": "000001", "name": "甲", "plates": [{"plate": "主题"}],
        "scenario": {"open": {"at_limit_0932": True}},
        "times": {"0925": {}, "0932": {"pct_vs_pre_close": 0.05, "amount": 100}},
    }]
    plates = [{"plate": "主题", "kind": "THEME", "times": {
        "0925": {"effective_member_count": 5, "pct_equal_weight": 0.01, "amount_sum": 10},
        "0932": {"effective_member_count": 5, "pct_equal_weight": 0.05, "amount_sum": 100,
                 "top1_concentration": 0.3026},
    }}]
    view = build_plate_stock_view(stocks, plates)
    row = view["opening"]["0932"]["plates"][0]
    assert row["limit_up_count"] == 1
    rendered = render_plate_stock_view(view)
    assert "Top1 30.3%" in rendered
    assert "Top1 +30.3%" not in rendered


def test_opening_0935_and_0940_fold_insignificant_plates_only():
    stocks = [{"symbol": f"00000{i}", "name": f"甲{i}", "plates": [{"plate": f"主题{i}"}],
               "times": {tag: {"pct_vs_pre_close": 0.01, "amount": 100}
                         for tag in ("0925", "0932", "0935", "0940", "0945")}}
              for i in range(2)]
    plates = []
    for i, pct_by_tag in enumerate((
        {"0925": 0.01, "0932": 0.01, "0935": 0.011, "0940": 0.011, "0945": 0.011},
        {"0925": 0.02, "0932": 0.02, "0935": 0.04, "0940": 0.04, "0945": 0.04},
    )):
        plates.append({"plate": f"主题{i}", "kind": "THEME", "times": {
            tag: {"effective_member_count": 5, "pct_equal_weight": pct, "amount_sum": 100}
            for tag, pct in pct_by_tag.items()}})
    rendered = render_plate_stock_view(build_plate_stock_view(stocks, plates))
    assert "变化≥1.0pp" in rendered
    assert "其余 1 个板块无显著变化(见 JSON)" in rendered
    assert rendered.count("#### 主题0（来源：") == 2  # 0932/0945 full; not 0935/0940
    assert rendered.count("#### 主题1（来源：") == 3  # 0940 has no further change from 0935


def test_emerging_stock_table_precedes_each_opening_plate_table():
    stocks = [{
        "symbol": "000001", "name": "甲", "primary_plate": "主题", "plates": [{"plate": "主题"}],
        "times": {"0925": {"pct_vs_pre_close": 0.02, "amount": 100},
                  **{tag: {"pct_vs_pre_close": 0.06, "amount": 200} for tag in ("0932", "0935", "0940", "0945")}},
    }]
    plates = [{"plate": "主题", "kind": "THEME", "times": {
        tag: {"effective_member_count": 5, "pct_equal_weight": 0.06 if tag != "0925" else 0.02,
             "amount_sum": 200}
        for tag in ("0925", "0932", "0935", "0940", "0945")}}]
    independent = {tag: {"selected": [{"plate": "主题"}]} for tag in ("0932", "0935", "0940", "0945")}
    rendered = render_plate_stock_view(build_plate_stock_view(stocks, plates, independent_by_tag=independent))
    for tag in ("0932", "0935", "0940", "0945"):
        section = rendered.split(f"### {tag}\n", 1)[1]
        assert section.index(f"#### {tag} 竞价后新冒头") < section.index("#### 主题（来源：")
        assert "| 代码 | 名称 | 主板块 | 0925 涨幅 | 当前涨幅 | 成交额(亿) | 昨板 | 独立入选板块 |" in section
        assert "000001 | 甲 | 主题 | +2.00% | +6.00% | 0.00 | 否 | 是" in section
