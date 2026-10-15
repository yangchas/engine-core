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
    assert sum(1 for cell in cells if cell.startswith("股") or "股" in cell) >= 8
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

