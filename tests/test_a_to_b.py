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
    assert result[0]["reason"] == "无板块关系"


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


def test_main_plate_uses_qualified_theme_and_common_membership_breaks_peer_ties():
    stocks = [
        {"symbol": "600001", "lb_days": 2, "plates": ["物流", "芯片", "通信"],
         "main_plate": "物流", "p0925_pct": 0, "auction_amount_yuan": 1},
        {"symbol": "600002", "plates": ["芯片", "通信"], "p0925_pct": 1,
         "auction_amount_yuan": 100},
        {"symbol": "600003", "plates": ["通信"], "p0925_pct": 1,
         "auction_amount_yuan": 100},
        {"symbol": "600004", "plates": ["芯片"], "p0925_pct": 1,
         "auction_amount_yuan": 999, "in_yest_pool": True},
        {"symbol": "600005", "plates": ["芯片"], "p0925_pct": None,
         "auction_amount_yuan": 999},
    ]
    metadata = {
        "物流": {"class": "BROAD", "size": 297},
        "芯片": {"class": "THEME", "size": 40},
        "通信": {"class": "NICHE", "size": 20},
    }
    result = build_a_to_b(stocks, trade_date="2026-10-15", plate_metadata=metadata)
    assert result[0]["plate"] == "通信"
    assert [row["symbol"] for row in result[0]["candidates"]] == ["600002", "600003"]
    assert result[0]["candidates"][0]["common_plate_count"] == 2


def test_no_qualified_theme_plate_has_explicit_reason():
    result = build_a_to_b([{"symbol": "600001", "lb_days": 2, "plates": ["物流"],
                            "p0925_pct": 0}], trade_date="2026-10-15",
                          plate_metadata={"物流": {"class": "BROAD", "size": 297}})
    assert result[0]["plate"] is None
    assert result[0]["reason"] == "无合格主题板块"


def test_close_derived_unlimited_flag_does_not_remove_prior_known_anchor():
    rows = [{"symbol": "600001", "name": "A", "lb_days": 2, "main_plate": "传媒",
             "is_unlimited_price_band": True, "p0925_pct": 0},
            {"symbol": "600002", "name": "B", "main_plate": "传媒", "p0925_pct": 1,
             "auction_amount_yuan": 10}]
    result = build_a_to_b(rows, trade_date="2026-10-15")
    assert len(result) == 1
    assert result[0]["a_symbol"] == "600001"


def test_anchors_on_same_main_plate_are_marked_as_shared():
    rows = [
        {"symbol": "600001", "lb_days": 2, "main_plate": "传媒", "p0925_pct": 0,
         "auction_amount_yuan": 10},
        {"symbol": "600002", "auction_limit_state": 1, "main_plate": "传媒",
         "rest_bid_yuan": 10, "p0925_pct": 10, "auction_amount_yuan": 20},
        {"symbol": "600003", "main_plate": "传媒", "p0925_pct": 1,
         "auction_amount_yuan": 5},
    ]
    result = build_a_to_b(rows, trade_date="2026-10-15")
    assert [row["plate_display"] for row in result] == ["同板块 传媒", "同板块 传媒"]
    assert all(any(peer["symbol"] == "600003" for peer in row["candidates"])
               for row in result)


def test_near_limit_price_without_locked_state_does_not_create_anchor():
    rows = [{"symbol": "600001", "auction_pct": 9.84, "limit_pct": 10,
             "auction_limit_state": 0, "main_plate": "传媒", "auction_amount_yuan": 10}]
    assert build_a_to_b(rows, trade_date="2026-10-15") == []


def test_yesterday_pool_lb_days_wins_over_default_zero_and_marks_conflict():
    rows = [
        {"symbol": "000006", "name": "冰山冷热", "in_yest_pool": True,
         "prior_limit_streak": 0, "lb_days": 2, "main_plate": "芯片",
         "auction_amount_yuan": 10},
        {"symbol": "000007", "name": "同伴", "main_plate": "芯片",
         "auction_pct": 1, "auction_amount_yuan": 5},
    ]
    result = build_a_to_b(rows, trade_date="2026-10-15")
    assert len(result) == 1
    assert result[0]["a_prior_streak"] == 2
    assert result[0]["streak_status"] == "字段冲突"


