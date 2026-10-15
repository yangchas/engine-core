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


def test_close_report_uses_scorecard_for_conclusion_points():
    report = {"trade_date": "2026-10-15", "market": {"close_median": -2.0,
              "up_count": 900, "down_count": 4000}, "emerging_scorecard": {"status": "READY",
              "emerging": [{"name": "版权", "score": 0.7}]}, "data_quality": {"daily_source": "wencai"}}
    html, plain = render_email_html(report, "close", "2026-10-15")
    assert "未提供结论" not in html + plain
    assert "收盘中位" in plain and "日线来源" in plain and "问财" in plain
    assert "数据缺口" not in html
    assert "未提供" not in html + plain


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
    assert "上涨家数" in both and "收盘变化/涨跌停幅度" in both and "竞价额" in both
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

