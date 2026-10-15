import json
import csv
from datetime import date, timedelta

import pytest

from examples.render_live_pages import (_json_report_payload, _section_body, render_page,
                                        write_page)


def _history_csv(path, days=22):
    fields = ["date", "symbol", "open", "high", "low", "close", "volume", "amount_yuan", "turnover_pct", "pct_chg"]
    start = date(2026, 9, 1)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        previous_a = None
        for i in range(days):
            day = (start + timedelta(days=i)).isoformat()
            # A was limit-up on the last two prior sessions; B has a rising 20-day range.
            close_a = (previous_a * 1.1) if i >= days - 2 and previous_a is not None else 10 + i * .1
            previous_a = close_a
            for symbol, close, pct in (("000001", close_a, 10 if i >= days - 2 else 0),
                                       ("000002", 20 + i * .2, 0)):
                writer.writerow({"date": day, "symbol": symbol, "open": close * (1 + (-.01 if i == days - 1 else .01)),
                                 "high": close, "low": close * .9, "close": close,
                                 "volume": "", "amount_yuan": 1000000, "turnover_pct": "", "pct_chg": pct})


def _state(symbol, pct, amount, *, ts="09:25:00", limit_state=0, rb=0, ra=0):
    price = int(100000 * (1 + pct / 100))
    return {"symbol": symbol, "pct_change": pct, "match_amount_yuan": amount,
            "price_milli": price, "prev_close_milli": 100000,
            "rest_bid_shares": rb, "rest_ask_shares": ra,
            "rest_bid_yuan": rb * 100, "rest_ask_yuan": ra * 100,
            "limit_state": limit_state, "ts": ts}


def test_render_page_has_sections_and_self_contained_assets():
    td = type("TD", (), {"states_at": lambda self, cutoff: {}})()
    page, payload = render_page(td, trade_date="2026-10-15", stage="auction_0926", previous_day_final_yuan=None)
    for section in ("一屏结论", "大盘", "板块份额", "封涨停质量", "昨日连板高标", "竞价内变化", "看A做B", "近72日对比"):
        assert section in page
    assert "<style>" in page and "@media" in page
    assert payload["limitations"]


def test_write_page_refuses_nonempty_directory(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "existing").write_text("x")
    with pytest.raises(FileExistsError):
        write_page(output, "page", {})


def test_html_and_default_json_omit_snapshots_with_manifest(tmp_path):
    td = type("TD", (), {"states_at": lambda self, cutoff: {
        f"{i:06d}": _state(f"{i:06d}", i / 10, 1_000_000) for i in range(100)},
        "latest_tick_ts": lambda self: "2026-10-15T09:45:30"})()
    page, payload = render_page(td, trade_date="2026-10-15", stage="open_0946")
    assert "snapshots" not in page
    assert "\"snapshots\"" not in page
    assert len(page.encode("utf-8")) <= 200_000
    auction_page, _ = render_page(td, trade_date="2026-10-15", stage="auction_0926")
    assert len(auction_page.encode("utf-8")) <= 120_000

    default_dir = tmp_path / "default"
    write_page(default_dir, page, payload)
    default = json.loads((default_dir / "report.json").read_text(encoding="utf-8"))
    assert "snapshots" not in default
    assert (default_dir / "report.json").stat().st_size <= 300_000
    omitted = default["manifest"]["omitted_blocks"]["snapshots"]
    assert omitted["size_bytes"] > 0 and len(omitted["sha256"]) == 64

    full_dir = tmp_path / "full"
    write_page(full_dir, page, payload, full_json=True)
    full = json.loads((full_dir / "report.json").read_text(encoding="utf-8"))
    assert full["snapshots"]
    assert full["manifest"]["omitted_blocks"] == {}


def test_default_json_omits_behavior_rows_but_full_json_keeps_them(tmp_path):
    class TD:
        def states_at(self, cutoff):
            return {f"{i:06d}": _state(f"{i:06d}", (i % 7) - 3, 100_000 + i)
                    for i in range(80)}

    _, payload = render_page(TD(), trade_date="2026-10-15", stage="auction_0926")
    original_count = len(payload["chapters"]["behavior_rows"])
    default_dir, full_dir = tmp_path / "default", tmp_path / "full"
    write_page(default_dir, "", payload)
    write_page(full_dir, "", payload, full_json=True)
    default = json.loads((default_dir / "report.json").read_text(encoding="utf-8"))
    full = json.loads((full_dir / "report.json").read_text(encoding="utf-8"))

    assert "behavior_rows" not in default["chapters"]
    assert len(full["chapters"]["behavior_rows"]) == original_count
    assert default["chapters"]["behavior_tags"] == payload["chapters"]["behavior_tags"]
    assert (default_dir / "report.json").stat().st_size <= 300_000
    omitted = default["manifest"]["omitted_blocks"]["behavior_rows"]
    assert omitted["size_bytes"] > 0 and len(omitted["sha256"]) == 64
    assert omitted["row_count"] == original_count


