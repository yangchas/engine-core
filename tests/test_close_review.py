from engine_core.close_review import derive_close_review, render_close_markdown


def test_big_meat_loss_visibility_and_missed_opportunities():
    rows = [
        {"symbol": "600001", "name": "甲", "auction_pct": 0, "p0932_pct": 5.1, "p0945_pct": 4,
         "close_pct": 6, "delta_pp": 6, "auction_amount_yuan": 200, "plates": ["芯片"]},
        {"symbol": "600002", "name": "乙", "auction_pct": 0, "p0932_pct": 0, "p0945_pct": 0,
         "close_pct": -6, "delta_pp": -6, "auction_amount_yuan": 300, "plates": ["化工"]},
        {"symbol": "600003", "name": "丙", "auction_pct": 1, "p0932_pct": 0, "p0945_pct": 0,
         "close_pct": 7, "delta_pp": 6, "auction_amount_yuan": 100, "plates": ["化工"]},
    ]
    report = derive_close_review(rows, trade_date="2026-01-01", live_report={"chapters": {"emerging": [{"symbol": "600001"}]}})
    assert report["big_meat"][0]["visibility"] == "在已观测检查点达到0.5L"
    assert report["big_loss"][0]["visibility"] == "在已观测检查点未达到0.5L"
    assert report["missed_opportunities"]["symbols"] == ["600003"]
    assert report["missed_opportunities"]["plate_counts"] == {"化工": 1}


def test_emerging_close_scorecard_hand_calculation():
    rows = [
        {"symbol": "600001", "auction_pct": 0, "p0945_pct": 5, "close_pct": 10, "limit_pct": 10, "auction_amount_yuan": 200, "p0945_cumulative_amount_yuan": 50},
        {"symbol": "600002", "auction_pct": 0, "p0945_pct": 4.99, "close_pct": -1, "limit_pct": 10, "auction_amount_yuan": 100, "p0945_cumulative_amount_yuan": 60},
        {"symbol": "600003", "auction_pct": 0, "p0945_pct": 2, "close_pct": 6, "limit_pct": 10, "p0945_cumulative_amount_yuan": 70},
    ]
    report = derive_close_review(rows, trade_date="2026-01-01", live_report={"chapters": {"emerging": [{"symbol": "600001"}, {"symbol": "600002"}]}})
    score = report["emerging_scorecard"]
    assert {k: score["emerging"][k] for k in ("n", "close_ge_5", "sealed_up", "turned_negative")} == {"n": 1, "close_ge_5": 1, "sealed_up": 1, "turned_negative": 0}
    assert score["emerging"]["close_ge_5_rate"] == 1.0
    assert score["amount_top_k"]["symbols"] == ["600003"]


def test_missing_inputs_are_downgraded_not_errors():
    report = derive_close_review([], trade_date="2026-01-01")
    assert report["missed_opportunities"]["status"] == "UNAVAILABLE_NO_LIVE_REPORT"
    assert report["yesterday_review"]["status"] == "UNAVAILABLE_NO_PREV_REPORT"
    assert report["plates"]["status"] == "UNAVAILABLE_NO_RELATION"


def test_missing_required_checkpoint_degrades_visibility_and_dependent_conclusions():
    report = derive_close_review(
        [{"symbol": "600001", "auction_pct": 0, "p0932_pct": 0, "p0945_pct": None,
          "close_pct": 8, "delta_pp": 8}],
        trade_date="2026-10-15",
        live_report={"chapters": {"emerging": []}},
        checkpoint_status={"0932": "READY", "0935": "READY", "0940": "READY", "0945": "MISSING"})
    assert report["checkpoint_chapters"]["visibility"]["status"] == "DEGRADED"
    assert report["big_meat"][0]["visibility_half_limit"] == "不可判断：板幅或检查点缺失"
    assert report["emerging_scorecard"]["status"] == "DEGRADED"
    assert report["emerging_scorecard"]["p0945_top_k"] is None


def test_price_band_exception_is_separated_and_auction_share_has_market_denominator():
    report = derive_close_review([
        {"symbol": "301569", "name": "新股", "limit_pct": 20, "auction_pct": 280,
         "close_pct": 400, "high_pct": 500, "low_pct": 280, "auction_amount_yuan": 100},
        {"symbol": "600001", "name": "普通股", "limit_pct": 10, "auction_pct": 0,
         "close_pct": 2, "high_pct": 3, "low_pct": -1, "auction_amount_yuan": 900}],
        trade_date="2026-01-01")
    assert [row["symbol"] for row in report["unlimited_price_band_stocks"]] == ["301569"]
    assert all(row["symbol"] != "301569" for row in report["big_meat"] + report["big_loss"])
    assert report["stocks"][0]["auction_amount_share"] == 0.1
    assert report["stocks"][0]["auction_amount_bp"] == 1000.0
    assert report["stocks"][0]["visibility_half_limit"] == "不可判断：板幅或检查点缺失"


