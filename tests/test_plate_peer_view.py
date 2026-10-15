from engine_core.plate_peer_view import (
    classify_day_type,
    classify_peer,
    limit_streaks,
    range_position,
)


def test_streaks_range_position_and_missing_history():
    history = [
        {"date": f"2026-01-{day:02d}", "symbol": "000001", "open": 10,
         "high": 11, "low": 9, "close": close, "pct_chg": 10}
        for day, close in enumerate(([10] * 18 + [11, 12]), start=1)
    ]
    assert limit_streaks(history, "2026-01-04")["000001"] == 3
    assert range_position(history, "000001", "2026-01-21") == 1.0
    assert range_position(history, "999999", "2026-01-21") is None


def test_peer_type_thresholds_and_amount_rank_are_explicit():
    flags = classify_peer(pct=0.02, plate_mean=0.01, amount_rank=15,
                          member_count=20, position=0.30, positive_amount=True)
    assert flags == ["LOW_POS", "STRONG_REL", "HOLLOW_TOP", "LOW_AMOUNT_UP"]
    assert "AMOUNT_LEAD" in classify_peer(pct=0.01, plate_mean=0.01, amount_rank=4,
                                         member_count=20, position=None, positive_amount=True)
    assert "WEAK_REL" in classify_peer(pct=-0.01, plate_mean=0.01, amount_rank=20,
                                       member_count=20, position=None, positive_amount=True)


def test_yesterday_day_type_priority_and_boundaries():
    assert classify_day_type(left_tail=300, right_tail=999, median=1, up_share=0.8) == "恐慌"
    assert classify_day_type(left_tail=0, right_tail=200, median=0.01, up_share=0.4) == "修复/强势"
    assert classify_day_type(left_tail=0, right_tail=0, median=-1.2, up_share=0.4) == "弱势"
    assert classify_day_type(left_tail=0, right_tail=0, median=0, up_share=0.25) == "弱势"


def test_cluster_deduplicates_a_coverage_and_tracks_peer_types():
    from engine_core.plate_peer_view import summarize_clusters

    rows = [
        {"symbol": "A", "plate": "传媒", "is_a": True, "a_buyable": False, "peer_flags": []},
        {"symbol": "B", "plate": "传媒", "is_a": False, "peer_flags": ["LOW_POS"], "has_observed_support": True},
        {"symbol": "C", "plate": "传媒", "is_a": False, "peer_flags": ["STRONG_REL", "HOLLOW_TOP"]},
    ]
    result = summarize_clusters(rows)
    assert result[0]["a_count"] == 1
    assert result[0]["peer_count"] == 2
    assert result[0]["low_pos_count"] == 1
    assert result[0]["strong_rel_count"] == 1
    assert result[0]["hollow_top_count"] == 1
    assert result[0]["a_unbuyable_with_peers"] is True


def test_peer_view_uses_bounded_theme_primary_and_visible_buyable_peers():
    from engine_core.plate_peer_view import build_plate_peer_view

    symbols = ["600001", "600002", "600003", "600004", "600005", "600006",
               "600007", "600008", "600009", "600010"]
    stocks = [{"symbol": symbol, "name": symbol} for symbol in symbols]
    auctions = [
        {"trade_date": "2026-10-15", "auction_tag": "0925", "symbol": "600001",
         "chg_bp": 1000, "match_amt_yuan": 100, "limit_state": 1},
        {"trade_date": "2026-10-15", "auction_tag": "0925", "symbol": "600002",
         "chg_bp": 100, "match_amt_yuan": 50, "limit_state": 0},
        {"trade_date": "2026-10-15", "auction_tag": "0925", "symbol": "600003",
         "chg_bp": 100, "match_amt_yuan": 50, "limit_state": 0},
        {"trade_date": "2026-10-15", "auction_tag": "0925", "symbol": "600004",
         "chg_bp": 100, "match_amt_yuan": 999, "limit_state": 0},
        {"trade_date": "2026-10-15", "auction_tag": "0925", "symbol": "600005",
         "chg_bp": None, "match_amt_yuan": 999, "limit_state": 0},
        {"trade_date": "2026-10-15", "auction_tag": "0925", "symbol": "600006",
         "chg_bp": 1000, "match_amt_yuan": 999, "limit_state": 1},
        *[{"trade_date": "2026-10-15", "auction_tag": "0925", "symbol": symbol,
           "chg_bp": 100, "match_amt_yuan": 25, "limit_state": 0}
          for symbol in ("600007", "600008", "600009", "600010")],
    ]
    memberships = {
        "theme": set(symbols),
        "second": {"600001", "600002", "600007", "600008", "600009", "600010"},
        "broad": {"600001", "600004"},
    }
    view = build_plate_peer_view(trade_date="2026-10-15", stocks=stocks, checkpoints=[],
        auctions=auctions, history=[], memberships=memberships,
        plate_names={"theme": "产业主题", "second": "细分行业", "broad": "物流"},
        generic_names=set(), plate_metadata={
            "theme": {"class": "THEME", "size": 20},
            "second": {"class": "NICHE", "size": 10},
            "broad": {"class": "BROAD", "size": 297},
        }, pool_symbols={"600004"})
    assert [row["plate"] for row in view["plates"]] == ["细分行业"]
    peers = view["plates"][0]["peers"]
    peer_symbols = [row["symbol"] for row in peers]
    assert peer_symbols[0] == "600002"
    assert set(peer_symbols) == {"600002", "600007", "600008", "600009", "600010"}
    assert "600004" not in peer_symbols  # yesterday's limit pool
    assert "600005" not in peer_symbols  # no visible auction price
    assert "600006" not in peer_symbols  # locked at the limit
    assert peers[0]["common_plate_count_by_a"]["600001"] == 2