def test_attention_watchlist_rules_and_phase_timings(tmp_path):
    history = tmp_path / "daily.csv"
    _history_csv(history)
    class TD:
        def states_at(self, cutoff):
            minute = cutoff.strftime("%H:%M")
            states = {f"{i:06d}": _state(f"{i:06d}", i % 10, 1_000_000 + i,
                                           limit_state=1 if i < 2 else 0, rb=100 if i < 2 else 0)
                      for i in range(60)}
            if minute == "09:24":
                states["000000"] = _state("000000", 0, 1_000)
            elif minute == "09:25":
                states["000000"] = _state("000000", 5, 1_000_001,
                                           limit_state=1, rb=100)
            return states

    _, payload = render_page(TD(), trade_date="2026-10-15", stage="auction_0926",
                             daily_history_csv=history)
    watchlist = payload["chapters"]["watchlist"]
    assert len(watchlist["amount_top50"]) == 50
    assert {row["symbol"] for row in watchlist["sealed_up"]} == {"000000", "000001"}
    assert any(row["symbol"] == "000001" for row in watchlist["yesterday_limit_pool"])
    assert len(watchlist["behavior_candidates"]) <= 30
    assert any(row["symbol"] == "000000" for row in watchlist["behavior_candidates"])
    timings = payload["manifest"]["timings_ms"]
    assert set(timings) == {"td_fetch", "aggregation", "render"}
    assert all(isinstance(value, (int, float)) and value >= 0 for value in timings.values())


def test_history_drives_conclusion_highboard_and_peer_sections(tmp_path):
    history = tmp_path / "daily.csv"
    _history_csv(history)
    class TD:
        def states_at(self, cutoff):
            minute = cutoff.strftime("%H:%M")
            p_a = -8 if minute < "09:32" else (-7 if minute < "09:40" else -9)
            return {"000001": _state("000001", p_a, 50000000, ts=cutoff.strftime("%H:%M:%S")),
                    "000002": _state("000002", -4 if minute < "09:32" else -2, 30000000, ts=cutoff.strftime("%H:%M:%S"))}
        def latest_tick_ts(self): return "2026-10-15T09:45:30"
    page, payload = render_page(TD(), trade_date="2026-10-15", stage="open_0946",
                                relations={"p1": {"000001", "000002"}}, plate_names={"p1": "测试板块"},
                                daily_history_csv=history, stock_names={"000001": "高标", "000002": "同伴"})
    assert payload["conclusion"] and 3 <= len(payload["conclusion"]) <= 5
    assert payload["history"]["median_is_historical_low"] is True
    assert payload["history"]["history_min_median_date"]
    assert payload["chapters"]["yesterday_highboards"][0]["streak"] == 2
    assert payload["chapters"]["yesterday_highboards"][0]["status"] in {"封死", "开板", "回封", "走弱", "观察"}
    assert payload["chapters"]["look_a_do_b"]
    assert "昨日连板高标" in page and "近72日对比" in page


def test_c155_behavior_tags_and_seal_quality_are_rendered(tmp_path):
    class TD:
        def states_at(self, cutoff):
            if cutoff.strftime("%H:%M") == "09:24":
                return {"000001": _state("000001", 1, 10000000), "000002": _state("000002", 0, 10000000)}
            return {"000001": _state("000001", 4, 50000000, limit_state=1, rb=100),
                    "000002": _state("000002", -2, 10000000)}
    page, payload = render_page(TD(), trade_date="2026-10-15", stage="auction_0926")
    assert payload["chapters"]["behavior_tags"]["direction_counts"]["价升份额升(抢筹候选)"] == 1
    assert payload["chapters"]["seal_quality"]["up_count"] == 1
    assert "价升份额升(抢筹候选)" in page and "封涨停质量" in page


def test_missing_history_is_explicit_and_cli_inputs_are_supported():
    class TD:
        def states_at(self, cutoff): return {}
    page, payload = render_page(TD(), trade_date="2026-10-15", stage="auction_0926")
    assert payload["history"] is None
    assert payload["chapters"]["look_a_do_b"] == "缺少输入"
    assert payload["chapters"]["yesterday_highboards"] == "缺少输入"
    assert "缺少输入" in page


