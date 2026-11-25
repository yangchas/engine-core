from engine_core.email_board import render_email_board
import pytest


def _cells(markup):
    from html.parser import HTMLParser

    class Cells(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_cell = False
            self.value = ""
            self.rows = []
            self.headers = []
            self.current_headers = []

        def handle_starttag(self, tag, attrs):
            if tag in {"td", "th"}:
                self.in_cell = tag
                self.value = ""
            elif tag == "tr":
                self.current_headers = []

        def handle_endtag(self, tag):
            if self.in_cell == tag:
                if tag == "th":
                    self.current_headers.append(self.value)
                else:
                    self.rows.append(self.value)
                self.in_cell = False
            if tag == "tr" and self.current_headers:
                self.headers.append(self.current_headers)
                self.current_headers = []

        def handle_data(self, data):
            if self.in_cell:
                self.value += data

    parser = Cells()
    parser.feed(markup)
    return parser.headers, parser.rows


def test_close_board_puts_temperature_ladder_and_extremes_first_and_deduplicates_columns():
    report = {
        "market": {"close_median": -1.2, "up_count": 1200, "down_count": 3000,
                   "close_limit_up_count": 20, "close_limit_down_count": 8},
        "ladder_section": {"rows": [
            {"symbol": "600001", "name": "甲股", "prior_streak": 2, "main_plate": "通信",
             "p0925_pct": 1.0, "p0945_pct": 4.0, "close_pct": 9.9,
             "auction_amount_yuan": 125000000, "limit_pct": 10, "high_pct": 10},
            {"symbol": "600003", "name": "丙股", "prior_streak": 2, "main_plate": "通信",
             "p0925_pct": 2.0, "p0945_pct": 5.0, "close_pct": 10.0, "auction_amount_yuan": 50000000}],
            "yesterday_board_counts": [{"board_height": "2板", "count": 3}],
            "summary": [{"board_height": "2板", "previous_n": 3, "promoted_n": 1}]},
        "big_meat": [{"symbol": "600001", "name": "甲股", "main_plate": "通信概念特别长名称",
                      "plates": ["通信", "芯片"], "auction_pct": 1.0, "p0925_pct": 1.0,
                      "open_pct": 1.0, "p0945_pct": 4.0, "close_pct": 9.9,
                      "delta_over_limit": 0.9, "auction_amount_yuan": 125000000}],
        "big_loss": [{"symbol": "600002", "name": "乙股", "main_plate": "医药",
                      "auction_pct": 2.0, "p0945_pct": -4.0, "close_pct": -8.0,
                      "auction_amount_yuan": 50000000}],
        "unlimited_price_band_stocks": [{"symbol": "301001", "name": "新股", "close_pct": 80}],
        "plates": {"auction_amount_top10": [{"name": "通信", "members": 33,
                    "auction_amount_yuan": 1200000000, "auction_amount_share_bp": 850,
                    "p0925_mean": 1.2, "p0932_mean": 1.8, "p0945_mean": 2.2,
                    "close_mean": 2.8, "trend": [1.2, 1.8, 2.2, 2.8]},
                    {"name": "芯片", "members": 80, "auction_amount_yuan": 800000000,
                    "auction_amount_share_bp": 650, "p0925_mean": -1.0, "p0932_mean": -1.2,
                    "p0945_mean": -1.8, "close_mean": -2.0, "trend": [-1, -1.2, -1.8, -2]}]},
        "a_to_b": [],
    }
    rendered, plain = render_email_board(report, "close", "2026-10-16")
    assert rendered.index("收盘复盘") < rendered.index("今日主线定调") < rendered.index("全天兑现与核按钮") < rendered.index("连板高标二维矩阵")
    assert 'data-matrix="ladder-2d"' in rendered
    assert "主线题材" in rendered and "通信" in rendered and "甲股" in rendered and "乙股" in rendered
    assert "收盘中位" in plain or "中位 -1.20%" in plain
    assert "数据完整度与异常" not in rendered
    assert "<details" not in rendered
    headers, cells = _cells(rendered)
    assert all(len(row) == len(set(row)) for row in headers)
    assert not any("\n" in cell or "[" in cell or "]" in cell for cell in cells)
    assert rendered.count("<table") <= 6
    assert len(rendered.encode("utf-8")) < 30_000


def test_intraday_board_hides_close_fields_and_shows_missing_blocks():
    report = {"market": {"auction_median": -0.2},
              "stocks": [{"symbol": "600001", "name": "甲股", "auction_pct": 3,
                          "p0925_pct": 3, "p0945_pct": 5, "close_pct": 8}],
              "big_meat": [{"symbol": "600001", "name": "甲股", "auction_pct": 3,
                            "p0925_pct": 3, "p0945_pct": 5, "close_pct": 8}],
              "ladder_section": {"rows": [{"symbol": "600001", "name": "甲股",
                            "prior_streak": 1, "p0925_pct": 3, "p0945_pct": 5,
                            "close_pct": 8}]}}
    rendered, plain = render_email_board(report, "open", "2026-10-16")
    assert "收盘" not in rendered + plain
    assert "数据缺口：未生成" in rendered
    assert "竞价" in rendered and "0945" in rendered


@pytest.mark.parametrize(("checkpoint", "clock"), [("0932", "09:32"), ("0938", "09:38")])
def test_actual_mail_body_header_matches_asof(checkpoint, clock):
    market = {"market_match_amount_yuan": 111_000_000_000,
              f"p{checkpoint}_median": 1.5, "median_pct_change": 1.5,
              "up_share_pct": 90, "symbol_count": 5000}
    report = {"checkpoint": checkpoint, "asof": f"2026-10-16T{clock}:10+08:00",
              "market": market, "chapters": {"market_price_volume": market}}
    rendered, plain = render_email_board(report, "open", "2026-10-16")
    assert f"{checkpoint}｜" in rendered and f"{checkpoint}｜" in plain
    assert "0945｜" not in rendered + plain
    assert "1110.0亿" in rendered and "1110.0亿" in plain


def test_a_to_b_v2_is_explanatory_name_only_and_stage_bounded():
    group = {"schema_version": 2, "a_name": "甲股", "a_symbol": "600001", "a_prior_streak": 2,
             "a_status": "昨日高标", "plate": "芯片", "reason": "无同伴", "candidates": [],
             "a_strength": {"auction_pct": 2, "auction_amount_yuan": 100000000,
                            "auction_amount_bp": 25, "auction_amount_rank": 3,
                            "price_track_pct": {"0925": 2, "0932": 3, "0945": 4, "close": 5},
                            "path_type": "FLAT_PUSH"},
             "a_reasons": [{"rule_id": "R1", "text": "昨日≥2板"}],
             "plate_state": {"name": "芯片", "member_count": 30, "class": "THEME",
                             "auction_amount_yuan": 500000000, "auction_amount_share_bp": 100,
                             "relation_asof": "2026-10-14"},
             "sentiment_state": {"label": "WEAK"}, "b_candidates": [], "candidate_sort": "竞价额降序"}
    auction_html, auction_text = render_email_board({"a_to_b": [group]}, "auction", "2026-10-15")
    assert "R1" in auction_text and "竞价额" in auction_text
    assert "0945" not in auction_text and "收盘" not in auction_text
    assert "600001" not in auction_html + auction_text
    open_html, open_text = render_email_board({"a_to_b": [group]}, "open", "2026-10-15")
    assert "0945" in open_text and "收盘" not in open_text


def test_a_to_b_v2_empty_candidate_reasons_are_explicit_and_stage_fields_do_not_leak():
    from engine_core.email_board import render_email_board

    reasons = ["无合格主题板块", "无同伴", "同伴全部买不到", "缺少竞价价/额",
               "情绪档不支持（描述性）"]
    entries = [{"schema_version": 2, "a_name": f"A{i}", "a_prior_streak": 2,
                "a_today_status": "触", "a_status_is_price_proxy": True,
                "reason": reason, "plate": None, "a_strength": {
                    "price_track_pct": {"0925": 1, "0932": 2, "0945": 3, "close": 4}},
                "plate_state": {}, "sentiment_state": {"label": "WEAK"},
                "a_reasons": [{"rule_id": "R1", "text": "昨日≥2板"}], "b_candidates": []}
               for i, reason in enumerate(reasons)]
    auction_html, auction_text = render_email_board({"a_to_b": entries}, "auction", "2026-10-15")
    assert all(label in auction_text for label in ("无合格板块", "无同伴", "同伴均买不到", "缺竞价数据",
                                                   "情绪档不支持"))
    assert "0932" not in auction_text and "0945" not in auction_text and "收盘" not in auction_text
    open_html, open_text = render_email_board({"a_to_b": entries}, "open", "2026-10-15")
    assert "0945" in open_text and "收盘" not in open_text
    assert "<details" not in open_html


def test_email_renderer_applies_requested_stage_filter_before_board_rendering():
    from engine_core.email_render import render_email_html

    report = {"stage": "close_review", "stocks": [], "a_to_b": [{
        "schema_version": 2, "a_name": "锚点", "a_prior_streak": 2,
        "a_strength": {"price_track_pct": {"0925": 1, "0932": 2, "0945": 3, "close": 4}},
        "a_reasons": [], "plate_state": {}, "sentiment_state": {"label": "MIXED"},
        "b_candidates": [], "reason": "无同伴"}]}
    _, auction = render_email_html(report, "auction", "2026-10-15")
    _, opening = render_email_html(report, "open", "2026-10-15")
    assert "0932" not in auction and "0945" not in auction and "收盘" not in auction
    assert "0945" in opening and "收盘" not in opening


def test_board_cells_are_single_line_short_and_email_limits_hold():
    report = {"market": {}, "ladder_section": {"rows": []},
              "big_meat": [{"symbol": "600001", "name": "很长的名字" * 10,
                            "main_plate": "超长板块" * 10, "auction_pct": 1,
                            "p0945_pct": 2, "close_pct": 3}],
              "big_loss": []}
    rendered, _ = render_email_board(report, "close", "2026-10-16")
    assert len(rendered.encode("utf-8")) < 30 * 1024
    assert rendered.count("<table") <= 6
    assert "<details" not in rendered


def test_ladder_descends_by_board_count_and_extremes_are_capped_at_eight_rows():
    ladder = {"rows": [
        {"symbol": f"60000{i}", "name": f"股{i}", "prior_streak": level - 1,
         "limit_pct": 10, "close_pct": 10 if i == 1 else 0, "auction_amount_yuan": i * 100}
        for i, level in enumerate((2, 5, 3, 4, 1), 1)],
        "summary": [{"board_height": "4板", "previous_n": 2, "promoted_n": 1},
                    {"board_height": "2板", "previous_n": 4, "promoted_n": 2}]}
    report = {"ladder_section": ladder,
              "big_meat": [{"symbol": f"600{i:03}", "name": f"股{i}", "main_plate": "通信",
                            "auction_pct": 1, "p0945_pct": 2, "close_pct": 3}
                           for i in range(12)],
              "big_loss": []}
    rendered, _ = render_email_board(report, "close", "2026-10-16")
    ladder_text = rendered[rendered.index("连板高标二维矩阵"):]
    assert 'data-matrix="ladder-2d"' in ladder_text
    assert ladder_text.index("更高(昨≥4板)") < ladder_text.index("4板(昨3板)") < ladder_text.index("3板(昨2板)") < ladder_text.index("2板(昨1板)")
    _, cells = _cells(rendered)
    assert sum(1 for cell in cells if cell.startswith("股") and "→" not in cell and "%" not in cell) <= 5
    assert "股11" not in rendered


def test_v6_shaped_close_report_renders_real_ladder_a_to_b_and_yesterday_performance():
    report = {
        "trade_date": "2026-10-15",
        "market": {"close_median": -3.0, "amount_yuan": 1.2e12, "up_count": 1000, "down_count": 4000,
                   "close_limit_up_count": 40, "close_limit_down_count": 20},
        "stocks": [
            {"symbol": "000530", "name": "冰山冷热", "in_yest_pool": True, "lb_days": 2,
             "limit_pct": 10, "auction_pct": 9.84, "p0925_pct": 9.84, "close_pct": 10,
             "high_pct": 10, "auction_amount_yuan": 11716600},
            {"symbol": "000668", "name": "荣丰控股", "in_yest_pool": True, "lb_days": 2,
             "limit_pct": 10, "auction_pct": -2, "p0925_pct": -2, "close_pct": -9.99,
             "high_pct": -1, "auction_amount_yuan": 5252500},
        ],
        "ladder_section": {"rows": [
            {"symbol": "000530", "name": "冰山冷热", "prior_streak": 2, "close_pct": 10, "main_plate": "冷链"},
            {"symbol": "000668", "name": "荣丰控股", "prior_streak": 2, "close_pct": -9.99, "main_plate": "地产"},
        ], "summary": [], "yesterday_board_counts": [{"board_height": "2板", "count": 2}]},
    }
    rendered, plain = render_email_board(report, "close", "2026-10-15")
    assert 'data-matrix="ladder-2d"' in rendered
    assert "冰山冷热" in rendered and "+10.00%" in rendered and "冷链" in rendered
    assert "荣丰控股" in rendered and "-9.99%" in rendered and "地产" in rendered
    assert "1/2" in rendered and "50%" in rendered
    assert not any("—" * 3 in line for line in (rendered + "\n" + plain).splitlines())
    assert "—板" not in rendered and "—→—" not in rendered and "—亿" not in rendered

import re

def test_email_board_never_displays_codes_and_uses_unknown_name():
    from engine_core.email_board import render_email_board

    html, plain = render_email_board({
        "market": {},
        "stocks": [{"symbol": "000833", "name": "粤桂股份", "p0925_pct": 1,
                    "p0945_pct": 2, "close_pct": 3, "limit_pct": 10,
                    "auction_amount_yuan": 10}],
        "big_meat": [{"symbol": "000833", "name": "粤桂股份", "close_pct": 3}],
        "big_loss": [], "a_to_b": [], "data_gaps": []}, "close", "2026-10-15")
    assert "粤桂股份" in html and "粤桂股份" in plain
    visible_html = re.sub(r"<[^>]*>", " ", html)
    assert re.search(r"(?<!\d)\d{6}(?!\d)", visible_html + plain) is None

    html, plain = render_email_board({"market": {},
        "stocks": [{"symbol": "600001", "name": None, "p0925_pct": 1}],
        "big_meat": [{"symbol": "600001", "name": None}], "big_loss": [],
        "a_to_b": [], "data_gaps": []}, "close", "2026-10-15")
    assert "名称未知" in html + plain
    assert "600001" not in re.sub(r"<[^>]*>", " ", html) + plain


def test_auction_extremes_table_is_name_only_and_open_stage_tracks_fixed_union():
    from engine_core.extremes_board import build_extremes_board
    from engine_core.email_board import render_email_board

    stocks = [
        {"symbol": "600001", "name": "甲股", "main_plate": "通信", "auction_pct": 8,
         "auction_amount_yuan": 30_000_000, "auction_price_status": "FINAL", "limit_pct": 10,
         "p0932_pct": 5, "p0945_pct": 4, "latest_pct": 3, "close_pct": 2},
        {"symbol": "600002", "name": "乙股", "main_plate": "芯片", "auction_pct": -6,
         "auction_amount_yuan": 40_000_000, "auction_price_status": "INDICATIVE", "limit_pct": 10,
         "p0932_pct": -3, "p0945_pct": -1, "latest_pct": 1, "close_pct": 2},
    ]
    board = build_extremes_board(stocks, stage="open", k_abs=1, k_amount=1,
                                 min_auction_amount_yuan=0)
    html, plain = render_email_board({"market": {}, "stocks": stocks,
        "chapters": {"extremes_board": board}, "big_meat": [], "big_loss": [], "a_to_b": [],
        "data_gaps": []}, "open", "2026-10-15")
    visible = re.sub(r"<[^>]*>", " ", html) + plain
    assert "甲股" in visible
    assert "600001" not in visible and "600002" not in visible
    auction_html, auction_plain = render_email_board({"market": {}, "stocks": stocks,
        "extremes_board": board, "big_meat": [], "big_loss": [], "a_to_b": [],
        "data_gaps": []}, "auction", "2026-10-15")
    auction_visible = re.sub(r"<[^>]*>", " ", auction_html) + auction_plain
    assert "0932" not in auction_visible and "0945" not in auction_visible


def test_extremes_table_explains_rank_columns_and_marks_missing_plate():
    from engine_core.email_board import _extremes
    from engine_core.extremes_board import build_extremes_board
    board = build_extremes_board([{"symbol": "600001", "name": "甲", "auction_pct": 2,
                                   "auction_amount_yuan": 50_000_000,
                                   "auction_price_status": "FINAL"}],
                                 k_abs=1, k_amount=0, min_auction_amount_yuan=0)
    _, text = _extremes({"extremes_board": board}, "auction")
    assert "无板块" in text
    assert "绝对值#=竞价涨跌幅绝对值排名" in text and "额#=竞价匹配额排名" in text


def test_real_open_report_converges_appendix_within_mail_budget():
    import copy
    import json
    from pathlib import Path
    from engine_core.email_board import _front_page_view, _plates

    report = json.loads(Path('/home/exedev/core-data/reports/2026-11-24/open_0935/report.json').read_text())
    report['checkpoint'] = '0945'
    entries = report['a_to_b']
    report['a_to_b'] = [copy.deepcopy(entries[i % len(entries)]) for i in range(30)]
    board = report['chapters']['extremes_board']
    rows = board['auction']['rows']
    board['open']['rows'] = [copy.deepcopy(rows[i % len(rows)]) for i in range(700)]
    front, _ = _front_page_view(report, 'open')
    plates, _ = _plates(report, 'open')
    rendered, plain = render_email_board(report, 'open', '2026-11-24')
    assert len(rendered.encode('utf-8')) < 30_000
    assert '<details' not in rendered
    assert 'data-section="open-money-flow"' in rendered
    assert 'data-section="open-verification"' in rendered
    assert '收盘%' not in rendered + plain
    assert 'close_pct' not in rendered + plain
    assert re.search(r'(?<!\d)\d{6}(?!\d)', re.sub(r'<[^>]*>', ' ', rendered) + plain) is None


@pytest.mark.parametrize("checkpoint", ["0935", "0945"])
def test_real_open_minimal_contract(checkpoint):
    import json
    from pathlib import Path
    report = json.loads(Path(f"/home/exedev/core-data/reports/2026-11-25/open_{checkpoint}/report.json").read_text())
    rendered, plain = render_email_board(report, "open", "2026-11-25")
    assert len(rendered.encode("utf-8")) < 30_000
    assert "<details" not in rendered
    assert 'data-section="open-money-flow"' in rendered
    assert 'data-section="open-verification"' in rendered
    for marker, limit in (("open-flow-row", 5), ("open-emerging-row", 5),
                          ("open-retreat-row", 3), ("open-highboard", 6)):
        assert rendered.count(f'data-item="{marker}"') <= limit
    chapters = report["chapters"]
    assert rendered.count('data-item="open-flow-row"') == 5
    assert rendered.count('data-item="open-emerging-row"') == min(5, len(chapters["emerging"]))
    assert rendered.count('data-item="open-retreat-row"') == min(3, sum(
        1 for row in chapters["extremes_board"]["open"]["rows"]
        if row.get("tracking", {}).get("delta_latest_pp") is not None
        and row["tracking"]["delta_latest_pp"] < 0))
    for row in sorted(chapters["emerging"], key=lambda row: row.get("match_amount_yuan") or 0, reverse=True)[:5]:
        assert row["name"] in rendered
    assert not re.search(r"(?<!\d)\d{6}(?!\d)", rendered + plain)
    assert not any(word in rendered + plain for word in ("close_pct", "close_mean", "close_median", "收盘"))


def test_open_verification_real_report_contract():
    import json
    import re
    from pathlib import Path

    report = json.loads(Path('/home/exedev/core-data/reports/2026-11-24/open_0935/report.json').read_text())
    rendered, plain = render_email_board(report, 'open', '2026-11-24')
    for expected in ('data-section="open-money-flow"', 'data-section="open-verification"',
                     'data-item="open-flow-row"', '文投控股', '荣联科技', '东山精密', '封死', '开板'):
        assert expected in rendered
    assert len(rendered.encode('utf-8')) < 90_000
    assert not re.search(r'(?<!\d)\d{6}(?!\d)', rendered + plain)
    assert not any(word in rendered + plain for word in ('close_pct', 'close_mean', 'close_median', '收盘'))


def test_real_open_attack_and_risk_corrections():
    import json
    import re
    from pathlib import Path

    report = json.loads(Path('/home/exedev/core-data/reports/2026-11-25/open_0935/report.json').read_text())
    rendered, _ = render_email_board(report, 'open', '2026-11-25')
    flow = ''.join(re.findall(r'<tr data-item="open-flow-row">.*?</tr>', rendered))
    risk = re.search(r'🩸 放量失血.*?</div>', rendered)
    seals = ''.join(re.findall(r'<span data-item="open-seal".*?</span>', rendered))
    assert '医药' in flow and '存储' not in flow and '先进封装' not in flow
    assert risk and '存储' in risk.group()
    assert 'ST天圣' not in seals and 'ST汇洲' not in seals
    assert re.search(r'克来机电 · 封[\d.]+亿', seals)
    assert '砸盘/炸板避坑 Top 3' in rendered and '高开跳水避坑 Top 3' not in rendered
    assert len(rendered.encode('utf-8')) < 25_000 and '<details' not in rendered
