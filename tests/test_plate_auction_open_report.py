from engine_core.plate_auction_open_report import build_session_report, render_report_markdown


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


def test_opening_tick_after_cutoff_is_not_used():
    source = _source()
    source["opening_by_symbol"]["000001"]["ts"] = "2026-10-13 09:32:01"
    result = build_session_report(**source)
    stock = next(row for row in result["stocks"] if row["symbol"] == "000001")
    assert stock["times"]["0932"]["amount"] is None
    assert "NO_0932_ASOF_ROW" in stock["flags"]


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
    assert "## 板块强度与 Kaipan 热板参考" in result["report_md"]
    assert "## 独立板块（净效应）" not in result["report_md"]


def test_strength_fields_keep_missing_values_and_report_no_baseline():
    result = build_session_report(**_source())
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    for tag in ("0920", "0924", "0925", "0932"):
        strength = bank["times"][tag]["strength"]
        assert strength["beta_ridge"] is None
        assert strength["conserved_share"] is None
        assert "UNKNOWN" in strength["flags"]
        assert "NO_BASELINE" in strength["flags"]
        assert strength["strength_rank"] == "flagged"


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
    strength = bank["times"]["0925"]["strength"]
    assert strength["baseline_share"] is not None
    assert strength["baseline_share"] == 1.0
    assert strength["share_vs_baseline"] is not None
    assert "NO_BASELINE" not in strength["flags"]
    assert bank["times"]["0932"]["strength"]["baseline_share"] is None


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
    assert "HOT_LIVE_UNAVAILABLE" not in bank["times"]["0925"]["strength"]["flags"]
    assert bank["times"]["0924"]["hot_live_rank"] == 3


def test_strength_rank_consistency_summary_is_present_per_stage():
    result = build_session_report(**_source())
    assert set(result["strength_hot_consistency"]) == {"0920", "0924", "0925", "0932"}
    assert result["strength_hot_consistency"]["0925"]["intersection_count"] == 0


def test_markdown_strength_units_flagged_rows_and_missing_values():
    result = build_session_report(**_source())
    bank = next(row for row in result["plates"] if row["plate"] == "银行")
    strength = bank["times"]["0925"]["strength"]
    strength.update({"beta_ridge": 0.0059, "beta_se": 0.0012, "excess_amt_weighted": 0.0059,
                     "excess_median": -0.001, "excess_trimmed": 0.002,
                     "conserved_share": 0.1298, "baseline_share": 0.1,
                     "share_vs_baseline": 0.0298, "top1_amount_share": None,
                     "flags": [], "strength_rank": 1})
    bank["times"]["0925"]["amount_share_of_market"] = 0.1298
    flagged = {**bank, "plate": "异常板块", "times": dict(bank["times"])}
    flagged["times"]["0925"] = {**bank["times"]["0925"], "strength": {
        **strength, "beta_ridge": 0.5, "flags": ["SINGLE_STOCK_DOMINATED"],
        "dominant_symbol": "000001", "strength_rank": "flagged"}}
    result["plates"].append(flagged)

    report = render_report_markdown(result)
    ranked = report.split("### 0925 板块强度", 1)[1].split("### 0925 Flagged", 1)[0]
    assert "×1.30" in ranked
    assert "12.98%" in ranked
    assert "—" in ranked
    assert "异常板块" not in ranked
    assert "异常板块" in report.split("### 0925 Flagged", 1)[1]
    assert "金额(亿元)" in report


def test_hot_day_list_shape_is_accepted():
    from examples.run_offline_plate_auction_open_report import _load_prev_hot
    import json, tempfile, pathlib

    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp) / "hot_plates"
        d.mkdir()
        (d / "2026-10-13.json").write_text(json.dumps(
            {"Day": ["2026-10-13"], "list": [["801660", "通信", 6780, 1.113]]}), encoding="utf-8")
        assert _load_prev_hot(pathlib.Path(tmp), "2026-10-13")["通信"]["rank"] == 1


def test_share_ratio_is_reported_as_multiple_and_keeps_legacy_fields():
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
    strength = bank["times"]["0925"]["strength"]
    result["report_md"] = render_report_markdown(result)
    assert strength["share_ratio_to_baseline"] is not None
    assert "×" in result["report_md"]


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