def test_auction_baseline_is_shown_only_for_auction_stage(tmp_path):
    baseline = tmp_path / "auction.csv"
    baseline.write_text(
        "date,rows,sum_yi,top100_share,threshold_wan\n"
        "2026-10-01,10,100,50,100\n"
        "2026-10-02,10,200,50,100\n"
        "2026-10-03,10,200,50,100\n"
        "2026-10-15,10,900,50,100\n", encoding="utf-8")

    class TD:
        def states_at(self, cutoff):
            return {"000001": _state("000001", 1, 20_000_000_000,
                                     ts=cutoff.strftime("%H:%M:%S"))}
        def latest_tick_ts(self): return "2026-10-15T09:45:30"

    page, auction = render_page(TD(), trade_date="2026-10-15", stage="auction_0926",
                                auction_baseline_csv=baseline)
    comparison = auction["chapters"]["market_price_volume"]["auction_baseline_comparison"]
    assert comparison["multiple"] == 1
    assert comparison["percentile"] == pytest.approx(2 / 3 * 100)
    assert comparison["minimum_yi"] == 100 and comparison["maximum_yi"] == 200
    assert auction["manifest"]["inputs"]["auction_baseline"]["sample_days"] == 3
    assert auction["manifest"]["inputs"]["auction_baseline"]["earliest_date"] == "2026-10-01"
    assert "对64日中位" in page and "对64日分位" in page
    assert "历史分位" in auction["conclusion"][0]
    _, without_baseline = render_page(TD(), trade_date="2026-10-15", stage="auction_0926")
    payload_delta = (len(json.dumps(_json_report_payload(auction), ensure_ascii=False,
                                    separators=(",", ":"))) -
                     len(json.dumps(_json_report_payload(without_baseline), ensure_ascii=False,
                                    separators=(",", ":"))))
    assert payload_delta <= 5_000

    opening_page, opening = render_page(TD(), trade_date="2026-10-15", stage="open_0946",
                                        auction_baseline_csv=baseline)
    assert opening["chapters"]["market_price_volume"]["auction_baseline_comparison"] is None
    assert "累计成交额不与竞价额比较" in opening["conclusion"][0]
    assert "对64日中位" not in opening_page

    empty = tmp_path / "empty.csv"
    empty.write_text("date,rows,sum_yi,top100_share,threshold_wan\n", encoding="utf-8")
    _, unavailable = render_page(TD(), trade_date="2026-10-15", stage="auction_0926",
                                 auction_baseline_csv=empty)
    assert unavailable["chapters"]["market_price_volume"]["auction_baseline_comparison"] is None
    assert unavailable["manifest"]["inputs"]["auction_baseline"]["sample_days"] == 0
    assert "基线未提供" in unavailable["conclusion"][0]


def test_hot_plate_quadrants_and_zero_input_fallback(tmp_path):
    hot = tmp_path / "hot.json"
    hot.write_text(json.dumps({"captured_local": "2026-10-15 09:26:30", "response": {"list": [
        ["801001", "双正", 10, 2.0, 0, 2_100_000_000, 1, 3, -2, 0],
        ["801002", "双负", -10, -2.0, 0, 2_100_000_000, -3, 1, -2, 0],
        ["801003", "正流出", 10, 0.2, 0, 2_100_000_000, -1, 3, -2, 0],
        ["801004", "负流入", -10, -0.2, 0, 2_100_000_000, 1, -1, -2, 0],
        ["801005", "零值", 0, 0, 0, 2_100_000_000, 0, 0, 0, 0],
        *[[f"80{i:04d}", f"双负-{i}", -10, -2.0, 0, 2_100_000_000, -i, 1, -2, 0]
          for i in range(10, 27)],
    ]}}), encoding="utf-8")

    class TD:
        def states_at(self, cutoff):
            return {f"s{i:03}": _state(f"s{i:03}", (i % 5) - 2, 100_000_000,
                                         ts=cutoff.strftime("%H:%M:%S")) for i in range(20)}
    relations = {f"p{i}": {f"s{j:03}" for j in range(20)} for i in range(1, 6)}
    names = {f"p{i}": f"板{i}" for i in range(1, 6)}
    # Match supplied names to the captured Kaipan names.
    names = {f"p{i}": name for i, name in enumerate(("双正", "双负", "正流出", "负流入", "零值"), 1)}
    page, payload = render_page(TD(), trade_date="2026-10-15", stage="auction_0926",
                                relations=relations, plate_names=names, kaipan_hot_json=hot)
    block = payload["chapters"]["plate_quadrants"]
    assert block["available"] is True
    assert block["counts"] == {"双正": 1, "双负": 18, "强度正净流出背离": 1,
                                "强度负净流入背离": 1, "零值边界": 1}
    assert len(block["rows"]) == 22
    assert len(block["display_rows"]) == 18
    assert sum(row["quadrant"] == "双负" for row in block["display_rows"]) == 15
    assert next(row for row in block["display_rows"] if row["quadrant"] == "双负")["net_yuan"] == -26
    assert block["zero_value_boundary_count"] == 1
    assert all("pct_diff_pp" in row for row in block["display_rows"])
    assert block["rows"][0]["our_pct"] is not None and block["rows"][0]["pct_diff_pp"] is not None
    assert "09:26:30" in page and "描述" in page

    hot.write_text(json.dumps({"captured_local": "2026-10-15 09:21:00", "response": {"list": [
        ["801001", "双正", 0, 0, 0, 0, 0, 0, 0, 0]
    ]}}), encoding="utf-8")
    _, fallback = render_page(TD(), trade_date="2026-10-15", stage="auction_0926",
                              relations=relations, plate_names=names, kaipan_hot_json=hot)
    assert fallback["chapters"]["plate_quadrants"]["available"] is False
    assert fallback["chapters"]["plate_quadrants"]["fallback_equal_weight"]
    assert "Kaipan 强度不可用" in fallback["chapters"]["plate_quadrants"]["status"]


