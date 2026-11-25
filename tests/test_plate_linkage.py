import pytest

from engine_core.plate_linkage import build_plate_linkage


def test_plate_linkage_hand_calculation_and_missing_values():
    states = {
        "a": {"symbol": "a", "pct_change": 10.0, "match_amount_yuan": 60, "limit_state": 1},
        "b": {"symbol": "b", "pct_change": -2.0, "match_amount_yuan": 30, "limit_state": -1},
        "c": {"symbol": "c", "pct_change": 0.0, "match_amount_yuan": 10, "limit_state": 0},
        "d": {"symbol": "d", "pct_change": None, "match_amount_yuan": 100, "limit_state": 0},
        "e": {"symbol": "e", "pct_change": 4.0, "match_amount_yuan": None, "limit_state": 0},
        **{f"x{i}": {"symbol": f"x{i}", "pct_change": 0.0, "match_amount_yuan": 0,
                     "limit_state": 0} for i in range(3)},
    }
    result = build_plate_linkage(states, {"p": set(states)}, {"p": "主题板块"},
                                 stock_names={"a": "上涨封板", "b": "下跌封板"}, min_members=7)
    row = result["rows"][0]
    # (a) positive among priced rows; median is across finite member returns.
    assert row["up_share_pct"] == pytest.approx(200 / 7)
    assert row["equal_weight_median_pct"] == pytest.approx(0.0)
    # (b) weighted return uses only rows with both known amount and return.
    assert row["amount_weighted_pct"] == pytest.approx(5.4)
    assert row["weighted_minus_equal_mean_pp"] == pytest.approx(5.4 - 12 / 7)
    # (c)/(d) sealed count and sealed amount ratios use known amounts only.
    assert row["limit_up_count"] == 1 and row["limit_up_amount_share_pct"] == pytest.approx(30.0)
    assert row["limit_up_top3"][0]["name"] == "上涨封板"
    assert row["limit_down_count"] == 1 and row["limit_down_amount_share_pct"] == pytest.approx(15.0)
    # (e) Incomplete amount ranking makes top-three contribution unavailable.
    assert row["top3_leading_contribution_share_pct"] is None
    assert row["member_count"] == 8
    assert row["priced_member_count"] == 7 and row["amount_member_count"] == 7
    assert row["weighted_member_count"] == 6

    complete = {symbol: dict(value) for symbol, value in states.items()}
    complete["d"]["pct_change"] = 1.0
    complete["e"]["match_amount_yuan"] = 50
    complete_row = build_plate_linkage(complete, {"p": set(complete)}, {"p": "主题板块"},
                                       min_members=8)["rows"][0]
    assert complete_row["top3_leading_contribution_share_pct"] == pytest.approx(900 / 840 * 100)


def test_plate_linkage_excludes_generic_and_keeps_unavailable_distinct():
    states = {f"s{i}": {"symbol": f"s{i}", "pct_change": 1.0, "match_amount_yuan": None}
              for i in range(8)}
    result = build_plate_linkage(states,
                                 {"generic": set(states), "topic": set(states)},
                                 {"generic": "全体关系", "topic": "主题"},
                                 generic_names={"全体关系"}, min_members=8)
    assert [row["name"] for row in result["rows"]] == ["主题"]
    row = result["rows"][0]
    assert row["amount_yuan"] is None
    assert row["amount_weighted_pct"] is None
    assert row["limit_up_amount_share_pct"] is None
    assert result["down_limit_top5"] == []


def test_plate_linkage_keeps_missing_limit_state_unknown_in_report():
    from engine_core.auction_tracking import _at_limit

    unknown = {"symbol": "600001", "price_milli": None, "prev_close_milli": 10000,
               "rest_bid_shares": 5, "rest_ask_shares": 0, "pct_change": 1.0,
               "match_amount_yuan": 10}
    assert _at_limit(unknown, "up") is None
    states = {f"s{i}": {"symbol": f"s{i}", "pct_change": 1.0, "match_amount_yuan": 10,
                         "limit_state": 0} for i in range(7)}
    states["s7"] = {**unknown, "limit_state": None}
    report = build_plate_linkage(states, {"p": set(states)}, {"p": "主题"}, min_members=8)
    row = report["rows"][0]
    assert row["limit_up_count"] == 0
    assert row["limit_up_unknown_count"] == 1
    assert row["limit_up_unknown_coverage_pct"] == pytest.approx(12.5)
    assert row["limit_up_amount_share_pct"] is None


def test_primary_theme_clustering_does_not_change_legacy_plate_metrics():
    states = {f"s{i}": {"symbol": f"s{i}", "pct_change": i - 3.5,
                         "match_amount_yuan": 100 + i, "limit_state": 1 if i == 0 else 0}
              for i in range(8)}
    themes = {symbol: {"source": "reason", "primary_theme": {
        "id": f"theme-{int(symbol[1:]) // 4}", "name": f"主题{int(symbol[1:]) // 4}"}}
        for symbol in states}
    before = build_plate_linkage(states, {"original": set(states)}, {"original": "原板块"},
                                 min_members=8)
    after = build_plate_linkage(states, {"original": set(states)}, {"original": "原板块"},
                                theme_assignments=themes, min_members=8)
    assert after["rows"] == before["rows"]
    assert after["primary_theme_rows"] == []
