from engine_core.email_board import render_email_board


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
            {"symbol": "600003", "name": "丙股", "prior_streak": 2, "p0925_pct": 2.0,
             "p0945_pct": 5.0, "close_pct": 10.0, "auction_amount_yuan": 50000000}],
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
    assert rendered.index("情绪温度计") < rendered.index("连板梯队") < rendered.index("大肉")
    assert "2板" in rendered and "1/3" in rendered
    assert rendered.count("3板 昨3 晋1/3") == 1
    assert "新股" in rendered and "无涨跌幅限制" in rendered
    assert "看A做B：无合格A" in rendered
    assert "趋势" in rendered and "▁" in rendered
    assert "通信概念特别长名称" not in rendered
    assert "所属板块" not in rendered and "竞价涨幅" not in rendered
    assert "收盘变化÷板幅" not in rendered
    assert "收盘中位" in plain
    headers, cells = _cells(rendered)
    assert all(len(row) == len(set(row)) for row in headers)
    assert all(sum("\u4e00" <= char <= "\u9fff" for char in cell) <= 14 for cell in cells)
    assert not any("\n" in cell or "[" in cell or "]" in cell for cell in cells)
    assert rendered.count("<table") <= 12


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
    assert "竞价" in rendered and "09:45" in rendered


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
    assert "价格代理" in open_text
    assert "A0" in open_html


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
    assert len(rendered.encode("utf-8")) <= 120 * 1024
    assert rendered.count("<table") <= 12
    assert "<br" not in rendered
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
    ladder_text = rendered[rendered.index("连板梯队"):rendered.index("大肉 TOP8")]
    assert ladder_text.index("5板") < ladder_text.index("4板") < ladder_text.index("3板") < ladder_text.index("2板") < ladder_text.index("1板")
    assert "晋1/2" in ladder_text
    _, cells = _cells(rendered)
    # The new fixed-cohort board is separate; the old post-close appendix displays Top5.
    assert sum(1 for cell in cells if cell.startswith("股") or "股" in cell) <= 5
    assert "股11" not in rendered


def test_v6_shaped_close_report_renders_real_ladder_a_to_b_and_yesterday_performance():
    # Small fixture copied from report.full.json in close-review-20261015-v6.
    report = {
        "trade_date": "2026-10-15",
        "market": {"close_median": -3.0},
        "stocks": [
            {"symbol": "000530", "name": "冰山冷热", "in_yest_pool": True, "lb_days": 2,
             "limit_pct": 10, "auction_pct": 9.84, "p0925_pct": 9.84, "close_pct": 10,
             "high_pct": 10, "auction_amount_yuan": 11716600},
            {"symbol": "000668", "name": "荣丰控股", "in_yest_pool": True, "lb_days": 2,
             "limit_pct": 10, "auction_pct": -2, "p0925_pct": -2, "close_pct": -9.99,
             "high_pct": -1, "auction_amount_yuan": 5252500},
            {"symbol": "300781", "name": "因赛集团", "in_yest_pool": False, "lb_days": 0,
             "limit_pct": 20, "auction_pct": -2.18, "p0925_pct": -2.18,
             "p0945_pct": -0.89, "close_pct": -4.80, "auction_amount_yuan": 4742500,
             "main_plate": "外卖概念", "plates": ["外卖概念"], "prior_range_position20_pct": 79},
        ],
        "ladder_section": {"rows": [
            {"symbol": "000530", "name": "冰山冷热", "prior_streak": 2, "close_pct": 10},
        ], "summary": [], "yesterday_board_counts": [{"board_height": "2板", "count": 2}]},
        "a_to_b_status": "READY",
        "a_to_b": [{"a_symbol": "000530", "a_name": "冰山冷热", "a_prior_streak": 2,
                     "a_status": "昨日高标", "plate": "外卖概念", "plate_display": "外卖概念",
                     "candidates": [{"symbol": "300781", "name": "因赛集团", "auction_pct": -2.18,
                                     "auction_amount_yuan": 4742500, "position": "79%",
                                     "p0945_pct": -0.89, "close_pct": -4.80}], "reason": None}],
    }
    rendered, plain = render_email_board(report, "close", "2026-10-15")
    assert "晋1/2" in rendered
    assert "0.1亿" in rendered
    assert "高" in rendered
    assert "昨板表现" in rendered and "红1/2" in rendered
    assert "冰山冷热 昨2板" in rendered and "因赛集团" in rendered
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
        "extremes_board": board, "big_meat": [], "big_loss": [], "a_to_b": [],
        "data_gaps": []}, "open", "2026-10-15")
    visible = re.sub(r"<[^>]*>", " ", html) + plain
    assert "竞价集合开盘追踪" in visible and "甲股" in visible and "乙股" in visible
    assert "INDICATIVE" in visible or "指示价" in visible
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
