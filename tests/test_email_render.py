import json

from engine_core.email_render import render_email_html


def test_renderer_escapes_structured_report_and_marks_missing():
    report = {
        "trade_date": "2026-10-15",
        "conclusion": ["市场 <观察> ▲", "缺口字段"],
        "chapters": {"emerging": [{"name": "AI &芯片", "pct": 2.3, "amount_yuan": 123000000},
                                    {"name": "缺失", "pct": None}]},
        "data_gaps": ["日线覆盖 —"],
    }
    html, plain = render_email_html(report, "auction_0926", "2026-10-15")
    assert "&lt;观察&gt;" in html
    assert "AI &amp;芯片" in html
    assert "—" in html and "—" in plain
    lowered = html.lower()
    assert all(token not in lowered for token in ("<script", "http://", "https://", "@import", "url(", "src="))
    assert "<table" in html and "multipart" not in html
    assert len(html.encode("utf-8")) <= 120 * 1024


def test_stale_intraday_report_does_not_present_missing_coverage_as_zero():
    report = {"trade_date": "2026-10-15", "chapters": {
        "market_price_volume": {"status": "STALE", "symbol_count": None,
                                "priced_symbol_count": None, "market_match_amount_yuan": None}}}
    manifest = {"sources": {"td": {"coverage": {"symbols": 0, "priced_symbols": 0}}}}
    html, plain = render_email_html(report, "auction_0926", "2026-10-15", manifest=manifest)
    assert "覆盖未知" in html + plain
    assert "覆盖：0 / 0" not in html + plain
    assert "有价标的数</div><b" not in html


def test_renderer_emits_a_share_bar_and_inline_safe_email_layout():
    report = {"trade_date": "2026-10-15", "conclusion": ["一条事实"],
                  "chapters": {"plate_changes": [{"name": "通信", "amount_share_pct": 19.6,
                                                    "pct": 1.2},
                                                   {"name": "医药", "amount_share_pct": 7.1,
                                                    "pct": -0.8}]}}
    html, _ = render_email_html(report, "open_0946", "2026-10-15")
    assert 'width:19.6%' in html
    assert "max-width:680px" in html
    assert 'name="color-scheme" content="light dark"' in html
    assert "@media (prefers-color-scheme: dark)" in html
    assert "▲" in html and "#c83b3b" in html.lower()
    assert "▼" in html and "#16835d" in html.lower()


