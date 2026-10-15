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
    assert "收盘中位" in plain and "数据来源" in plain
    assert "数据缺口" in html

