import csv
import json
import pytest

from examples.run_offline_plate_auction_open_report import (
    _apply_auction_tick_prices,
    _apply_selection_stability,
    _attach_independent_plates,
    _build_revision_auction,
    _compare_revision_selections,
    _load_registry_dir,
    _merge_revision_stage,
    _compact_report_plates,
    _compact_report_stocks,
    _revision_should_render,
    _registry_point_in_time_flag,
    _render_revision_note,
    _load_stock_names,
    _load_stock_caps,
    _load_hot_captures,
    parse_open_times,
    validate_revision_cutoff,
)


def test_stock_names_loader_reports_hash_and_match_rate(tmp_path):
    path = tmp_path / "names.json"
    path.write_text(json.dumps({"000001": "平安银行", "000002": "万科A"}), encoding="utf-8")

    names, manifest = _load_stock_names(path, ["000001", "000003"])

    assert names == {"000001": "平安银行"}
    assert manifest["sha256"]
    assert manifest["matched_count"] == 1
    assert manifest["symbol_count"] == 2
    assert manifest["hit_rate"] == 0.5


def test_stock_caps_loader_reports_hash_hit_rate_and_missing_codes(tmp_path):
    path = tmp_path / "caps.json"
    path.write_text(json.dumps({"000001": {"float_cap": 1000, "total_cap": 2000}}), encoding="utf-8")

    caps, manifest = _load_stock_caps(path, ["000001", "000002"])

    assert caps == {"000001": {"float_cap": 1000.0, "total_cap": 2000.0}}
    assert manifest["sha256"]
    assert manifest["hit_rate"] == 0.5
    assert manifest["missing_count"] == 1


def test_hot_capture_loader_keeps_member_sum_amount_column(tmp_path):
    (tmp_path / "hot_0925.json").write_text(json.dumps({
        "captured_local": "2026-10-13T09:26:00+08:00",
        "response": {"Day": "2026-10-13", "list": [["801660", "通信", 6780, 0.4, 0, 3_444_000_000]]},
    }), encoding="utf-8")

    captures = _load_hot_captures(tmp_path)

    assert captures[0]["response"]["list"][0]["amount_yuan"] == 3_444_000_000


def test_registry_loader_accepts_list_registry_and_reports_asof_flag(tmp_path):
    (tmp_path / "registry.json").write_text(json.dumps([
        {"code": "801001", "name": "AI应用", "class": "THEME"},
    ]), encoding="utf-8")
    (tmp_path / "relation.csv").write_text(
        "plateid,stockid\n801001,000001\n", encoding="utf-8"
    )

    registry, members = _load_registry_dir(tmp_path)

    assert registry["801001"]["name"] == "AI应用"
    assert members == {"801001": {"000001"}}
    assert _registry_point_in_time_flag("2026-10-13", "2026-10-13") is True
    assert _registry_point_in_time_flag("2026-10-12", "2026-10-13") is False
    (tmp_path / "registry.json").write_text(
        json.dumps({"801002": {"name": "通信", "class": "THEME"}}), encoding="utf-8"
    )
    keyed_registry, _ = _load_registry_dir(tmp_path)
    assert keyed_registry["801002"]["name"] == "通信"