def test_markdown_filters_a_truncates_plates_and_formats_values():
    from engine_core.plate_peer_view import render_plate_peer_markdown

    base_peer = {"symbol": "000010", "name": "同伴", "streak": 0,
        "range_position_20d": .25, "auction_pct": .0994, "relative_plate_pp": 1.234,
        "match_amount_yuan": 12_000_000, "amount_rank": 1, "amount_share_of_plate": .25,
        "rest_bid_yuan": 1_000_000, "rest_ask_yuan": 0,
        "opening": {"0932": {"pct": .1, "change_vs_0925_pp": .2}, "0945": {"pct": .2, "change_vs_0925_pp": .3}},
        "other_plates": [], "type_flags": ["LOW_POS"]}
    view = {"summary": {"market_auction": {"symbol_count": 1, "median_open_pct": -.0022,
        "down_le_3_pct_share": .1, "up_ge_3_pct_share": .2, "matched_amount_yuan": 117_000_000,
        "baseline_multiple": None, "limit_up_count": 1, "limit_down_count": 0},
        "yesterday": {"date": "2026-10-14", "day_type": "弱势", "left_tail": 2,
            "right_tail": 3, "median": -.2, "up_share": .3}, "yesterday_pool": None},
        "a_list": [
            {"symbol": "A", "name": "过滤A", "streak": 0, "auction_pct": .05,
             "match_amount_yuan": 2_000_000, "rest_bid_yuan": 0, "rest_ask_yuan": 0,
             "buyable": True, "sealed": False, "one_word": False, "plates": ["P1", "P2"]},
            {"symbol": "B", "name": "保留A", "streak": 2, "auction_pct": .0994,
             "match_amount_yuan": 12_000_000, "rest_bid_yuan": 1_000_000, "rest_ask_yuan": 0,
             "buyable": False, "sealed": True, "one_word": False, "plates": [f"P{i}" for i in range(7)]}],
        "plates": [{"plate": "P", "a_symbols": ["B"], "auction_mean_pct": .1,
            "amount_sum_yuan": 200_000_000, "market_amount_share": .2, "limit_up_count": 1,
            "yest_limit_streak_count": 1, "peers": [dict(base_peer, symbol=f"0000{i:02d}", amount_rank=i,
                type_flags=["LOW_POS"] if i < 14 else []) for i in range(1, 16)]}],
        "clusters": [{"plate": "P", "a_count": 1, "peer_count": 15, "low_pos_count": 12,
            "strong_rel_count": 1, "hollow_top_count": 0, "a_unbuyable_with_peers": True}],
        "reference_statistics": {"rows": [{"day_type": "弱势", "rule": "基线", "observations": 10,
            "peer_close_mean_pct": -1.75, "relative_market_pp": .01, "close_ge_5_pct": 2,
            "large_loss_pct": 3.7, "today_match": True}]}}
    md = render_plate_peer_markdown(view)
    assert "过滤A" not in md and "保留A" in md
    assert "等 2 个" in md
    assert "9.94%" in md and "1200.00万" in md
    assert "其余 3 只同伴见 JSON" in md
    assert md.index("板块聚类汇总") < md.index("A 所属板块与同伴")
    assert "是" in md.split("72日描述性参考", 1)[1]
