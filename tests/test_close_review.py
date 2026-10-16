from engine_core.close_review import derive_close_review, render_close_markdown


def test_default_close_facts_filter_historical_rows_from_today():
    from engine_core.close_review import _rows_for_trade_date
    rows = [
        {"date": "2026-10-14", "symbol": "600001", "amount_yuan": 100},
        {"date": "2026-10-15", "symbol": "600001", "amount_yuan": 200},
    ]
    assert [row["amount_yuan"] for row in _rows_for_trade_date(rows, "2026-10-15")] == [200]


def test_close_review_defaults_to_slice_data_and_never_validation_root(tmp_path, monkeypatch):
    from engine_core.close_review import build_parser, main
    import json

    monkeypatch.delenv("CORE_ALLOW_FILE_FALLBACK", raising=False)
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path / "core-data"))
    assert build_parser().get_default("reference_root") is None
    out = tmp_path / "close"
    assert main(["--trade-date", "2026-10-16", "--out-dir", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["legacy_fallback"] is False
    assert set(manifest["slices"]) == {"daily_history", "auction_baseline"}


def test_close_review_rejects_explicit_files_without_fallback_opt_in(tmp_path, monkeypatch):
    from engine_core.close_review import main
    import pytest

    monkeypatch.delenv("CORE_ALLOW_FILE_FALLBACK", raising=False)
    with pytest.raises(ValueError, match="CORE_ALLOW_FILE_FALLBACK=1"):
        main(["--trade-date", "2026-10-16", "--stock-day-facts", str(tmp_path / "facts.json"),
              "--out-dir", str(tmp_path / "close")])


def test_close_review_facts_file_fallback_is_marked_in_manifest(tmp_path, monkeypatch):
    from engine_core import stock_day_facts
    from engine_core.close_review import main
    import json

    monkeypatch.setenv("CORE_ALLOW_FILE_FALLBACK", "1")
    monkeypatch.setattr(stock_day_facts, "read_facts", lambda _path: ([], {"sha256": "fixture"}))
    out = tmp_path / "close-facts"
    assert main(["--trade-date", "2026-10-16", "--stock-day-facts", str(tmp_path / "facts.json"),
                 "--out-dir", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["quality"] == "FILE_FALLBACK"
    assert manifest["file_fallback"] == "FILE_FALLBACK"


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
            {"symbol": "600001", "auction_pct": 0, "p0945_pct": 5, "close_pct": 10, "limit_pct": 10, "close": 11.0, "high": 11.0, "low": 10.0, "pre_close": 10.0, "auction_amount_yuan": 200, "p0945_cumulative_amount_yuan": 50},
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


def test_evolution_uses_qualified_main_plate_filters_unbuyable_peers_and_labels_range():
    rows = [
        {"symbol": "600001", "name": "A", "delta_pp": 8, "close_pct": 8,
         "p0925_pct": 0, "p0932_pct": 1, "p0945_pct": 2, "auction_amount_yuan": 1,
         "plates": ["物流", "芯片"], "p0925_range_position20_pct": 148},
        {"symbol": "600002", "name": "B", "p0925_pct": 1, "p0932_pct": 1,
         "p0945_pct": 1, "close_pct": 2, "auction_amount_yuan": 100,
         "plates": ["物流", "芯片"], "p0925_range_position20_pct": 148},
        {"symbol": "600003", "name": "C", "p0925_pct": 9.9, "limit_pct": 10,
         "p0932_pct": 9, "p0945_pct": 8, "close_pct": 8, "auction_amount_yuan": 999,
         "plates": ["芯片"]},
        {"symbol": "600004", "name": "D", "p0925_pct": 1, "p0932_pct": 1,
         "p0945_pct": 1, "close_pct": 2, "auction_amount_yuan": 50,
         "plates": ["芯片"], "p0925_range_position20_pct": -14},
    ]
    plate_meta = [{"name": "物流", "code": "801019", "kind": "BROAD", "members": 297},
                  {"name": "芯片", "code": "801001", "kind": "THEME", "members": 40}]
    report = derive_close_review(rows, trade_date="2026-01-01", plates=plate_meta)
    a = next(r for r in report["plate_evolution"] if r["symbol"] == "600001")
    assert a["plate"] == "芯片"
    assert "600002" in a["peer_top3"] and "600003" not in a["peer_top3"]
    assert "高于前20日区间" in a["peer_top3"]
    assert "低于前20日区间" in a["peer_top3"]
    assert "148%" not in a["peer_top3"] and "-14%" not in a["peer_top3"]


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
    assert result["history_percentile"] == 1.0


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


def test_close_markdown_is_name_only_and_missing_name_is_explicit():
    from engine_core.close_review import render_close_markdown
    import re

    data = {"trade_date": "2026-10-15", "market": {"close_median": 0},
            "big_meat": [{"symbol": "000833", "name": "粤桂股份", "auction_pct": 1,
                          "p0932_pct": 2, "p0945_pct": 3, "close_pct": 4,
                          "delta_over_limit": .3, "auction_amount_yuan": 100,
                          "visibility_half_limit": "未知"}],
            "big_loss": [], "missed_opportunities": {"status": "READY", "count": 1,
                                                         "symbols": ["000833"], "plate_counts": {}},
            "yesterday_review": {"status": "UNAVAILABLE_NO_PREV_REPORT"},
            "emerging_scorecard": {}, "plates": {"close_top10": [], "close_bottom10": [],
                                                       "auction_amount_top10": []},
            "checkpoint_chapters": {"timeline": {"missing": []}}, "data_quality": {
                "close_pct_coverage": 1, "included_stock_count": 1, "auction_pct_coverage": 1,
                "p0932_coverage": 1, "p0945_coverage": 1, "final_count": 0,
                "indicative_count": 0, "daily_source": "test"}}
    rendered = render_close_markdown(data)
    assert "粤桂股份" in rendered and "名称未知" in rendered
    assert re.search(r"(?<!\d)\d{6}(?!\d)", rendered) is None


def test_close_section_attachment_writes_a_to_b_and_ladder_into_report():
    from engine_core.close_review import attach_close_sections

    report = {"trade_date": "2026-10-15", "stocks": [
        {"symbol": "600001", "name": "昨日高标", "lb_days": 2, "prior_limit_streak": 0, "main_plate": "芯片",
         "auction_amount_yuan": 100},
        {"symbol": "600002", "name": "同伴", "main_plate": "芯片", "p0925_pct": 1,
         "auction_amount_yuan": 50},
    ]}
    attach_close_sections(report)
    assert len(report["a_to_b"]) == 1
    assert report["a_to_b"][0]["a_symbol"] == "600001"
    assert report["a_to_b_schema_version"] == 2
    assert report["a_to_b"][0]["schema_version"] == 2
    assert report["a_to_b"][0]["a_reasons"][0]["rule_id"] == "R1"
    assert report["a_to_b"][0]["plate_state"]["name"] == "芯片"
    assert report["ladder_section"]["rows"][0]["symbol"] == "600001"
    assert report["ladder_section"]["rows"][0]["prior_streak"] == 2
    assert report["ladder_section"]["rows"][0]["prior_streak_status"] == "字段冲突"


def test_plate_report_includes_narrow_theme_ranked_by_auction_share():
    rows = [{"symbol": f"600{i:03d}", "plates": ["小主题"], "p0925_pct": i / 10,
             "p0945_pct": i / 10, "close_pct": i / 10, "auction_amount_yuan": 100}
            for i in range(8)]
    report = derive_close_review(rows, trade_date="2026-01-01",
                                 plates=[{"name": "小主题", "kind": "THEME"}])
    assert report["plates"]["narrow_theme_top5"][0]["name"] == "小主题"
    assert report["plates"]["narrow_theme_top5"][0]["members"] == 8


def test_close_extremes_uses_fixed_auction_cohort_and_limits_old_appendix_to_five():
    from engine_core.close_review import derive_close_review, render_close_markdown

    rows = [{"symbol": f"600{i:03d}", "name": f"股票{i}", "auction_pct": i - 6,
             "auction_amount_yuan": 40_000_000 + i, "auction_price_status": "FINAL",
             "limit_pct": 10, "p0932_pct": i, "p0945_pct": i - 1,
             "close_pct": i - 2, "high_pct": i + 1, "low_pct": i - 3}
            for i in range(30)]
    report = derive_close_review(rows, trade_date="2026-10-15", extremes_k_abs=3,
                                 extremes_k_amount=3, extremes_min_auction_amount_yuan=0)
    board = report["extremes_board"]["close"]
    assert len(board["rows"]) <= 6
    assert all(row["symbol"] in {r["symbol"] for r in report["extremes_board"]["auction"]["rows"]}
               for row in board["rebound_top8"] + board["weak_top8"])
    markdown = render_close_markdown(report)
    assert "竞价先选、后追踪极值（当日描述）" in markdown
    assert "旧口径附录：全市场收盘后极值（事后筛选，不是可见机会；Top5）" in markdown
    assert markdown.count("### 大肉") == 1 and markdown.count("### 大面") == 1
