from engine_core.plate_auction_open_report import build_session_report, render_report_markdown
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
    bank["times"]["0925"]["performance"]["covered_count"] = 20
    bank["times"]["0925"]["activity"].update(
        amount_share_of_market=0.01, baseline_multiple=31.3, flags=["BASELINE_SHORT"])
    thin = {**bank, "plate": "冰雪产业", "member_count": 5, "priced_member_count": 5,
            "quality_flags": ["SMALL_OR_THIN"], "times": dict(bank["times"])}
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