def test_share_fraction_is_rendered_as_percentage_and_empty_columns_are_hidden():
    report = {"stage": "close", "market": {"close_median": -1.0},
              "plates": {"auction_amount_top10": [
                  {"name": "通信", "auction_amount_share": 0.00066, "close_mean": 1.2},
                  {"name": "芯片", "auction_amount_share": 0.0012, "close_mean": -0.3}]},
              "data_quality": {"included_stock_count": 2}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    assert "0.1%" in html + plain
    assert "auction_amount_share" not in html

    empty = {"stage": "auction", "chapters": {"yesterday_highboards": [
        {"symbol": "600001", "name": "样例", "auction_amount_share": None}]}}
    html, plain = render_email_html(empty, "auction", "2026-10-15")
    assert "竞价额占比</th>" not in html
    assert "数据缺口" in html


def test_intraday_email_renders_market_board_changes_and_highboard_context():
    report = {"stage": "open_0946", "auction_terminal_0925": {"market_match_amount_yuan": 1_000_000_000,
              "top_stocks_by_amount": [{"symbol": "600001", "name": "甲", "match_amount_yuan": 6_600_000,
                                        "amount_share_pct": 0.66, "pct_change": 2.0}]},
              "chapters": {"plate_changes": {"up": [{"name": "通信", "mean_pct": 1.2}],
                                                "down": [{"name": "芯片", "mean_pct": -1.4}]},
                           "yesterday_highboards": [{"symbol": "600002", "name": "乙", "streak": 3,
                                                     "0925": 4.0, "0932": 5.0, "0940": 6.0, "0945": 7.0,
                                                     "amount_share_bp": 82.0}],
                           "look_a_do_b": []}}
    html, plain = render_email_html(report, "open", "2026-10-15")
    for expected in ("板块变化", "昨日高标", "看A做B", "无匹配样本", "0.66%", "09:45涨幅"):
        assert expected in html + plain


def test_close_email_separates_unlimited_listing_from_price_band_rank():
    report = {"stage": "close", "market": {},
              "big_meat": [{"symbol": "301569", "name": "新股样例", "limit_pct": 20,
                            "close_pct": 285.0, "is_new_listing": True}],
              "stocks": [{"symbol": "301569", "name": "新股样例", "listing_days": 1,
                          "limit_pct": None, "close_pct": 285.0}],
              "data_quality": {"included_stock_count": 1}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    assert "新股/无涨跌幅限制" in html + plain


def test_close_report_uses_scorecard_for_conclusion_points():
    report = {"trade_date": "2026-10-15", "a_to_b": [], "ladder_section": {"rows": []},
              "market": {"close_median": -2.0,
              "up_count": 900, "down_count": 4000}, "emerging_scorecard": {"status": "READY",
              "emerging": [{"name": "版权", "score": 0.7}]}, "data_quality": {"daily_source": "wencai"}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    assert "未提供结论" not in html + plain
    assert "收盘中位" in plain and "日线来源" in plain and "问财" in plain
    assert "A→B 章节未生成" not in html and "连板梯队章节未生成" not in html
    assert "未提供" not in html + plain


def test_missing_yesterday_review_is_explicit():
    html, plain = render_email_html({"stage": "close", "market": {}}, "close", "2026-10-15")
    assert "昨日机会回看" in html + plain
    assert "无昨日报告" in html + plain


def test_close_extreme_rows_show_full_context_and_half_limit_visibility():
    row = {"symbol": "600001", "name": "甲", "main_plate": "银行", "limit_pct": 10,
           "auction_pct": 1, "p0932_pct": 2, "p0945_pct": 3, "close_pct": 4,
           "high_pct": 5, "low_pct": -1, "delta_over_limit": 0.3,
           "auction_amount_yuan": 6_600_000, "auction_amount_bp": 6.6,
           "in_yest_pool": True, "lb_days": 2, "visibility_half_limit": "09:32前已达0.5L"}
    html, plain = render_email_html({"stage": "close", "market": {}, "big_meat": [row]},
                                   "close", "2026-10-15")
    for expected in ("所属板块", "涨跌停幅度", "竞价涨幅", "09:32涨幅", "09:45涨幅",
                     "日内最高", "日内最低", "昨日涨停池", "连板数", "0.5L可见时点", "6.6 bp", "是"):
        assert expected in html + plain


def test_intraday_without_plate_share_source_reports_gap():
    report = {"stage": "auction", "auction_terminal_0925": {"top_plates_by_share": []},
              "chapters": {"plate_linkage": {"available": False, "rows": []}}}
    html, plain = render_email_html(report, "auction", "2026-10-15")
    assert "板块份额横条" in html + plain
    assert "个股—板块联动样本" in html + plain


def test_renderer_never_exposes_unmapped_fields_or_placeholder_metadata():
    report = {
        "stage": "open_0946",
        "asof": "09:46:00",
        "conclusion": ["market.close_median=-2.1", "上涨家数 10 家、下跌家数 20 家。"],
        "chapters": {"yesterday_highboards": [{"symbol": "000001", "name": "示例",
          "delta_over_limit": -0.2, "auction_amount_yuan": 12000000,
          "opaque_internal_value": 123, "amount_yuan": 10000000}]},
        "limitations": [],
        "auction_terminal_0925": {"data_lag_seconds": 3.0, "priced_symbol_count": 9,
                                   "symbol_count": 10, "sealed_up_count": 1, "sealed_down_count": 2},
    }
    manifest = {"asof": "09:46:00", "sources": {"td": {"data_lag_seconds": 3.0,
               "coverage": {"symbols": 10, "priced_symbols": 9}}}}
    rendered, plain = render_email_html(report, "open_0946", "2026-10-15", manifest)
    both = rendered + plain
    assert "market." not in both and "_pct" not in both and "yuan" not in both
    assert "未提供" not in both
    assert "未知" not in both
    assert "上涨家数" in both and "收盘变化÷板幅" in both and "竞价额" in both
    assert "opaque_internal_value" not in both
    assert "▲ 1" not in both and "▼ 2" not in both


def test_renderer_translates_status_tokens_in_gap_copy():
    report = {"data_gaps": ["关系快照 PIT；09:45 标记 NOT_READY/STALE；lag 4 秒"]}
    html, plain = render_email_html(report, "open", "2026-10-15")
    both = html + plain
    assert "点时" in both and "未就绪/数据滞后" in both and "数据滞后 4 秒" in both
    assert "PIT" not in both and "NOT_READY" not in both and "STALE" not in both
    assert "单日描述" in both


def test_close_email_has_descriptive_scorecard_and_chinese_headers():
    report = {"market": {"amount_yuan": 1600000000000, "amount_vs_prev": 1.15,
              "close_median": -3.0, "up_count": 400, "down_count": 5000,
              "close_limit_up_count": 38, "close_limit_down_count": 29},
        "email_editorial": {"单日描述": True, "结论": ["单日描述，不推断全天路径。"],
          "SCORECARD": [{"cohort": "严格新冒头", "k": 26, "matched": "16 / 10 / 1",
                         "rate": "61.54% / 38.46% / 3.85%"}],
          "高标收盘状态": [{"symbol": "000530", "name": "冰山冷热", "prior_streak": 2,
                           "sample_0945_pct": 8.89, "close_result_pct": 10.0}]}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    assert "收盘核心对照" in html + plain
    assert "严格新冒头" in html + plain and "高标收盘状态" in html + plain
    assert "单日描述" in html + plain
    assert "market." not in html + plain and "▲ 38" not in html + plain and "▼ 29" not in html + plain


def test_numeric_format_uses_explicit_semantics_for_ratios_percentages_and_changes():
    from engine_core.email_render import _num
    assert _num(0.125, "amount_64d_percentile") == "12.5%"
    assert _num(17 / 28, "close_ge_5_rate") == "60.7%"
    assert _num(1.0, "anchor_coverage") == "100.0%"
    assert _num(5215, "p0945_coverage") == "5,215"
    assert _num(5215, "close_pct_coverage") == "5,215"
    assert _num(0.25, "auction_amount_share") == "25.0%"
    assert _num(60.7, "p0925_range_position20_pct") == "60.7%"
    assert _num(0.125, "close_pct") == "+0.12%"
    assert _num(0.125, "relative_to_plate_pp") == "+0.12 pp"


def test_open_email_strips_close_fields_and_long_tables_are_collapsed():
    report = {"stage": "open_0946", "chapters": {"yesterday_highboards": [
        {"symbol": f"600{i:03d}", "name": f"股{i}", "p0945_pct": i, "close_pct": 99}
        for i in range(12)]}}
    rendered, plain = render_email_html(report, "open", "2026-10-15")
    assert "收盘涨幅" not in rendered + plain and "99.00%" not in rendered + plain
    assert "前 5 / 共 12 行" in rendered
    assert "<details>" in rendered and "其余 7 行" in rendered
    assert len(rendered.encode("utf-8")) <= 120 * 1024


def test_close_email_marks_proxy_and_overlap_without_summing():
    report = {"market": {"close_limit_up_count": 2, "close_limit_down_count": 1},
              "data_quality": {"close_pct_coverage": 5215, "final_count": 4449, "indicative_count": 766},
              "plates": {"auction_amount_top10": [{"name": "芯片", "auction_amount_share": 2.1}]}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    both = html + plain
    assert "收盘涨停价代理 / 跌停价代理" in both
    assert "成员重叠, 不可相加" in both or "成员重叠，不可相加" in both
    assert "INDICATIVE 766 只" in both and "FINAL 4,449 只" in both
    assert "收盘对账价覆盖 5,215 只" in both and "首次达到时刻 UNKNOWN" in both


def test_common_pool_and_all_market_amount_semantics_are_explicit():
    from engine_core.email_render import _metrics
    metrics = _metrics({"market": {"amount_vs_prev_common": 1.154, "amount_common_pool_n": 5191,
                                   "amount_vs_prev": 1.18, "amount_vs_64d_median_common": 0.793,
                                   "amount_64d_common_pool_n": 5000}}, "close")
    rendered = " ".join(label for label, _, _ in metrics)
    assert "固定共同池 5,191 只" in rendered
    assert "全市场口径注脚" in rendered
    assert "64日中位数（固定共同池 5,000 只）" in rendered


def test_generic_and_event_plate_appendix_is_collapsed_inside_plate_section():
    from engine_core.email_render import render_email_html

    html, _ = render_email_html({"stage": "close", "market": {}, "plates": {
        "auction_amount_top10": [{"name": "芯片", "auction_amount_share": 0.2}],
        "deferred_generic_event": [{"name": "业绩增长", "kind": "EVENT", "members": 8}],
    }}, "close", "2026-01-01")
    assert "竞价额前十（成员重叠，不可相加）" in html
    assert "<details><summary>泛化/事件板块附录（折叠，不参与主榜）</summary>" in html
    assert "业绩增长（EVENT，8只成员）" in html


def test_close_email_separates_auction_and_daily_amount_baselines_and_reports_missing_sections():
    report = {"stage": "close", "limitations": [], "data_gaps": ["其他缺口"],
              "market": {"auction_amount_yuan": 20_205_000_000,
                         "auction_baseline_comparison": {"median_yi": 169, "multiple": 1.196,
                                                          "percentile": 67.19, "sample_days": 64},
                         "amount_64d_median_yuan": 20_687_252_865,
                         "amount_vs_64d_median_common": 0.79,
                         "amount_64d_common_pool_n": 5191}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    both = html + plain
    assert "竞价额历史基线（竞价池）" in both
    assert "全天成交额历史基线（共同池）" in both
    daily_start = html.index("全天成交额历史基线（共同池）")
    daily_end = html.find("</table>", daily_start)
    daily_baseline_table = html[daily_start:daily_end]
    assert "0.79×" in daily_baseline_table and "5,191" in daily_baseline_table
    assert "1.20×" in both and "67.19%" in both
    assert "0.79×" in both and "5,191" in both
    assert "其他缺口" in both
    assert "A→B 章节未生成/原因" in both and "连板梯队章节未生成/原因" in both


def test_sealed_up_label_is_price_proxy_everywhere():
    report = {"stage": "close", "market": {},
              "chapters": {"emerging": [{"name": "样例", "sealed_up": 1}]}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    assert "收盘价=涨停价(价格代理)" in html + plain
    assert "涨停：1" not in html + plain



def test_email_starts_with_manifest_quality_and_keeps_unknowns_unknown():
    report = {
        "stage": "auction",
        "auction_terminal_0925": {"vs_amount_baseline": 25.0},
        "chapters": {"market_price_volume": {"symbol_count": None}},
    }
    manifest = {
        "trade_date": "2026-10-16", "elapsed_seconds": 3.2,
        "degraded": ["Q2_REDIS_UNAVAILABLE"],
        "sources": {
            "auction_baseline": {"status": "PARTIAL", "coverage": "15/64"},
            "plate_members": {"status": "MISSING", "reason": "incomplete"},
            "hot_rank": {"status": "DEGRADED", "reason": "stale"},
            "q2_redis": {"status": "PARTIAL", "coverage": "12/20"},
            "td": {"status": "PARTIAL", "coverage": {"priced_symbols": 3, "symbols": 10}},
        },
    }
    html, plain = render_email_html(report, "auction", "2026-10-16", manifest)
    assert "[PARTIAL]" in plain and "[PARTIAL]" in html
    assert "竞价基线：PARTIAL 15/64" in plain
    assert "板块成员：MISSING" in plain
    assert "热榜：DEGRADED" in plain
    assert "Q2：PARTIAL 12/20" in plain
    assert "耗时：3.2s" in plain and "used_date：UNKNOWN（报告交易日 2026-10-16）" in plain
    assert "疑似异常" in plain and "25" in plain and "缺失率 70.0%" in plain
    assert "None" not in plain and "—" in plain


def test_email_quality_uses_slice_meta_and_reports_missing_provenance_reason():
    report = {"stage": "auction", "manifest": {"inputs": {}}}
    manifest = {
        "trade_date": "2026-10-16", "elapsed_seconds": 2.0,
        "used_dates": {"daily_history": ["2026-10-15"], "plate_members": ["2026-10-16"]},
        "slices": {
            "daily_history": {"source": "core_data", "status": "READY", "rows": 5226,
                              "version": "sha256-daily", "used_date": ["2026-10-15"]},
            "plate_members": {"source": "core_data", "status": "READY", "rows": 88646,
                              "version": "sha256-plates", "used_date": ["2026-10-16"]},
            "plate_registry": {"source": "core_data", "status": "READY", "rows": 709,
                               "version": "sha256-registry", "used_date": ["2026-10-13"]},
            "auction_baseline": {"source": "import_from_validation", "status": "PARTIAL", "rows": 65,
                                 "version": "v2-import", "coverage": {"sample_days": 15, "expected_days": 64}},
            "hot_rank": {"source": "ths_hot_rank", "status": "MISSING", "rows": 0,
                         "reason": "NO_DATA_FOR_DATE", "version": None},
            "unavailable_slice": {"source": None, "status": None, "reason": "catalog entry absent"},
        },
    }
    _, plain = render_email_html(report, "auction", "2026-10-16", manifest)
    assert "日K/指标：READY 5226行" in plain
    assert "板块成员：READY 88646行；709板块" in plain
    assert "竞价基线：PARTIAL 样本 15/64 日" in plain
    assert "65行" in plain and "v2-import" in plain and "import_from_validation" in plain
    assert "热榜：MISSING 0行（NO_DATA_FOR_DATE）" in plain
    assert "unavailable_slice：UNKNOWN（catalog entry absent）" in plain
    assert "用 D-1 数据" in plain


def test_four_preview_contexts_reconcile_disabled_inputs_and_baseline_coverage():
    cases = [("2026-10-15", "auction", 65), ("2026-10-15", "open", 65),
             ("2026-10-16", "auction", 15), ("2026-10-16", "open", 15)]
    for trade_date, stage, sample_days in cases:
        report = {"stage": stage, "manifest": {"inputs": {
            "auction_baseline": {"provided": True, "sample_days": sample_days},
            "optional_feed": {"provided": False}}},
            "chapters": {"market_price_volume": {"auction_baseline_comparison": {
                "sample_days": sample_days}}}}
        manifest = {"trade_date": trade_date, "slices": {
            "auction_baseline": {"status": "PARTIAL", "coverage": {"sample_days": sample_days,
                                                                          "expected_days": 64}}}}
        _, plain = render_email_html(report, stage, trade_date, manifest)
        assert f"竞价基线：PARTIAL 样本 {sample_days}/64 日" in plain
        assert "optional_feed：NOT_ENABLED（未启用）" in plain
        assert "optional_feed：UNKNOWN" not in plain
