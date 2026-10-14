from engine_core.plate_auction_open_report import (
    _build_auction_sections,
    build_session_report,
    render_report_markdown,
)
import pytest


def _source():
    return {
        "trade_date": "2026-10-13",
        "snapshot": {
            "stock_plate": {"000001": "银行", "000002": "银行"},
            "s2p": {"000001": ["金融"], "000002": ["金融"]},
        },
        "pre_close_by_symbol": {"000001": 10.0, "000002": 20.0},
        "auction_by_tag": {
            "0920": {"000001": {"px_milli": 10100, "match_amt_yuan": 100, "rest_bid_amt_yuan": 5}},
            "0924": {"000001": {"px_milli": 10200, "match_amt_yuan": 200}},
            "0925": {"000001": {"px_milli": 10300, "match_amt_yuan": 300, "limit_state": 1}},
        },
        "opening_by_symbol": {
            "000001": {"ts": "2026-10-13 09:32:00", "px_milli": 11000, "amt_yuan": 900},
        },
        "yest_pool_by_symbol": {"000001": {"lb_days": 2}},
        "degradation": ["KAIPAN_INPUTS_MISSING"],
    }


def test_auction_match_and_opening_traded_amounts_are_separate():
    result = build_session_report(**_source())
    stock = next(row for row in result["stocks"] if row["symbol"] == "000001")
    assert stock["times"]["0925"]["amount"] == 300
    assert stock["times"]["0925"]["amount_kind"] == "MATCH"
    assert stock["times"]["0932"]["amount"] == 900
    assert stock["times"]["0932"]["amount_kind"] == "TRADED"
    assert stock["times"]["0932"]["amount"] != 900 + 300
    assert stock["times"]["0925"]["input_cutoff"] == "2026-10-13T09:25:00"
    assert stock["times"]["0932"]["input_cutoff"] == "2026-10-13T09:32:00"
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    assert bank["times"]["0932"]["input_cutoff"] == "2026-10-13T09:32:00"


def test_stock_scenario_report_has_three_lists_and_rule_explanation():
    result = build_session_report(**_source())
    assert result["stocks"][0]["scenario"] is not None
    report = result["report_md"]
    assert "## 个股竞价→开盘情景" in report
    assert "HIGH×DOWN（高开回落）" in report
    assert "LOW×UP（低开拉升）" in report
    assert "HIGH×UP（高开延续）" in report
    assert "FINAL / INDICATIVE" in report
    assert "缺失不补 0" in report


def test_integrated_plate_stock_views_follow_the_first_screen_summary():
    result = build_session_report(**_source())
    report = result["report_md"]
    assert report.index("## 表现 × 活跃度摘要") < report.index("## 竞价分析")
    assert report.index("## 竞价分析") < report.index("## 开盘分析")
    assert "## 延续 / 反转" in report
    assert "plate_stock_view" in result
    assert result["plate_stock_view"]["manifest"]["focus_count_by_stage"]["0925"] <= 30


def test_plate_strength_is_two_explicit_dimensions_and_excludes_current_baseline():
    source = _source()
    source["baseline_auction_by_tag"] = {"0925": [
        {"trade_date": "2026-10-10", "amount_by_symbol": {"000001": 100}},
        {"trade_date": "2026-10-11", "amount_by_symbol": {"000001": 100}},
        {"trade_date": "2026-10-12", "amount_by_symbol": {"000001": 100}},
        {"trade_date": "2026-10-13", "amount_by_symbol": {"000001": 1_000_000}},
    ]}
    result = build_session_report(**source)
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    row = bank["times"]["0925"]

    assert row["activity"]["baseline_days"] == 3
    assert row["activity"]["baseline_multiple"] == 1.0
    assert "relative_market_z" in row["performance"]
    assert "score" not in row and "strength" not in row and "strength_rank" not in row
    assert set(row["activity"]) == {"amount_kind", "amount_sum", "amount_share_of_market",
                                    "baseline_share", "baseline_multiple", "baseline_days", "flags"}


def test_conserved_activity_shares_sum_to_one_over_covered_market_amount():
    result = build_session_report(**_source())
    shares = [plate["times"]["0925"]["activity"]["amount_share_of_market"]
              for plate in result["plates"] if plate["times"]["0925"]]
    assert sum(shares) == pytest.approx(1.0)


def _size_neutral_input():
    symbols = [f"0000{index:02d}" for index in range(11)]
    source = {
        "trade_date": "2026-10-13",
        "snapshot": {"stock_plate": {symbol: "主题" for symbol in symbols}, "s2p": {}},
        "pre_close_by_symbol": {symbol: 10.0 for symbol in symbols},
        "auction_by_tag": {tag: {symbol: {"px_milli": 10100, "match_amt_yuan": 100.0}
                                 for symbol in symbols}
                           for tag in ("0920", "0924", "0925")},
        "opening_by_symbol": {symbol: {"ts": "2026-10-13 09:32:00", "px_milli": 10100,
                                        "amt_yuan": 100.0} for symbol in symbols},
        "stock_caps": {symbol: {"float_cap": 1_000_000.0, "total_cap": 2_000_000.0}
                       for symbol in symbols[:10]},
    }
    return source


