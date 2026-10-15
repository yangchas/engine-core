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


def test_share_fraction_is_rendered_as_basis_points_and_empty_columns_are_hidden():
    report = {"stage": "close", "market": {"close_median": -1.0},
              "plates": {"auction_amount_top10": [
                  {"name": "通信", "auction_amount_share": 0.00066, "close_mean": 1.2},
                  {"name": "芯片", "auction_amount_share": 0.0012, "close_mean": -0.3}]},
              "data_quality": {"included_stock_count": 2}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    assert "6.6 bp" in html + plain
    assert "12.0 bp" in html + plain
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
    report = {"trade_date": "2026-10-15", "market": {"close_median": -2.0,
              "up_count": 900, "down_count": 4000}, "emerging_scorecard": {"status": "READY",
              "emerging": [{"name": "版权", "score": 0.7}]}, "data_quality": {"daily_source": "wencai"}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    assert "未提供结论" not in html + plain
    assert "收盘中位" in plain and "日线来源" in plain and "问财" in plain
    assert "数据缺口" not in html
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