def test_independent_report_keeps_missing_returns_missing(tmp_path):
    rows = [{"code": "801001", "name": "AI应用", "class": "THEME"},
            {"code": "801002", "name": "事件板块", "class": "EVENT"}]
    (tmp_path / "registry.json").write_text(json.dumps({"plates": rows}), encoding="utf-8")
    with (tmp_path / "relation.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("plateid", "stockid"))
        writer.writerows(("801001", f"00000{i}") for i in range(1, 10))
        writer.writerow(("801002", "000001"))
    (tmp_path / "manifest.json").write_text(json.dumps({"as_of": "2026-10-13",
        "as_of_source": "csv_unknown"}), encoding="utf-8")

    stocks = []
    for i in range(1, 10):
        value = None if i == 9 else (i - 5) * 0.01
        stocks.append({
            "symbol": f"00000{i}",
            "times": {tag: {"pct_vs_pre_close": value, "limit_state": None,
                            "at_limit_price": i == 1, "rest_bid": None}
                      for tag in ("0920", "0924", "0925", "0932")},
        })
    report = {"stocks": stocks, "plates": [{"plate": "AI应用", "times": {
        tag: {"hot_live_rank": None, "hot_prev_close_rank": None}
        for tag in ("0920", "0924", "0925", "0932")
    }}]}

    independent_manifest = _attach_independent_plates(report, tmp_path, "2026-10-13", "2026-10-13")

    row = report["plates"][0]["times"]["0925"]["independent"]
    assert row["market"]["n"] == 8
    assert row["missing_count"] == 1
    assert row["uncovered"]["n"] <= 8
    assert "NOT_POINT_IN_TIME" in row["flags"]
    assert "REGISTRY_ASOF_UNVERIFIED" in independent_manifest["flags"]
    assert report["independent_by_tag"]["0925"]["event_plates"][0]["touch_limit_price_count"] == 1
    assert "limit_up_count" not in report["independent_by_tag"]["0925"]["event_plates"][0]


def test_api_members_registry_asof_before_day_has_no_independent_npt_flag(tmp_path):
    (tmp_path / "registry.json").write_text(json.dumps([{"code": "801001", "name": "主题",
        "class": "THEME"}]), encoding="utf-8")
    (tmp_path / "relation.csv").write_text("plateid,stockid\n801001,000001\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({"as_of": "2026-10-12",
        "as_of_source": "api_members"}), encoding="utf-8")
    report = {"stocks": [{"symbol": "000001", "times": {tag: {"pct_vs_pre_close": 0.01,
        "limit_state": None, "at_limit_price": False, "rest_bid": None}
        for tag in ("0920", "0924", "0925", "0932")}}], "plates": [{"plate": "主题",
            "times": {tag: {} for tag in ("0920", "0924", "0925", "0932")}}]}

    manifest = _attach_independent_plates(report, tmp_path, None, "2026-10-13")

    assert "NOT_POINT_IN_TIME" not in manifest["flags"]
    assert all("NOT_POINT_IN_TIME" not in row["flags"]
               for row in report["independent_by_tag"].values())


def test_revision_omitted_without_indicative_prices_and_compact_payloads_keep_full_row_values():
    assert _revision_should_render({"pre_final_indicative_count": 1})
    assert not _revision_should_render({"pre_final_indicative_count": 0})
    stocks = [{"symbol": f"0000{i:02d}", "primary_plate": "主题", "times": {"0932": {
        "limit_state": None, "pct_vs_pre_close": 0.02, "amount": 100}}, "labels": {}}
        for i in range(1, 102)]
    selected_stock = {"symbol": "000002", "primary_plate": "主题", "times": {"0932": {
        "limit_state": 1, "pct_vs_pre_close": 0.1, "amount": 1000}}, "labels": {}}
    stocks[1] = selected_stock
    compact_stocks = _compact_report_stocks(stocks)
    assert compact_stocks[100] == {"symbol": "0000101", "primary_plate": "主题", "final_label": None}
    assert compact_stocks[1] == selected_stock

    full_plate = {"plate": "主题", "category": "THEME", "priced_member_count": 5,
        "member_count": 5, "times": {"0925": {"amount_sum": 88.0}}}
    thin_plate = {"plate": "微型", "category": "THEME", "priced_member_count": 2,
        "member_count": 2, "times": {"0925": {"amount_sum": 1.0}}}
    compact_plates = _compact_report_plates([full_plate, thin_plate])
    assert compact_plates[0]["times"]["0925"]["amount_sum"] == 88.0
    assert compact_plates[1]["detail_omitted"] is True
    assert "SMALL_OR_THIN" in compact_plates[1]["quality_flags"]


def test_no_late_revision_note_omits_revision_section():
    text = _render_revision_note({"no_late_finals": True, "revision_summary": {
        "price_changed_count": 0}, "auction_price_note": "终价 5 / 指示价 0"})

    assert "无迟到终价, 0925R 与 0925 相同" in text
    assert "## 0925 → 0925R 修订对比" not in text