def test_size_neutral_metrics_are_hand_computable_and_ignore_missing_caps():
    source = _size_neutral_input()
    source["auction_by_tag"]["0925"]["000010"]["match_amt_yuan"] = 900.0
    result = build_session_report(**source)
    section = next(row for row in result["plates"] if row["plate"] == "主题")["times"]["0925"]

    assert section["member_count_with_cap"] == 10
    assert section["member_sum_amount_yuan"] == 1000.0
    assert section["amount_turnover_permille"] == pytest.approx(0.1)
    assert section["relative_size_multiple"] == pytest.approx(1.0)


def test_f10_industry_labels_only_unassigned_stocks_without_changing_plate_stats():
    source = _source()
    source["pre_close_by_symbol"]["000003"] = 10.0
    source["auction_by_tag"]["0925"]["000003"] = {"px_milli": 10100, "match_amt_yuan": 500}
    source["opening_by_symbol"]["000003"] = {"ts": "2026-10-13 09:32:00", "px_milli": 10200,
                                                "amt_yuan": 800}
    without_fallback = build_session_report(**source)
    with_fallback = build_session_report(**source, f10_industry_by_symbol={
        "000001": "金融-银行-国有大型银行", "000003": "电子-半导体-数字芯片设计",
    })
    with_empty_industry = build_session_report(**source, f10_industry_by_symbol={"000003": "  "})

    industry_stock = next(row for row in with_fallback["stocks"] if row["symbol"] == "000003")
    assigned_stock = next(row for row in with_fallback["stocks"] if row["symbol"] == "000001")
    before_bank = next(row for row in without_fallback["plates"] if row["plate"] == "银行")
    after_bank = next(row for row in with_fallback["plates"] if row["plate"] == "银行")
    assert industry_stock["primary_plate"] == "行业:电子-半导体-数字芯片设计"
    assert "industry_fallback" not in without_fallback
    assert industry_stock["plates"] == []
    assert industry_stock["industry_fallback"] == {
        "industry": "电子-半导体-数字芯片设计", "source": "F10_INDUSTRY", "asof_unknown": True,
    }
    assert assigned_stock["primary_plate"] == "银行"
    assert after_bank["member_count"] == before_bank["member_count"]
    assert after_bank["times"]["0925"]["activity"]["amount_sum"] == before_bank["times"]["0925"]["activity"]["amount_sum"]
    assert with_fallback["industry_fallback"] == [{
        "code": "000003", "name": None, "industry": "电子-半导体-数字芯片设计",
    }]
    assert with_empty_industry["industry_fallback"] == []
    assert with_empty_industry["no_industry_no_plate_count"] == 1
    assert next(row for row in with_empty_industry["stocks"] if row["symbol"] == "000003")["primary_plate"] is None


def test_size_neutral_board_applies_thresholds_sorts_and_marks_concentration():
    source = _size_neutral_input()
    for symbol in list(source["auction_by_tag"]["0925"])[:1]:
        source["auction_by_tag"]["0925"][symbol]["match_amt_yuan"] = 5000.0
    result = build_session_report(**source)
    theme = next(row for row in result["plates"] if row["plate"] == "主题")
    higher = dict(theme)
    higher.update({"plate": "更高倍数", "times": dict(theme["times"])})
    higher["times"]["0925"] = dict(theme["times"]["0925"])
    higher["times"]["0925"].update({"relative_size_multiple": 3.0, "top1_concentration": 0.6})
    thin = dict(higher)
    thin.update({"plate": "薄成员", "times": dict(higher["times"])})
    thin["times"]["0925"] = dict(higher["times"]["0925"])
    thin["times"]["0925"].update({"relative_size_multiple": 9.0, "member_count_with_cap": 9})
    result["plates"].extend((higher, thin))
    for stock in result["stocks"][:10]:
        stock["plates"].append({"plate": "更高倍数"})
    result["stocks"][0]["plates"].append({"plate": "薄成员"})
    section = _build_auction_sections(result)["0925"]

    assert section["size_neutral_top"][0]["plate"] == "更高倍数"
    assert section["size_neutral_top"][0]["concentration_flag"] == "CONCENTRATED"
    assert section["size_neutral_top"][0]["top1_concentration"] == 0.6
    assert all(row["plate"] != "薄成员" for row in section["size_neutral_top"])