def test_close_review_uses_strict_0945_emerging_pool_and_symmetric_sampled_visibility():
    rows = [
        {"symbol": "600001", "auction_pct": 0, "p0932_pct": 1, "p0935_pct": 6,
         "p0940_pct": 1, "p0945_pct": 4.99, "close_pct": 8, "auction_amount_yuan": 100,
         "p0945_cumulative_amount_yuan": 30},
        {"symbol": "600002", "auction_pct": 0, "p0932_pct": -6, "p0935_pct": 0,
         "p0940_pct": 0, "p0945_pct": 0, "close_pct": -8, "auction_amount_yuan": 200,
         "p0945_cumulative_amount_yuan": 20},
        {"symbol": "600003", "auction_pct": 0, "p0932_pct": 0, "p0935_pct": 0,
         "p0940_pct": 0, "p0945_pct": 5, "close_pct": 8, "auction_amount_yuan": 300,
         "p0945_cumulative_amount_yuan": 40},
    ]
    live = {"chapters": {"emerging": [{"symbol": "600001"}, {"symbol": "600003"}]}}
    report = derive_close_review(rows, trade_date="2026-01-01", live_report=live)
    assert report["emerging_scorecard"]["emerging"]["n"] == 1
    assert report["emerging_scorecard"]["emerging"]["matched"] == 1
    assert next(r for r in report["big_meat"] if r["symbol"] == "600001")["visibility_half_limit"] == "0935首次已观测达到0.5L"
    assert next(r for r in report["big_loss"] if r["symbol"] == "600002")["visibility_half_limit"] == "0932首次已观测达到0.5L"
    assert report["emerging_scorecard"]["amount_top_k"]["symbols"] == ["600003"]


def test_plate_evolution_peers_are_selected_by_auction_amount_and_show_price_state():
    rows = [{"symbol": "600001", "auction_pct": 0, "p0925_pct": 0, "p0932_pct": 1,
             "p0945_pct": 2, "close_pct": 8, "delta_pp": 8, "limit_pct": 10, "auction_amount_yuan": 1, "plates": ["芯片"],
             "auction_price_status": "FINAL"},
            {"symbol": "600002", "auction_pct": 0, "p0925_pct": 1, "p0932_pct": 1,
             "p0945_pct": 1, "close_pct": 9, "auction_amount_yuan": 100, "plates": ["芯片"],
             "auction_price_status": "INDICATIVE", "prior_range_quintile": "Q2"},
            {"symbol": "600003", "auction_pct": 0, "p0925_pct": 2, "p0932_pct": 2,
             "p0945_pct": 2, "close_pct": 4, "auction_amount_yuan": 50, "plates": ["芯片"],
             "auction_price_status": "FINAL", "prior_range_quintile": "Q3"}]
    report = derive_close_review(rows, trade_date="2026-01-01")
    row = next(r for r in report["plate_evolution"] if r["symbol"] == "600001")
    assert row["peer_top3"].startswith("按竞价额选")
    assert "600002" in row["peer_top3"] and "UNKNOWN" in row["peer_top3"] and "0945" in row["peer_top3"]
    assert "INDICATIVE" in report["stocks"][1]["auction_price_status"]


def test_generic_and_event_plates_are_excluded_from_top_and_evolution():
    rows = [{"symbol": f"60000{i}", "auction_pct": 0, "p0925_pct": 0, "p0932_pct": 1,
             "p0945_pct": 2, "close_pct": 8 if i == 1 else 1, "delta_pp": 8 if i == 1 else 1,
             "limit_pct": 10, "auction_amount_yuan": 100 - i, "plates": ["业绩增长"]}
            for i in range(1, 9)]
    report = derive_close_review(rows, trade_date="2026-01-01", plates=[{"name": "业绩增长", "kind": "EVENT"}])
    assert report["plate_evolution"] == []
    assert report["plates"]["close_top10"] == []
    assert report["plates"]["deferred_generic_event"][0]["name"] == "业绩增长"


def test_market_amount_ratios_share_one_fixed_common_pool():
    from engine_core.close_review import _fixed_common_amounts

    result = _fixed_common_amounts(
        {"A": 150, "B": 500}, {"A": 100, "B": 400},
        [{"A": 80, "B": 200}, {"A": 120}],
    )
    assert result["symbols"] == {"A"}
    assert result["today"] == 150
    assert result["previous"] == 100
    assert result["today_vs_previous"] == 1.5
    assert result["history_median"] == 100
    assert result["today_vs_history_median"] == 1.5


def test_close_markdown_labels_limit_counts_as_price_proxies_and_discloses_overlap():
    data = {"trade_date": "2026-01-01", "market": {"close_median": 0, "up_count": 1,
            "down_count": 1, "flat_count": 0, "close_limit_up_count": 1, "close_limit_down_count": 2,
            "amount_yuan": 10, "amount_common_pool_n": 1, "amount_vs_prev_common": 1.1,
            "amount_64d_common_pool_n": 1, "amount_vs_64d_median_common": 0.9},
            "big_meat": [], "big_loss": [], "missed_opportunities": {"status": "UNAVAILABLE_NO_LIVE_REPORT"},
            "yesterday_review": {"status": "UNAVAILABLE_NO_PREV_REPORT"}, "emerging_scorecard": {},
            "plates": {"close_top10": [], "close_bottom10": [], "auction_amount_top10": [{"name": "芯片"}],
                       "top_bottom_overlap": 0}, "checkpoint_chapters": {"timeline": {"missing": []}},
            "data_quality": {"close_pct_coverage": 1, "included_stock_count": 1, "auction_pct_coverage": 1,
                             "p0932_coverage": 1, "p0945_coverage": 1, "final_count": 1,
                             "indicative_count": 0, "daily_source": "fixture"}}
    markdown = render_close_markdown(data)
    assert "涨停价代理/跌停价代理 1/2" in markdown
    assert "竞价额份额Top10（成员重叠，不可相加）" in markdown