def test_strength_anchor_estimate_age_and_missing_chapters(tmp_path):
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"板块A": {"strength": 200, "pct": 1.0, "ts": "2026-10-15T09:20:00"}}), encoding="utf-8")
    class TD:
        def states_at(self, cutoff):
            return {f"s{i:03}": _state(f"s{i:03}", 2.0, 100_000_000,
                                         ts=cutoff.strftime("%H:%M:%S")) for i in range(20)}
    rel = {"p": {f"s{i:03}" for i in range(20)}}
    _, payload = render_page(TD(), trade_date="2026-10-15", stage="auction_0926",
                             relations=rel, plate_names={"p": "板块A"}, strength_anchor_json=anchor)
    block = payload["chapters"]["plate_quadrants"]
    assert block["strength_estimates"][0]["estimated_strength"] == 300
    assert block["strength_estimates"][0]["anchor_age_seconds"] == 306
    assert payload["manifest"]["inputs"]["strength_anchor"]["captured_local"] == "2026-10-15T09:20:00"
    _, missing = render_page(TD(), trade_date="2026-10-15", stage="auction_0926")
    assert missing["chapters"]["plate_quadrants"]["status"] == "Kaipan 强度不可用；未提供抓取输入"


def test_plate_enrichment_annex_states_and_default_json_size():
    relations = {"p": {f"s{i:03}" for i in range(5)},
                 "generic": {f"s{i:03}" for i in range(5)},
                 "universe": {f"s{i:03}" for i in range(100)}}
    names = {"p": "主题板块", "generic": "ST板块", "universe": "全体关系测试"}

    for emerging_count, expected_text in ((0, "无显著关联板块"),
                                           (2, "集合过小不检验"),
                                           (3, "仅称关联不称买点")):
        class TD:
            def states_at(self, cutoff):
                at_auction = cutoff.strftime("%H:%M") == "09:25"
                rows = {}
                for i in range(100):
                    pct = -1.0 if at_auction else (2.0 if i < emerging_count else -1.0)
                    rows[f"s{i:03}"] = _state(f"s{i:03}", pct, 100_000,
                                               ts=cutoff.strftime("%H:%M:%S"))
                return rows
            def latest_tick_ts(self): return "2026-10-15T09:45:30"

        _, payload = render_page(TD(), trade_date="2026-10-15", stage="open_0946",
                                 relations=relations, plate_names=names,
                                 member_snapshot_date="PIT 20261013")
        block = payload["chapters"]["plate_enrichment"]
        assert block["emerging_count"] == emerging_count
        assert expected_text in block["status"]
        assert block["member_snapshot_date"] == "PIT 20261013"
        annex_text = _section_body("竞价后新冒头", payload)
        assert "关联板块(富集反推, 仅关联不构成买点)" in annex_text
        assert expected_text in annex_text

        compact = json.dumps(payload["chapters"]["plate_enrichment"], ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        report = _json_report_payload(payload)
        without_annex = json.loads(json.dumps(report, ensure_ascii=False))
        without_annex["chapters"].pop("plate_enrichment")
        increment = (len(json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) -
                     len(json.dumps(without_annex, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
        assert len(compact) <= 20_000
        assert 0 < increment <= 20_000
        assert "plate_enrichment" in report["chapters"]