def test_small_plate_filter_is_stage_specific_and_preserves_excluded_rows():
    source = _size_neutral_input()
    symbols = list(source["snapshot"]["stock_plate"])
    for tag in ("0920", "0924", "0925"):
        source["auction_by_tag"][tag] = {
            symbol: {"px_milli": 10100, "match_amt_yuan": 100}
            for symbol in symbols
        }
    source["auction_by_tag"]["0920"] = {
        symbol: {"px_milli": 10100, "match_amt_yuan": 100} for symbol in symbols[:4]
    }
    source["auction_by_tag"]["0924"] = {
        symbol: {"px_milli": 10100, "match_amt_yuan": 100} for symbol in symbols[:5]
    }
    source["opening_by_symbol"] = {
        symbol: {"ts": "2026-10-13 09:32:00", "px_milli": 10100, "amt_yuan": 100}
        for symbol in symbols
    }

    result = build_session_report(**source)
    sections = _build_auction_sections(result)

    assert sections["0920"]["excluded_small_plates"] == [{
        "code": "主题", "name": "主题", "effective_members": 4, "nominal_members": 11,
    }]
    assert "主题" not in {row["plate"] for row in sections["0920"]["all"]}
    assert "主题" in {row["plate"] for row in sections["0924"]["all"]}
    assert result["plates"][0]["times"]["0920"]["effective_member_count"] == 4
    assert result["plates"][0]["times"]["0924"]["effective_member_count"] == 5
    assert result["plates"][0]["times"]["0925"]["effective_member_count"] == 11
    assert "本时点因有效成员<5未入榜: 1 个" in result["report_md"]


def test_size_neutral_summary_is_rendered_and_absent_without_caps():
    source = _source()
    result = build_session_report(**source)
    assert "额换手‰" not in result["report_md"]

    text = render_report_markdown(result)
    assert "额换手‰" not in text


def test_size_neutral_metrics_are_added_to_report_and_first_screen():
    source = _size_neutral_input()

    result = build_session_report(**source)

    assert "规模中性前 3" in result["report_md"].split("## 阅读说明", 1)[0]
    assert "额换手‰" in result["report_md"]
    assert result["plates"][0]["times"]["0925"]["amount_turnover_permille"] == pytest.approx(0.1)


def test_0925_summary_counts_final_and_indicative_and_is_in_first_screen():
    source = _source()
    source["auction_by_tag"]["0925"]["000001"]["price_source"] = "SNAPSHOT_PX"
    source["auction_by_tag"]["0925"]["000002"] = {
        "px_milli": 19900, "match_amt_yuan": 100, "price_source": "TICK_BOOK1_INDICATIVE"}
    result = build_session_report(**source)
    lines = result["report_md"].splitlines()
    assert any("0925:" in line and "终价 1 / 指示价 1" in line for line in lines[:10])


def test_primary_stock_plate_summary_exposes_activity_and_contribution():
    result = build_session_report(**_source())
    stock = next(row for row in result["stocks"] if row["symbol"] == "000001")
    primary = stock["plate_summary"][0]
    assert primary["plate"] == stock["primary_plate"]
    assert "activity_multiple_0925" in primary
    assert "performance_z_0925" in primary
    assert "stock_amount_share_0925" in primary
    assert {"0920", "0924", "0925", "0932"}.issubset(primary["activity_by_stage"])
    assert "score" not in primary


def test_0925r_is_rendered_as_revised_match_with_its_cutoff():
    result = build_session_report(**_source())
    result["market"]["0925R"] = dict(result["market"]["0925"])
    result["input_cutoff"]["0925R"] = "2026-10-13T09:27:10"
    for row in result["stocks"]:
        row["times"]["0925R"] = dict(row["times"]["0925"])
    for row in result["plates"]:
        if row["times"].get("0925") is not None:
            row["times"]["0925R"] = dict(row["times"]["0925"])

    text = render_report_markdown(result)

    assert "0925R=2026-10-13T09:27:10" in text
    assert "0925R MATCH_REVISED(亿元)" in text
    assert "0925R MATCH_REVISED(亿元)" in text


def test_auction_section_exposes_conserved_and_member_sum_amounts_and_keeps_special_categories():
    stocks = []
    for index in range(10):
        symbol = f"0000{index:02d}"
        stocks.append({
            "symbol": symbol,
            "plates": [{"plate": "通信"}, {"plate": "三季报增长"}],
            "times": {tag: {"amount": 100.0, "pct_vs_pre_close": 0.01,
                            "rest_bid": 0.0, "rest_ask": 0.0, "price": 10.0}
                      for tag in ("0920", "0924", "0925")},
        })
    result = {"stocks": stocks, "plates": [
        {"plate": "通信", "category": "THEME"},
        {"plate": "三季报增长", "category": "EVENT"},
    ]}

    section = _build_auction_sections(result)["0925"]

    by_name = {row["plate"]: row for row in section["all"]}
    assert by_name["通信"]["match_amount_yuan"] == 500.0
    assert by_name["通信"]["member_sum_amount_yuan"] == 1000.0
    assert by_name["三季报增长"]["category"] == "EVENT"
    assert {row["plate"] for row in section["main"]} == {"通信", "三季报增长"}
    assert [row["plate"] for row in section["member_sum_top"]] == ["三季报增长", "通信"]