def test_open_times_default_parse_and_validation():
    assert parse_open_times("0932,0935,0940,0945") == ("0932", "0935", "0940", "0945")
    with pytest.raises(ValueError):
        parse_open_times("0932,0945,0935")
    with pytest.raises(ValueError):
        parse_open_times("932")


def test_stability_counts_contiguous_selection_and_adjacent_jaccard():
    data = {
        "0920": {"selected": [{"code": "A", "direction": "STRONG"}, {"code": "B"}]},
        "0924": {"selected": [{"code": "A", "direction": "WEAK"}, {"code": "C"}]},
        "0925": {"selected": [{"code": "A", "direction": "WEAK"}]},
        "0932": {"selected": [{"code": "A", "direction": "STRONG"}]},
        "0935": {"selected": [{"code": "A", "direction": "WEAK"}]},
    }
    _apply_selection_stability(data, tuple(data))
    assert data["0925"]["selected"][0]["consecutive_selected"] == 3
    assert data["0925"]["selected"][0]["previous_direction"] == "WEAK"
    assert data["0924"]["stability"]["previous_jaccard"] == 1 / 3
    assert data["0935"]["selected"][0]["consecutive_selected"] == 2
    assert data["0932"]["stability"]["previous_jaccard"] is None


def test_auction_price_uses_last_tick_at_or_before_snapshot_cutoff_and_counts_disagreement():
    auctions = {"0925": {
        "000001": {"ts": "2026-10-14 09:25:06", "px_milli": 10000},
        "000002": {"ts": "2026-10-14 09:25:06", "px_milli": None},
    }}
    ticks = {"0925": [
        {"symbol": "000001", "ts": "2026-10-14 09:25:02", "px_milli": 9990,
         "bp1_milli": 9990, "ap1_milli": 9990},
        {"symbol": "000001", "ts": "2026-10-14 09:25:07", "px_milli": 12000,
         "bp1_milli": 12000, "ap1_milli": 12000},
        {"symbol": "000002", "ts": "2026-10-14 09:24:57", "px_milli": None,
         "bp1_milli": 9900, "ap1_milli": 9900},
        {"symbol": "000002", "ts": "2026-10-14 09:25:07", "px_milli": 12000,
         "bp1_milli": 12000, "ap1_milli": 12000},
    ]}

    meta = _apply_auction_tick_prices(auctions, ticks)

    assert auctions["0925"]["000001"]["px_milli"] == 10000
    assert auctions["0925"]["000001"]["price_source"] == "SNAPSHOT_PX"
    assert auctions["0925"]["000002"]["px_milli"] == 9900
    assert auctions["0925"]["000002"]["price_flags"] == ["PRE_FINAL_INDICATIVE"]
    assert meta["0925"]["price_source_disagree"] == 1
    assert meta["0925"]["source_counts"]["TICK_BOOK1_INDICATIVE"] == 1


def test_auction_price_caps_snapshot_cutoff_at_system_boundary():
    auctions = {"0925": {"000001": {"ts": "2026-10-14 09:25:10", "px_milli": None}}}
    ticks = {"0925": [
        {"symbol": "000001", "ts": "2026-10-14 09:25:06", "bp1_milli": 10000, "ap1_milli": 10000},
        {"symbol": "000001", "ts": "2026-10-14 09:25:07", "bp1_milli": 12000, "ap1_milli": 12000},
    ]}

    meta = _apply_auction_tick_prices(auctions, ticks)

    assert auctions["0925"]["000001"]["px_milli"] == 10000
    assert meta["0925"]["input_cutoff"] == "2026-10-14 09:25:06+08:00"
    assert meta["0925"]["cutoff_capped"] is True
    assert meta["0925"]["raw_snapshot_max_ts"] == "2026-10-14 09:25:10+08:00"


