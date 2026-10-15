from engine_core.a_to_b import build_a_to_b


def test_builds_point_in_time_a_to_b_and_drops_unbuyable_candidates():
    stocks = [
        {"symbol": "600001", "name": "A", "lb_days": 3, "auction_amount_yuan": 100,
         "p0925_pct": 0, "p0945_pct": 2, "close_pct": 3, "main_plate": "芯片"},
        {"symbol": "600002", "name": "B", "auction_amount_yuan": 80,
         "p0925_pct": 2, "p0932_pct": 3, "p0945_pct": 4, "close_pct": 5,
         "prior_range_position20_pct": 30, "main_plate": "芯片"},
        {"symbol": "600003", "name": "C", "auction_amount_yuan": 90,
         "p0925_pct": 10, "limit_pct": 10, "main_plate": "芯片"},
    ]
    result = build_a_to_b(stocks, trade_date="2026-10-15", stage="open", relation_asof="2026-10-14")
    assert result[0]["a_symbol"] == "600001"
    assert [row["symbol"] for row in result[0]["candidates"]] == ["600002"]
    assert "close_pct" not in result[0]["candidates"][0]  # intraday report is no-lookahead


def test_missing_relations_are_explicit():
    result = build_a_to_b([{"symbol": "600001", "lb_days": 2}], trade_date="2026-10-15")
    assert result[0]["reason"] == "缺关系数据"


def test_auction_stage_does_not_use_or_emit_later_prices():
    stocks = [
        {"symbol": "600001", "lb_days": 2, "main_plate": "芯片", "p0925_pct": 0,
         "auction_amount_yuan": 100, "open_pct": 9.9},
        {"symbol": "600002", "main_plate": "芯片", "limit_pct": 10, "p0925_pct": 9.9,
         "open_pct": 9.9, "auction_amount_yuan": 50, "p0945_pct": 3, "close_pct": 4},
        {"symbol": "600003", "main_plate": "芯片", "p0925_pct": 1,
         "open_pct": 1, "auction_amount_yuan": 40, "p0945_pct": 2, "close_pct": 3},
    ]
    result = build_a_to_b(stocks, trade_date="2026-10-15", stage="auction")
    peer = result[0]["candidates"][0]
    assert peer["symbol"] == "600003"
    assert peer["p0945_pct"] is None
    assert "close_pct" not in peer