def test_auction_markdown_marks_event_and_generic_rows_without_changing_first_screen_filters():
    assert "<span style=\"color:#888\">[事件] </span>" in __import__(
        "engine_core.plate_auction_open_report", fromlist=["_auction_plate_label"]
    )._auction_plate_label({"plate": "事件板块", "category": "EVENT"})
    assert "<span style=\"color:#888\">[泛化] </span>" in __import__(
        "engine_core.plate_auction_open_report", fromlist=["_auction_plate_label"]
    )._auction_plate_label({"plate": "泛化板块", "category": "GENERIC_FACTOR"})
    source = _source()
    source["snapshot"]["stock_plate"].update({f"0000{i:02d}": "事件甲" for i in range(2, 12)})
    source["snapshot"]["s2p"].update({f"0000{i:02d}": ["事件甲"] for i in range(2, 12)})
    source["pre_close_by_symbol"].update({f"0000{i:02d}": 10.0 for i in range(2, 12)})
    for tag, px in (("0920", 10100), ("0924", 10200), ("0925", 10300)):
        source["auction_by_tag"][tag].update({f"0000{i:02d}": {
            "px_milli": px, "match_amt_yuan": 300} for i in range(2, 12)})
    result = build_session_report(**source)
    event = next(row for row in result["plates"] if row["plate"] == "事件甲")
    event["category"] = "EVENT"

    text = render_report_markdown(result)

    first_screen = text.split("## 阅读说明", 1)[0]
    assert "[事件] </span>事件甲" in text
    assert "放量偏强 Top3" in first_screen and "事件甲(z=" not in first_screen


def test_kaipan_comparison_uses_latest_capture_within_ninety_seconds_and_labels_zero_amount():
    source = _source()
    for index in range(3, 12):
        symbol = f"0000{index:02d}"
        source["snapshot"]["stock_plate"][symbol] = "银行"
        source["pre_close_by_symbol"][symbol] = 10.0
        for tag, px in (("0920", 10100), ("0924", 10200), ("0925", 10300)):
            source["auction_by_tag"][tag][symbol] = {"px_milli": px, "match_amt_yuan": 100}
    source["hot_captures"] = [
        {"captured_local": "2026-10-13T09:25:30+08:00", "response": {
            "Day": "2026-10-13", "list": [{"name": "银行", "rank": 1, "change_pct": 0.01,
                                              "amount_yuan": 700, "strength": 7}]}},
        {"captured_local": "2026-10-13T09:26:00+08:00", "response": {
            "Day": "2026-10-13", "list": [{"name": "银行", "rank": 1, "change_pct": 0.02,
                                              "amount_yuan": 0, "strength": 9}]}},
        {"captured_local": "2026-10-13T09:26:31+08:00", "response": {
            "Day": "2026-10-13", "list": [{"name": "银行", "rank": 1, "change_pct": 0.03,
                                              "amount_yuan": 900, "strength": 11}]}},
    ]

    result = build_session_report(**source)
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    stage = bank["times"]["0925"]

    assert stage["hot_live_captured_at"] == "2026-10-13T09:26:00+08:00"
    assert stage["hot_live_staleness_s"] == 60.0
    assert stage["hot_live_strength"] == 9
    assert "Kaipan 竞价期无金额" in result["report_md"]


def test_auction_sections_rank_by_match_amount_and_keep_missing_deltas_missing():
    source = _source()
    source["snapshot"]["stock_plate"] = {}
    source["snapshot"]["s2p"] = {}
    source["snapshot"]["names"] = {}
    source["pre_close_by_symbol"] = {}
    source["auction_by_tag"] = {tag: {} for tag in ("0920", "0924", "0925")}
    for prefix, plate, unit in (("0001", "新能源", 100), ("0002", "银行", 10)):
        for idx in range(10):
            symbol = f"{prefix}{idx:02d}"
            source["snapshot"]["stock_plate"][symbol] = plate
            source["snapshot"]["names"][symbol] = f"名称{idx}"
            source["pre_close_by_symbol"][symbol] = 10.0
            for tag, amount in (("0920", unit), ("0924", unit * 1.2), ("0925", unit * 1.5)):
                source["auction_by_tag"][tag][symbol] = {
                    "px_milli": 11000 if prefix == "0002" else 10100,
                    "match_amt_yuan": amount,
                    "rest_bid_amt_yuan": 4 if tag == "0925" else None,
                    "rest_ask_amt_yuan": 1 if tag == "0925" else None,
                    "price_source": "TICK_PX_FINAL" if tag == "0925" else "TICK_BOOK1_INDICATIVE",
                    "limit_state": 1 if prefix == "0002" and tag == "0925" else None,
                }

    result = build_session_report(**source)

    assert [row["plate"] for row in result["auction_sections"]["0920"]["main"]][0] == "新能源"
    assert result["auction_sections"]["0920"]["main"][0]["match_amount_yuan"] == 1000
    assert result["auction_sections"]["0920"]["main"][0]["previous_amount_change_yuan"] is None
    assert result["auction_sections"]["0924"]["main"][0]["previous_amount_change_yuan"] == 200
    bank = next(row for row in result["auction_sections"]["0925"]["main"] if row["plate"] == "银行")
    assert bank["limit_up_count"] == 10
    assert result["auction_sections"]["0925"]["main"][0]["price_coverage_pct"] is not None