def test_anchor_requires_sealed_auction_state_and_rest_bid_or_prior_streak():
    rows = [
        {"symbol": "688123", "name": "联亚药业", "auction_pct": 285.71,
         "limit_pct": 20, "auction_limit_state": 0, "main_plate": "芯片",
         "auction_amount_yuan": 3, "is_unlimited_price_band": True},
        {"symbol": "600001", "auction_limit_state": 1, "rest_bid_yuan": 0,
         "main_plate": "芯片", "auction_amount_yuan": 4},
        {"symbol": "600002", "auction_limit_state": 1, "rest_bid_yuan": 10,
         "main_plate": "芯片", "auction_amount_yuan": 4},
    ]
    result = build_a_to_b(rows, trade_date="2026-10-15")
    assert [row["a_symbol"] for row in result] == ["600002"]
    assert result[0]["a_status"] == "竞价涨停价且有封单"


def test_candidate_amount_never_falls_back_to_daily_amount_and_missing_is_explicit():
    rows = [
        {"symbol": "600001", "lb_days": 2, "main_plate": "芯片", "auction_pct": 0,
         "auction_amount_yuan": 1},
        {"symbol": "600002", "main_plate": "芯片", "auction_pct": 1, "amount_yuan": 999},
    ]
    result = build_a_to_b(rows, trade_date="2026-10-15")
    assert result[0]["candidates"] == []
    assert result[0]["reason"] == "缺少竞价价/额"


def test_default_zero_streak_without_yesterday_pool_value_is_missing():
    from engine_core.a_to_b import _prior_streak
    assert _prior_streak({"prior_limit_streak": 0}) == (None, None)


def test_close_a_to_b_emits_anchor_prices_and_candidate_display_fields():
    rows = [
        {"symbol": "000530", "name": "冰山冷热", "lb_days": 2, "main_plate": "外卖概念",
         "auction_pct": 9.84, "close_pct": 10, "auction_amount_yuan": 1},
        {"symbol": "300781", "name": "因赛集团", "main_plate": "外卖概念", "auction_pct": -2.18,
         "p0945_pct": -0.89, "close_pct": -4.8, "auction_amount_yuan": 4742500,
         "prior_range_position20_pct": 79},
    ]
    result = build_a_to_b(rows, trade_date="2026-10-15")
    assert result[0]["a_auction_pct"] == 9.84
    assert result[0]["a_close_pct"] == 10
    assert result[0]["candidates"][0]["position"] == "高"


def test_v2_contract_explains_anchor_strength_reasons_plate_and_b_selection_without_lookahead():
    rows = [
        {"symbol": "600001", "name": "A股", "lb_days": 3, "main_plate": "芯片",
         "auction_limit_state": 1, "rest_bid_yuan": 50, "limit_pct": 10,
         "p0925_pct": 10, "auction_amount_yuan": 200, "auction_amount_bp": 200,
         "p0932_pct": 11, "p0945_pct": 12, "close_pct": 13},
        {"symbol": "600002", "name": "B股", "main_plate": "芯片", "limit_pct": 10,
         "p0925_pct": 2, "p0924_pct": 1, "auction_amount_yuan": 150,
         "prior_range_position20_pct": 20, "p0932_pct": 3, "p0945_pct": 4,
         "close_pct": 5},
        {"symbol": "600003", "name": "C股", "main_plate": "芯片", "limit_pct": 10,
         "p0925_pct": 1, "auction_amount_yuan": 100, "p0932_pct": 1,
         "p0945_pct": 2, "close_pct": 3},
    ]
    result = build_a_to_b(rows, trade_date="2026-10-15", stage="auction", relation_asof="2026-10-14",
                          path_frequencies={"UNKNOWN": {"k": 0, "n": 79, "days": 14}})
    anchor = result[0]
    assert anchor["schema_version"] == 2
    assert {item["rule_id"] for item in anchor["a_reasons"]} >= {"R1", "R2", "R3", "R4"}
    assert anchor["a_strength"]["auction_amount_rank"] == 1
    assert anchor["a_strength"]["path_type"] == "UNKNOWN"
    assert anchor["a_strength"]["path_frequency"] is None
    assert anchor["plate_state"]["auction_amount_yuan"] == 450
    assert anchor["sentiment_state"]["label"] == "UNKNOWN"
    assert anchor["b_candidates"][0]["name"] == "B股"
    assert len(anchor["b_candidates"][0]["reasons"]) <= 2
    assert "p0945_pct" not in anchor["b_candidates"][0]["metrics"]
    assert "close_pct" not in anchor["b_candidates"][0]["metrics"]
