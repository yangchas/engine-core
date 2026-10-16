from engine_core.ladder_cluster import build_ladder_clusters, select_plate_strength


def test_ladder_clusters_group_by_eligible_theme_and_use_price_proxy_labels():
    highboards = [
        {"symbol": "000001", "name": "甲股", "streak": 4, "0925": 10.0, "match_amount_yuan": 20},
        {"symbol": "000002", "name": "乙股", "streak": 2, "0925": -6.0, "match_amount_yuan": 30},
        {"symbol": "000003", "name": "丙股", "streak": 2, "0925": 1.0, "match_amount_yuan": 50},
    ]
    a_to_b = [{"a_symbol": "000001", "plate": "芯片"},
              {"a_symbol": "000002", "plate": "芯片"},
              {"a_symbol": "000003", "plate": "未知板"}]
    plates = {"芯片": {"class": "THEME", "size": 60},
              "未知板": {"class": "EVENT", "size": 90}}

    result = build_ladder_clusters(highboards, a_to_b, plates, stage="auction")

    assert [row["name"] for row in result["clusters"]] == ["芯片", "无题材"]
    chip = result["clusters"][0]
    assert chip["judgement"] == "分化"
    assert "甲股 昨4板→+10.0%[封停价代理]" in chip["leaders"]
    assert "乙股 昨2板→-6.0%[大低开]" in chip["leaders"]
    assert result["first_board_summary"] == "昨首板0只"


def test_empty_highboards_and_first_board_counts_are_explicit():
    result = build_ladder_clusters(
        [], [], {}, stage="auction",
        first_board_rows=[{"symbol": "1", "name": "首板甲", "0925": 0.5}])

    assert result["clusters"] == []
    assert result["empty_message"] == "无昨高标簇"
    assert result["first_board_summary"] == "昨首板1只：高开1/低开0/封停价代理0"

    partial = build_ladder_clusters(
        [], [], {}, stage="auction",
        first_board_rows=[{"symbol": "1", "name": "首板甲", "0925": 0.5},
                          {"symbol": "2", "name": "首板乙"}])
    assert partial["first_board_summary"] == "昨首板2只：高开1/低开0/封停价代理0/缺1只"

    assert build_ladder_clusters(
        [{"symbol": "000001", "name": "甲", "streak": 2, "0925": 1},
         {"symbol": "000002", "name": "乙", "streak": 2, "0925": 2},
         {"symbol": "000003", "name": "丙", "streak": 2, "0925": 3}], [], {}, stage="auction"
    )["clusters"][0]["judgement"] == "承接强"
    assert build_ladder_clusters(
        [{"symbol": "000001", "name": "甲", "streak": 2, "0925": -1},
         {"symbol": "000002", "name": "乙", "streak": 2, "0925": -2},
         {"symbol": "000003", "name": "丙", "streak": 2, "0925": -3}], [], {}, stage="auction"
    )["clusters"][0]["judgement"] == "弱"


def test_plate_strength_uses_p80_absolute_delta_and_omits_zero_changes():
    current = [
        {"name": f"板{i}", "market_share_bp": now, "equal_weight_pct": pct,
         "amount_yuan": (i + 1) * 100, "top_stock_name": f"股{i}", "top_stock_pct": pct}
        for i, (now, pct) in enumerate([(110, 1), (120, 2), (130, 3), (140, 4), (150, 5), (160, 6)])
    ]
    previous = [{"name": f"板{i}", "market_share_bp": before}
                for i, before in enumerate([100, 115, 125, 135, 145, 160])]

    result = select_plate_strength(current, previous, eligible_names={f"板{i}" for i in range(6)})

    assert result["threshold_bp"] == 10
    assert all(row["delta_bp"] != 0 for row in result["up"] + result["down"])
    assert len(result["up"]) <= 5 and len(result["down"]) <= 5
    assert result["missing"] is False


def test_plate_strength_marks_missing_comparison_and_filters_noneligible_plates():
    result = select_plate_strength(
        [{"name": "芯片", "market_share_bp": 9000}], [], eligible_names={"芯片"})

    assert result["missing"] is True
    assert result["up"] == [] and result["down"] == []
    assert result["message"] == "0924份额缺失，变化暂不可判"