def test_auction_pressure_uses_absolute_amount_columns_and_previous_tag_needs_five_prices():
    source = _source()
    source["snapshot"]["stock_plate"] = {}
    source["snapshot"]["s2p"] = {}
    source["snapshot"]["names"] = {}
    source["pre_close_by_symbol"] = {}
    source["auction_by_tag"] = {tag: {} for tag in ("0920", "0924", "0925")}
    for idx in range(10):
        symbol = f"00020{idx}"
        source["snapshot"]["stock_plate"][symbol] = "新能源"
        source["pre_close_by_symbol"][symbol] = 10.0
        for tag in ("0920", "0924", "0925"):
            source["auction_by_tag"][tag][symbol] = {
                "px_milli": 10100 if tag != "0920" or idx < 4 else None,
                "match_amt_yuan": 100,
                "rest_bid_amt_yuan": 20,
                "rest_ask_amt_yuan": 5,
            }

    result = build_session_report(**source)
    section = result["auction_sections"]["0924"]
    plate = next(row for row in section["main"] if row["plate"] == "新能源")
    text = result["report_md"]

    assert plate["previous_pct_change_pp"] is None
    assert plate["previous_amount_change_yuan"] is None
    final_plate = next(row for row in result["auction_sections"]["0925"]["main"] if row["plate"] == "新能源")
    assert final_plate["previous_pct_change_pp"] == 0
    assert final_plate["previous_amount_change_yuan"] == 0
    assert plate["unmatched_buy_yuan"] == 200
    assert plate["unmatched_sell_yuan"] == 50
    assert plate["unmatched_buy_sell_diff_yuan"] == 150
    assert "未匹配买(亿)" in text and "未匹配卖(亿)" in text and "买卖差(亿)" in text
    assert "代码 | 名称 | 价格口径" in text
    assert "买单(亿)" not in text and "卖单(亿)" not in text
    assert "买卖压力%" not in text
    assert "上一标签有价成员少于 5" in text


def test_auction_gain_loss_and_stock_tables_render_price_provenance():
    source = _source()
    source["snapshot"]["stock_plate"] = {}
    source["snapshot"]["s2p"] = {}
    source["snapshot"]["names"] = {}
    source["pre_close_by_symbol"] = {}
    source["auction_by_tag"] = {tag: {} for tag in ("0920", "0924", "0925")}
    for idx in range(10):
        symbol = f"00010{idx}"
        source["snapshot"]["stock_plate"][symbol] = "新能源"
        source["snapshot"]["names"][symbol] = f"名称{idx}"
        source["pre_close_by_symbol"][symbol] = 10.0
        for tag in ("0920", "0924", "0925"):
            source["auction_by_tag"][tag][symbol] = {
                "px_milli": 10100 + idx * 100, "match_amt_yuan": 1000 - idx * 50,
                "price_source": "SNAPSHOT_PX" if idx == 0 and tag == "0925" else "TICK_PX_FINAL",
                "price_flags": ["PRE_FINAL_INDICATIVE"] if idx == 1 and tag == "0925" else [],
                "rest_bid_amt_yuan": 40 if tag == "0925" else None,
                "rest_ask_amt_yuan": 10 if tag == "0925" else None,
            }
    result = build_session_report(**source)
    section = result["auction_sections"]["0925"]

    assert section["gainers"] and section["losers"]
    assert section["stocks"][0]["price_kind"] == "SNAPSHOT_PX"
    assert any(row["price_kind"] == "PRE_FINAL_INDICATIVE" for row in section["stocks"])
    assert "竞价事实: 虚拟撮合" in result["report_md"]
    assert "竞价个股榜 0925" in result["report_md"]


def test_first_screen_market_summary_shows_auction_amount_progress():
    result = build_session_report(**_source())
    lines = result["report_md"].splitlines()
    assert "竞价金额进度" in "\n".join(lines[:18])
    assert "0920→0925" in result["report_md"]


def test_opening_tick_after_cutoff_is_not_used():
    source = _source()
    source["opening_by_symbol"]["000001"]["ts"] = "2026-10-13 09:32:01"
    result = build_session_report(**source)
    stock = next(row for row in result["stocks"] if row["symbol"] == "000001")
    assert stock["times"]["0932"]["amount"] is None
    assert "NO_0932_ASOF_ROW" in stock["flags"]


def test_open_times_keep_each_cutoff_and_missing_prices_missing():
    source = _source()
    source["open_times"] = ("0932", "0935")
    source["opening_by_tag"] = {"0932": {"000001": source["opening_by_symbol"]["000001"]},
                                "0935": {"000001": {"ts": "2026-10-13 09:35:00",
                                                     "px_milli": 11200, "amt_yuan": 1800}}}
    result = build_session_report(**source)
    stock = next(row for row in result["stocks"] if row["symbol"] == "000001")
    assert stock["times"]["0935"]["input_cutoff"] == "2026-10-13T09:35:00"
    assert stock["times"]["0935"]["price"] == 11.2
    assert stock["times"]["0935"]["amount"] == 1800
    missing = next(row for row in result["stocks"] if row["symbol"] == "000002")
    assert missing["times"]["0935"]["price"] is None