def test_revision_stage_does_not_change_base_auction_or_opening_prices():
    report = {"market": {"0925": {"covered_count": 1}}, "stocks": [{"symbol": "000001",
        "times": {"0925": {"price": 10.0}, "0932": {"price": 11.0}}}], "plates": [],
        "activity_manifest": {"0925": {"baseline_dates": ["2026-10-08", "2026-10-09"],
                                          "baseline_days": 2, "baseline_short_plates": 1}},
        "input_cutoff": {"0925": "2026-10-14T09:25:06", "0932": "2026-10-14T09:32:00"}}
    revised = {"market": {"0925": {"covered_count": 1}}, "stocks": [{"symbol": "000001",
        "times": {"0925": {"price": 12.0, "amount_kind": "MATCH"}}}], "plates": []}

    _merge_revision_stage(report, revised, {})

    assert report["stocks"][0]["times"]["0925"]["price"] == 10.0
    assert report["stocks"][0]["times"]["0932"]["price"] == 11.0
    assert report["stocks"][0]["times"]["0925R"]["price"] == 12.0
    assert report["input_cutoff"]["0925"] == "2026-10-14T09:25:06"
    assert report["activity_manifest"]["0925R"] == report["activity_manifest"]["0925"]


def test_snapshot_price_mode_preserves_old_missing_coverage():
    auctions = {"0920": {"000001": {"ts": "2026-10-14 09:20:03", "px_milli": None}}}
    ticks = {"0920": [{"symbol": "000001", "ts": "2026-10-14 09:20:02",
                       "px_milli": None, "bp1_milli": 10000, "ap1_milli": 10000}]}

    meta = _apply_auction_tick_prices(auctions, ticks, "snapshot_px")

    assert auctions["0920"]["000001"]["px_milli"] is None
    assert auctions["0920"]["000001"]["price_source"] == "NO_EFFECTIVE_PRICE"
    assert meta["0920"]["source_counts"]["NO_EFFECTIVE_PRICE"] == 1


def test_revision_cutoff_requires_strictly_after_boundary_and_before_open():
    assert validate_revision_cutoff("09:27:10") == "09:27:10"
    for cutoff in ("09:25:06", "09:25:00", "09:30:00"):
        with pytest.raises(ValueError):
            validate_revision_cutoff(cutoff)


def test_revision_changes_only_pre_final_indicative_rows_and_respects_cutoff():
    auction = {"0925": {
        "000001": {"px_milli": 10000, "price_source": "SNAPSHOT_PX", "price_flags": []},
        "000002": {"px_milli": 9900, "price_source": "TICK_BOOK1_INDICATIVE",
                   "price_flags": ["PRE_FINAL_INDICATIVE"]},
    }}
    ticks = [
        {"symbol": "000002", "ts": "2026-10-14 09:26:00", "px_milli": 10100,
         "bp1_milli": 10100, "ap1_milli": 10100},
        {"symbol": "000002", "ts": "2026-10-14 09:27:11", "px_milli": 11000,
         "bp1_milli": 11000, "ap1_milli": 11000},
        {"symbol": "000001", "ts": "2026-10-14 09:26:00", "px_milli": 12000,
         "bp1_milli": 12000, "ap1_milli": 12000},
    ]
    revised, meta = _build_revision_auction(auction, ticks, "2026-10-14", "09:27:10")
    assert revised["0925"]["000001"]["px_milli"] == 10000
    assert revised["0925"]["000002"]["px_milli"] == 10100
    assert revised["0925"]["000002"]["price_source"] == "TICK_PX_FINAL"
    assert meta["revised_count"] == 1
    assert meta["input_cutoff"] == "2026-10-14T09:27:10"


def test_revision_selection_comparison_reports_set_changes_and_jaccard():
    before = {"selected": [{"code": "A", "direction": "STRONG"}, {"code": "B", "direction": "WEAK"}]}
    after = {"selected": [{"code": "A", "direction": "WEAK"}, {"code": "C", "direction": "STRONG"}]}
    result = _compare_revision_selections(before, after)
    assert result["added"] == ["C"]
    assert result["removed"] == ["B"]
    assert result["direction_changed"] == ["A"]
    assert result["jaccard"] == 1 / 3


def test_markdown_has_revision_boundary_and_terminal_indicative_counts():
    markdown = _render_revision_note({
        "auction_price_note": "终价 93 只 / 指示价(终价迟到) 7 只(深市占 100%), 预计 09:26~09:27 到达",
        "revision_summary": {"price_changed_count": 7},
    })
    assert "终价 93 只 / 指示价(终价迟到) 7 只(深市占 100%)" in markdown
    assert "0925 是 09:25:06 的实时结论" in markdown