def test_event_facts_are_unranked_and_render_z_to_two_decimals():
    from engine_core.plate_auction_open_report import render_independent_plates_markdown

    text = render_independent_plates_markdown({"0925": {
        "market": {"median": 0.0}, "selected": [{"code": "801001", "name": "主题",
            "direction": "STRONG", "z": 3.456}],
        "uncovered": {}, "event_plates": [{"name": "并购重组", "covered_count": 4,
            "trim_mean": 0.01, "limit_up_count": 1, "top3": []}],
        "generic_controls": [{"name": "全体规模", "n": 2, "relative_market_pp": 0.01, "z": None}],
    }})
    assert "事件类, 仅作事实" in text
    assert "并购重组" in text and "| 排名 |" in text
    assert "3.46" in text and "样本不足" in text
    assert "触及涨停价数" in text and "封单涨停数(未知时为—)" in text
    event_part = text.split("事件类, 仅作事实", 1)[1]
    event_header = event_part.splitlines()[1]
    assert "排名" not in event_header


def test_membership_aggregation_deduplicates_members_and_keeps_missing_none():
    result = build_session_report(**_source())
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    assert bank["member_count"] == 2
    assert bank["times"]["0925"]["covered_count"] == 1
    assert bank["times"]["0925"]["amount_sum"] == 300
    stock = next(row for row in result["stocks"] if row["symbol"] == "000002")
    assert stock["times"]["0920"]["price"] is None
    assert stock["times"]["0920"]["amount"] is None


def test_yesterday_pool_marker_and_limit_state_are_preserved():
    result = build_session_report(**_source())
    stock = next(row for row in result["stocks"] if row["symbol"] == "000001")
    assert stock["in_yest_pool"] is True
    assert stock["lb_days"] == 2
    assert stock["times"]["0925"]["limit_state"] == 1
    assert stock["times"]["0932"]["limit_state"] is None
    assert stock["times"]["0932"]["at_limit_price"] is True


def test_report_contains_degradation_and_virtual_match_semantics():
    result = build_session_report(**_source())
    assert "KAIPAN_INPUTS_MISSING" in result["report_md"]
    assert "竞价金额为虚拟匹配额" in result["report_md"]
    assert "昨日涨停池成员" in result["report_md"]
    assert "## 活跃度榜（与表现分开）" in result["report_md"]
    assert "## 独立板块（净效应）" not in result["report_md"]


def test_first_screen_filters_thin_and_short_baseline_plates_but_activity_can_show_short():
    from engine_core.plate_auction_open_report import render_report_markdown

    result = build_session_report(**_source())
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    bank["member_count"] = 20
    bank["priced_member_count"] = 20
    bank["times"]["0925"]["effective_member_count"] = None
    bank["times"]["0925"]["performance"]["covered_count"] = 20
    bank["times"]["0925"]["activity"].update(
        amount_share_of_market=0.01, baseline_multiple=31.3, flags=["BASELINE_SHORT"])
    thin = {**bank, "plate": "冰雪产业", "member_count": 5, "priced_member_count": 5,
            "quality_flags": ["SMALL_OR_THIN"],
            "times": {tag: dict(value) if value else None for tag, value in bank["times"].items()}}
    for stage in thin["times"].values():
        if stage:
            stage["effective_member_count"] = 5
    thin["times"]["0925"] = {**bank["times"]["0925"], "performance": {
        **bank["times"]["0925"]["performance"], "covered_count": 5}}
    thin["times"]["0925"]["activity"] = {**bank["times"]["0925"]["activity"],
        "amount_share_of_market": 0.0001, "flags": []}
    result["plates"].append(thin)

    text = render_report_markdown(result)

    summary = text.split("## 阅读说明", 1)[0]
    assert "银行(z=" not in summary
    assert "冰雪产业" not in summary
    active = text.split("## 活跃度榜（与表现分开）", 1)[1].split("## 小样本/薄额板块", 1)[0]
    assert "银行" in active
    assert "BASELINE_SHORT" in active
    assert "冰雪产业" not in active
    assert "冰雪产业" in text.split("## 小样本/薄额板块", 1)[1]


def test_defaults_and_degradation_labels_are_one_explained_line():
    source = _source()
    source["degradation"] = ["STATIC_ASOF_UNKNOWN", "NOT_POINT_IN_TIME"]
    result = build_session_report(**source)

    assert result["candidate_thresholds"] == {"min_members": 10, "min_share": 0.003}
    label_line = result["report_md"].splitlines()[0]
    assert label_line.startswith("标签: ")
    assert "STATIC_ASOF_UNKNOWN（" in label_line
    assert "NOT_POINT_IN_TIME（" in label_line
    assert "静态关系 CSV" in label_line and "竞价快照点时性" in label_line


def test_activity_fields_keep_missing_values_and_report_short_baseline():
    result = build_session_report(**_source())
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    for tag in ("0920", "0924", "0925", "0932"):
        activity = bank["times"][tag]["activity"]
        assert activity["baseline_multiple"] is None
        assert "BASELINE_SHORT" in activity["flags"]


def test_baseline_uses_only_five_prior_dates_and_matches_amount_kind():
    source = _source()
    for index in range(2, 11):
        symbol = f"0000{index:02d}"
        source["snapshot"]["stock_plate"][symbol] = "银行"
        source["pre_close_by_symbol"][symbol] = 10.0
        for tag, amount in (("0920", 10), ("0924", 20), ("0925", 30)):
            source["auction_by_tag"][tag][symbol] = {"px_milli": 10100, "match_amt_yuan": amount}
        source["opening_by_symbol"][symbol] = {"ts": "2026-10-13 09:32:00", "px_milli": 10200,
                                                "amt_yuan": 90}
    source["baseline_auction_by_tag"] = {
        "0925": [
            {"trade_date": f"2026-10-{day:02d}", "amount_by_symbol": {"000001": day * 10}}
            for day in range(8, 13)
        ]
    }
    source["baseline_auction_by_tag"]["0925"].append({
        "trade_date": "2026-10-13",
        "amount_by_symbol": {"000001": 10, **{f"0000{i:02d}": 100 for i in range(2, 11)}},
    })
    result = build_session_report(**source)
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    activity = bank["times"]["0925"]["activity"]
    assert activity["baseline_share"] is not None
    assert activity["baseline_days"] == 5
    assert "BASELINE_SHORT" not in activity["flags"]
    assert bank["times"]["0932"]["activity"]["baseline_share"] is None


def test_hot_live_uses_latest_capture_at_or_before_cutoff_and_day_match():
    source = _source()
    source["hot_prev_by_plate"] = {"银行": {"rank": 1, "strength": 10, "change_pct": 2}}
    source["hot_captures"] = [
        {"captured_local": "2026-10-13T09:23:00+08:00", "Day": "2026-10-13",
         "rows": [{"name": "银行", "rank": 3, "strength": 11, "change_pct": 3}]},
        {"captured_local": "2026-10-13T09:24:00+08:00", "Day": "2026-10-14",
         "rows": [{"name": "银行", "rank": 2, "strength": 99, "change_pct": 9}]},
        {"captured_local": "2026-10-13T09:26:00+08:00", "Day": "2026-10-14",
         "rows": [{"name": "银行", "rank": 1, "strength": 99, "change_pct": 9}]},
    ]
    result = build_session_report(**source)
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    assert bank["times"]["0925"]["hot_live_rank"] == 3
    assert bank["times"]["0925"]["hot_prev_close_rank"] == 1
    assert bank["times"]["0924"]["hot_live_rank"] == 3


def test_two_dimensions_replace_composite_strength_summary():
    result = build_session_report(**_source())
    assert "activity_manifest" in result
    for row in result["plates"]:
        for stage in row["times"].values():
            if stage:
                assert "activity" in stage and "performance" in stage
                assert "strength" not in stage and "score" not in stage


def test_markdown_activity_table_keeps_missing_baseline_visible():
    result = build_session_report(**_source())
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    report = render_report_markdown(result)
    assert "### 0925" in report
    assert "基线倍数" in report
    assert "BASELINE_SHORT" in str(bank["times"]["0925"]["activity"]["flags"])


def test_hot_day_list_shape_is_accepted():
    from examples.run_offline_plate_auction_open_report import _load_prev_hot
    import json, tempfile, pathlib

    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp) / "hot_plates"
        d.mkdir()
        (d / "2026-10-13.json").write_text(json.dumps(
            {"Day": ["2026-10-13"], "list": [["801660", "通信", 6780, 1.113]]}), encoding="utf-8")
        assert _load_prev_hot(pathlib.Path(tmp), "2026-10-13")["通信"]["rank"] == 1


def test_activity_baseline_ratio_is_reported_as_multiple():
    source = _source()
    for index in range(3, 12):
        symbol = f"0000{index:02d}"
        source["snapshot"]["stock_plate"][symbol] = "银行"
        source["pre_close_by_symbol"][symbol] = 10
        source["auction_by_tag"]["0925"][symbol] = {"px_milli": 10000, "match_amt_yuan": 100}
    source["baseline_auction_by_tag"] = {"0925": [{
        "trade_date": "2026-10-12", "amount_by_symbol": {
            **{f"0000{index:02d}": 100 for index in range(1, 12)}}
    }]}
    result = build_session_report(**source)
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    activity = bank["times"]["0925"]["activity"]
    result["report_md"] = render_report_markdown(result)
    assert activity["baseline_multiple"] is not None


def test_report_adds_scale_ranking_and_meaning_line_without_score_keys():
    result = build_session_report(**_source())
    template = next(row for row in result["plates"] if row["plate"] == "银行")
    for name, share, multiple in (("大盘主题", 0.02, 2.0), ("小盘主题", 0.01, 8.0),
                                 ("门槛以下", 0.002, 50.0)):
        row = {**template, "plate": name, "category": "THEME", "priced_member_count": 12,
               "member_count": 12, "times": {tag: dict(value) if value else None
                                               for tag, value in template["times"].items()}}
        for stage in row["times"].values():
            if stage:
                stage["effective_member_count"] = 12
                stage["activity"] = {**stage["activity"], "amount_share_of_market": share,
                                      "baseline_multiple": multiple}
                stage["performance"] = {**stage["performance"], "covered_count": 12}
        result["plates"].append(row)
    result["report_md"] = render_report_markdown(result)

    first_screen = result["report_md"].split("## 阅读说明", 1)[0]
    scale_section = result["report_md"].split("## 成交额占比榜", 1)[1].split("## 活跃度榜", 1)[0]
    assert "本报告含义" in first_screen
    assert "规模前 5 大盘主题" in first_screen
    assert scale_section.index("大盘主题") < scale_section.index("小盘主题")
    assert "| 大盘主题 |" in scale_section and "| 小盘主题 |" in scale_section
    assert "门槛以下" not in scale_section
    assert "score" not in result and "strength" not in result


def test_event_first_screen_line_uses_top_limit_touch_counts():
    from engine_core.plate_auction_open_report import render_independent_plates_markdown

    text = render_independent_plates_markdown({"0925": {
        "market": {"median": 0.0}, "selected": [], "uncovered": {}, "event_plates": [
            {"name": "并购重组", "covered_count": 20, "trim_mean": 0.03,
             "touch_limit_price_count": 7, "top3": []},
            {"name": "三季报增长", "covered_count": 20, "trim_mean": 0.01,
             "touch_limit_price_count": 4, "top3": []},
            {"name": "体育产业", "covered_count": 20, "trim_mean": -0.01,
             "touch_limit_price_count": 1, "top3": []},
            {"name": "事件四", "covered_count": 20, "trim_mean": 0.0,
             "touch_limit_price_count": 0, "top3": []},
        ]
    }})

    first_screen = text.split("| 排名 |", 1)[0]
    assert "触及涨停价数最多" in first_screen
    assert "并购重组" in first_screen and "三季报增长" in first_screen and "体育产业" in first_screen
    assert "事件四" not in first_screen


def test_absent_plate_in_top50_capture_is_marked_beyond_50():
    source = _source()
    source["hot_captures"] = [{"captured_local": "2026-10-13 09:25:00", "response": {
        "Day": ["2026-10-13"], "list": [{"rank": i, "name": f"热板{i}"} for i in range(1, 51)]}}]
    report = build_session_report(**source)

    bank = next(row for row in report["plates"] if row["plate"] == "银行")
    bank["priced_member_count"] = 10
    bank["times"]["0925"]["effective_member_count"] = 10
    bank["times"]["0925"]["activity"].update(amount_share_of_market=0.01, baseline_multiple=2)
    bank["times"]["0925"]["performance"]["covered_count"] = 10
    report["report_md"] = render_report_markdown(report)
    assert bank["times"]["0925"]["hot_live_rank"] == ">50"
    assert "| 银行 |" in report["report_md"] and ">50" in report["report_md"]


def test_stock_sections_include_pool_stages_and_sealed_limit_amounts():
    source = _source()
    source["snapshot"]["names"] = {"000001": "甲"}
    source["auction_by_tag"]["0925"]["000001"].update(
        {"rest_bid_amt_yuan": 200_000_000, "rest_ask_amt_yuan": 50_000_000})
    source["auction_by_tag"]["0925"]["000002"] = {
        "px_milli": 19000, "match_amt_yuan": 400, "limit_state": -1,
        "rest_bid_amt_yuan": 0, "rest_ask_amt_yuan": 300_000_000}
    report = build_session_report(**source)["report_md"]
    assert "昨日涨停池成员" in report and "连板数" in report and "0920" in report and "0932" in report
    assert "涨停/跌停个股" in report and "封单额(亿元)" in report
    assert "2.00" in report and "3.00" in report
    result = build_session_report(**source)
    assert "板块活动倍数/z/个股额占比" in result["report_md"]
    assert "竞价金额是虚拟撮合额" in result["report_md"]


def test_stock_tables_cap_each_at_40_rows_but_keep_all_stocks_in_json():
    source = _source()
    for index in range(2, 43):
        symbol = f"0000{index:02d}"
        source["snapshot"]["stock_plate"][symbol] = "银行"
        source["pre_close_by_symbol"][symbol] = 10
        source["auction_by_tag"]["0925"][symbol] = {
            "px_milli": 10000, "match_amt_yuan": 1000 - index, "limit_state": None}
    result = build_session_report(**source)
    section = result["report_md"].split("## 昨日涨停池成员", 1)[1].split("## 0925 涨停/跌停个股", 1)[0]
    assert len(result["stocks"]) == 42
    assert section.count("| 000") <= 40


def test_independent_plates_renderer_includes_fdr_disclaimer_and_reference_column():
    from engine_core.plate_auction_open_report import render_independent_plates_markdown

    text = render_independent_plates_markdown({"0925": {
        "market": {"median": -0.007},
        "selected": [{"rank": 1, "name": "AI应用", "class": "THEME", "direction": "STRONG",
                      "trim_mean": 0.02, "z": 4.2, "residual_n": 8, "n": 10,
                      "limit_up_count": 2, "top1_weight_share": 0.2,
                      "explained_members": 7, "hot_rank": 3}],
        "uncovered": {"n": 4, "median": 0.0, "trim_mean": 0.001, "p10": -0.01, "p90": 0.02},
        "controls_top": [],
    }})
    assert "## 独立板块（净效应）" in text
    assert "统计显著性(BH FDR 10%)" in text
    assert "非预测" in text and "相对市场" in text
    assert "Kaipan 热板名次(参考)" in text
    assert "未被覆盖个股" in text
