import json

import pytest

from engine_core.daily_pipeline import (DailySources, _attach_range_positions, _fact_auction_amount, _load_ladder_summary,
                                        discover_references, run_daily_pipeline)


def test_zero_auction_amount_does_not_fall_back_to_another_source():
    assert _fact_auction_amount({"auction_match_amount_yuan": 0,
                                 "p0925_cumulative_amount_yuan": 123}) == 0
    assert _fact_auction_amount({"auction_match_amount_yuan": None,
                                 "p0925_cumulative_amount_yuan": 123}) == 123


def test_auction_assembly_switch_defaults_to_v6_and_can_restore_legacy(monkeypatch):
    from engine_core.daily_pipeline import _auction_assembly_mode

    monkeypatch.delenv("CORE_AUCTION_ASSEMBLY", raising=False)
    assert _auction_assembly_mode() == "v6"
    monkeypatch.setenv("CORE_AUCTION_ASSEMBLY", "legacy")
    assert _auction_assembly_mode() == "legacy"
    monkeypatch.setenv("CORE_AUCTION_ASSEMBLY", "invalid")
    with pytest.raises(ValueError, match="CORE_AUCTION_ASSEMBLY"):
        _auction_assembly_mode()


def test_v6_auction_inputs_are_minimal_and_preserve_missing_values():
    from engine_core.auction_v6_inputs import prepare_auction_v6_inputs

    payload = prepare_auction_v6_inputs(
        trade_date="2026-10-16", asof="2026-10-16T09:26:00+08:00",
        stock_facts=[{"symbol": "000001", "auction_amount_yuan": None,
                      "pct_change": None, "valid_price": False,
                      "seal": {"status": "UNKNOWN"}}],
        stock_names={"000001": "测试股"})
    assert payload["chapters"] == {}
    assert payload["stocks"][0]["name"] == "测试股"
    assert payload["stocks"][0]["auction_amount_yuan"] is None
    assert payload["snapshots"][0]["market_match_amount_yuan"] is None


def test_live_auction_fact_td_reader_uses_asof_window_and_maps_real_columns():
    from engine_core.daily_pipeline import _read_live_auction_fact_rows

    class Cursor:
        def __init__(self): self.calls = []; self.description = []
        def execute(self, sql):
            self.calls.append(sql)
            columns = (("ts", "symbol", "px_milli", "chg_bp", "match_amt_yuan",
                "rest_bid_amt_yuan", "rest_ask_amt_yuan", "limit_state", "trade_date", "auction_tag")
                if "auction_snapshot_v2" in sql else
                ("ts", "px_milli", "amt_yuan", "pc_milli", "bp1_milli", "bv1", "symbol"))
            self.description = [(name,) for name in columns]
        def fetchall(self):
            if "auction_snapshot_v2" in self.calls[-1]:
                return [("2026-10-16 09:25:06.112", "002815", 25580, 1002, 1234,
                         0, 0, 1, "20261016", "0925")]
            return [("2026-10-16 09:26:00.000", 25580, 1234, 23250, 25580, 350287, "002815")]
        def close(self): pass

    class TD:
        def __init__(self): self.c = Cursor()
        def cursor(self): return self.c

    td = TD()
    snapshots, ticks = _read_live_auction_fact_rows(
        td, "2026-10-16", "2026-10-16T09:26:00+08:00")
    assert len(snapshots) == len(ticks) == 1
    assert snapshots[0]["auction_amount_yuan"] == 1234
    assert snapshots[0]["price_milli"] == 25580
    assert ticks[0]["bv1"] == 350287
    assert snapshots[0]["pre_close_milli"] == 23250
    assert all("09:26:00" in sql for sql in td.c.calls)
    assert all("09:25:00" in sql for sql in td.c.calls if "stock_tick_v2" in sql)


def test_live_report_shrink_keeps_original_dictionary_omission_hash_idempotently():
    from engine_core.daily_pipeline import _shrink_live_report
    report = {"plate_dictionary": {"rows": [{"code": "A"}, {"code": "B"}]}}
    _shrink_live_report(report)
    first = report["omitted_blocks"][-1].copy()
    _shrink_live_report(report)
    assert first["rows"] == 2
    assert report["omitted_blocks"][-1] == first


def test_d06_audit_only_product_is_never_consumed():
    from engine_core.daily_pipeline import _d06_persisted_assignments
    from engine_core.slices import SliceResult

    row = {"symbol": "600001", "trade_date": "2026-10-16", "used_date": "2026-10-15",
           "as_of": "2026-10-15", "input_version": "inputs-v1",
           "visibility_sha256": "visible-v1", "primary_theme": {"id": "801001"}}
    result = SliceResult.create([row], {"slice_id": "stock_theme_assignment", "status": "READY",
        "pit_date": "2026-10-15", "available_at": "2026-10-16T08:50:00+08:00"})
    kwargs = dict(trade_date="2026-10-16", used_date="2026-10-15",
                  asof="2026-10-16T09:32:00+08:00", input_version="inputs-v1",
                  visibility_sha256="visible-v1")
    assert _d06_persisted_assignments(result, **kwargs) == {}
    assert _d06_persisted_assignments(result, **{**kwargs, "input_version": "inputs-v2"}) == {}
    assert _d06_persisted_assignments(result, **{**kwargs, "visibility_sha256": "visible-v2"}) == {}
    assert _d06_persisted_assignments(result, **{**kwargs, "used_date": "2026-10-14"}) == {}


class FakeTD:
    def states_at(self, cutoff):
        return {}

    def checkpoint_ready(self, cutoff):
        return False

    def latest_tick_ts(self):
        return None


class FakeRedis:
    def __init__(self):
        self.writes = []

    def scan_iter(self, **kwargs):
        return iter(())

    def set(self, key, value, **kwargs):
        self.writes.append((key, value, kwargs))


def test_a_to_b_range_position_uses_only_twenty_prior_closes_and_keeps_missing_unknown(tmp_path):
    path = tmp_path / "history.csv"
    rows = [f"2026-09-{i:02d},600001,{i}" for i in range(1, 21)]
    path.write_text("date,symbol,close\n" + "\n".join(rows) + "\n2026-10-15,600001,999\n")
    stocks = [{"symbol": "600001", "p0925_pct": 0}, {"symbol": "600002", "p0925_pct": 1}]
    _attach_range_positions(stocks, path, "2026-10-15")
    assert stocks[0]["prior_range_position20_pct"] == 100
    assert "prior_range_position20_pct" not in stocks[1]


def test_final_live_report_projection_shrinks_after_basis_notes_are_added():
    from engine_core.daily_pipeline import _shrink_live_report

    report = {"stage": "auction", "basis_notes": ["legacy_redis"] * 100,
              "chapters": {"plate_quadrants": {"rows": [{"text": "大" * 1000} for _ in range(100)]}},
              "snapshots": [{"text": "大" * 1000} for _ in range(400)]}
    report["slice_data_basis"] = ["legacy_redis"]
    projected = {key: value for key, value in report.items() if key != "stocks"}
    _shrink_live_report(projected)
    text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    assert len(text.encode()) < 250_000
    assert "slice_data_basis" in json.loads(text)


def test_reference_discovery_prefers_core_redis_pointer_and_records_version(tmp_path):
    class ReferenceRedis:
        def __init__(self, values): self.values = values
        def get(self, key): return self.values.get(key)

    redis_path = tmp_path / "redis-stock-names.json"
    redis_path.write_text('{"600001":"Redis名称"}', encoding="utf-8")
    catalog_path = tmp_path / "catalog-stock-names.json"
    catalog_path.write_text('{"600001":"Catalog名称"}', encoding="utf-8")
    (tmp_path / "reference_catalog.json").write_text(json.dumps({"references": {
        "stock_names": {"path": str(catalog_path), "as_of": "2026-10-14"}}}), encoding="utf-8")
    redis = ReferenceRedis({"core:cache:data:reference:stock_names:2026-10-15": json.dumps({
        "path": str(redis_path), "as_of": "2026-10-14", "version": "sha256-abc"})})
    from engine_core.daily_pipeline import resolve_references

    paths, metadata = resolve_references(tmp_path, "2026-10-15", redis=redis)
    assert paths["stock_names"] == redis_path
    assert metadata["stock_names"] == {"as_of": "2026-10-14", "discovered_by": "redis",
                                        "version": "sha256-abc"}


def test_reference_discovery_rejects_same_day_redis_input_before_close(tmp_path):
    class ReferenceRedis:
        def __init__(self, values): self.values = values
        def get(self, key): return self.values.get(key)

    redis_path = tmp_path / "same-day-stock-names.json"
    redis_path.write_text("{}", encoding="utf-8")
    catalog_path = tmp_path / "prior-stock-names.json"
    catalog_path.write_text("{}", encoding="utf-8")
    (tmp_path / "reference_catalog.json").write_text(json.dumps({"references": {
        "stock_names": {"path": str(catalog_path), "as_of": "2026-10-14"}}}), encoding="utf-8")
    redis = ReferenceRedis({"core:cache:data:reference:stock_names:2026-10-15": json.dumps({
        "path": str(redis_path), "as_of": "2026-10-15", "version": "same-day"})})
    from engine_core.daily_pipeline import resolve_references

    auction, auction_meta = resolve_references(tmp_path, "2026-10-15", stage="auction", redis=redis)
    close, close_meta = resolve_references(tmp_path, "2026-10-15", stage="close", redis=redis)
    assert auction["stock_names"] == catalog_path
    assert auction_meta["stock_names"]["discovered_by"] == "catalog"
    assert close["stock_names"] == redis_path
    assert close_meta["stock_names"]["discovered_by"] == "redis"


def test_live_section_stocks_builds_point_in_time_rows_without_close_fields():
    from engine_core.daily_pipeline import _live_section_stocks

    class States:
        def states_at(self, cutoff):
            if cutoff.strftime("%H:%M:%S") == "09:25:06":
                return {"600001": {"symbol": "600001", "pct_change": 10.0,
                                    "match_amount_yuan": 1000, "status": "FINAL"},
                        "600002": {"symbol": "600002", "pct_change": 2.0,
                                    "match_amount_yuan": 500, "status": "FINAL"}}
            if cutoff.strftime("%H:%M:%S") == "09:32:00":
                return {"600001": {"pct_change": 9.0}, "600002": {"pct_change": 3.0}}
            return {"600001": {"pct_change": 8.0}, "600002": {"pct_change": 4.0}}

    rows = _live_section_stocks(States(), trade_date="2026-10-15", stage="open",
        asof="09:46:00", relations={"801001": ["600001", "600002"]},
        plate_names={"801001": "通信"}, stock_names={}, history_path=None)
    assert rows[0]["main_plate"] == "通信"
    assert rows[0]["p0925_pct"] == 10.0
    assert rows[0]["p0945_pct"] == 8.0
    assert rows[0]["auction_amount_bp"] == pytest.approx(6666.6667)
    assert all("close_pct" not in row for row in rows)


def test_live_section_stocks_transmits_cutoff_prices_for_exact_limit_proxy():
    from engine_core.daily_pipeline import _live_section_stocks
    from engine_core.ladder_cluster import _price_label

    class States:
        def states_at(self, cutoff):
            return {"600001": {"symbol": "600001", "pct_change": 9.9, "match_amount_yuan": 100,
                                "price_milli": 11000, "prev_close_milli": 10000},
                    "600002": {"symbol": "600002", "pct_change": 9.99, "match_amount_yuan": 100,
                                "price_milli": 10990, "prev_close_milli": 10000}}

    rows = _live_section_stocks(States(), trade_date="2026-10-15", stage="auction", asof="09:26:00",
                                relations={}, plate_names={}, stock_names={}, history_path=None)
    by_symbol = {row["symbol"]: row for row in rows}
    assert _price_label(by_symbol["600001"]["p0925_pct"], "600001", by_symbol["600001"]["price"],
                        by_symbol["600001"]["pre_close"]) == "封停价代理"
    assert _price_label(by_symbol["600002"]["p0925_pct"], "600002", by_symbol["600002"]["price"],
                        by_symbol["600002"]["pre_close"]) == "高开"


def test_live_report_json_drops_5200_full_universe_rows_under_size_limit():
    import json
    from engine_core import live_pages
    payload = {"stage": "auction_0926", "chapters": {"behavior_rows": [
        {"symbol": f"{i:06d}", "name": "n" * 20, "pad": "x" * 900} for i in range(5200)]}}
    result = live_pages._json_report_payload(payload)
    assert "behavior_rows" not in result["chapters"]
    assert len(json.dumps(result, ensure_ascii=False)) < 250_000


def test_live_section_stocks_excludes_broad_plate_from_main_plate():
    from engine_core.daily_pipeline import _live_section_stocks

    class States:
        def states_at(self, cutoff):
            return {"600001": {"pct_change": 1, "match_amount_yuan": 10}}

    rows = _live_section_stocks(States(), trade_date="2026-10-15", stage="auction",
        asof="09:26:00", relations={"801019": ["600001"], "801001": ["600001"]},
        plate_names={"801019": "物流", "801001": "芯片"}, stock_names={}, history_path=None,
        plate_metadata={"物流": {"class": "BROAD", "size": 297},
                        "芯片": {"class": "THEME", "size": 40}})
    assert rows[0]["main_plate"] == "芯片"


def test_live_section_stocks_selects_main_plate_once_per_symbol(monkeypatch):
    import engine_core.a_to_b as a_to_b
    from engine_core.daily_pipeline import _live_section_stocks

    calls = []
    original = a_to_b.select_main_plate

    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(a_to_b, "select_main_plate", counted)

    class States:
        def states_at(self, cutoff):
            return {"600001": {"pct_change": 1.0, "match_amount_yuan": 10}}

    rows = _live_section_stocks(States(), trade_date="2026-10-15", stage="auction",
        asof="09:26:00", relations={"801001": ["600001"]},
        plate_names={"801001": "通信"}, stock_names={}, history_path=None)
    assert rows[0]["main_plate"] == "通信"
    assert len(calls) == 1


def test_live_section_stocks_does_not_request_future_checkpoints():
    from engine_core.daily_pipeline import _live_section_stocks

    class States:
        def __init__(self): self.cutoffs = []
        def states_at(self, cutoff):
            self.cutoffs.append(cutoff.strftime("%H:%M:%S"))
            return {"600001": {"pct_change": 1.0, "match_amount_yuan": 10}}

    source = States()
    _live_section_stocks(source, trade_date="2026-10-15", stage="open", asof="09:33:00",
                          relations={}, plate_names={}, stock_names={}, history_path=None)
    assert source.cutoffs == ["09:24:10", "09:25:06", "09:32:00"]


def test_live_section_stocks_includes_0945_at_exact_094530_cutoff():
    from engine_core.daily_pipeline import _live_section_stocks

    class States:
        def __init__(self): self.cutoffs = []
        def states_at(self, cutoff):
            self.cutoffs.append(cutoff.strftime("%H:%M:%S"))
            return {"600001": {"pct_change": 1.0, "match_amount_yuan": 10,
                               "amt_yuan": 20}}

    source = States()
    rows = _live_section_stocks(source, trade_date="2026-10-15", stage="open", asof="09:45:30",
                                relations={}, plate_names={}, stock_names={}, history_path=None)
    assert source.cutoffs == ["09:24:10", "09:25:06", "09:32:00", "09:35:00",
                             "09:38:00", "09:40:00", "09:45:30"]
    assert rows[0]["p0945_pct"] == 1.0
    assert rows[0]["p0945_amount_yuan"] == 20


def test_live_section_stocks_keeps_0945_missing_at_094529():
    from engine_core.daily_pipeline import _live_section_stocks

    class States:
        def __init__(self): self.cutoffs = []
        def states_at(self, cutoff):
            self.cutoffs.append(cutoff.strftime("%H:%M:%S"))
            return {"600001": {"pct_change": 1.0, "match_amount_yuan": 10,
                               "amt_yuan": 20}}

    source = States()
    rows = _live_section_stocks(source, trade_date="2026-10-15", stage="open", asof="09:45:29",
                                relations={}, plate_names={}, stock_names={}, history_path=None)
    assert "09:45:30" not in source.cutoffs
    assert rows[0]["p0945_pct"] is None
    assert rows[0]["p0945_amount_yuan"] is None
    assert rows[0]["current_checkpoint"] == "0940"


@pytest.mark.parametrize(("asof", "label", "price"), [("09:32:10", "0932", 10.32),
                                                          ("09:38:10", "0938", 10.38)])
def test_live_section_stocks_current_price_and_limit_state_use_asof_checkpoint(asof, label, price):
    from engine_core.daily_pipeline import _live_section_stocks

    class States:
        def states_at(self, cutoff):
            clock = cutoff.strftime("%H:%M:%S")
            if clock == "09:25:06":
                return {"600001": {"pct_change": 1, "price_milli": 10_000,
                                    "prev_close_milli": 10_000, "rest_bid_shares": 0,
                                    "rest_ask_shares": 0, "amt_yuan": 100}}
            current = 10.32 if clock == "09:32:00" else 10.38 if clock == "09:38:00" else 10.45
            return {"600001": {"pct_change": current - 10, "price_milli": round(current * 1000),
                                "prev_close_milli": 10_000, "rest_bid_shares": 10 if clock == "09:32:00" else 0,
                                "rest_ask_shares": 0, "amt_yuan": 500}}

    rows = _live_section_stocks(States(), trade_date="2026-10-16", stage="open", asof=asof,
                                relations={}, plate_names={}, stock_names={}, history_path=None)
    row = rows[0]
    assert row["price"] == price
    assert row["open_limit_state"] == 0
    assert row.get("p0945_pct") is None
    assert row.get(f"p{label}_pct") is not None


def test_ladder_summary_uses_dated_daily_rows_and_marks_close_price_proxy(tmp_path):
    # The pipeline now receives the versioned path_stats slice instead of
    # discovering ladder_daily.csv beneath a reference directory.
    rows = _load_ladder_summary([{"date": "2026-10-15", "phase_eod": "ICE",
                                  "max_price_streak": "5", "top_symbols": "002343|002696",
                                  "previous_space_height": "10", "previous_space_symbols": "600825",
                                  "board_1_n": "30", "previous_limit_n": "38", "promoted_n": "6",
                                  "promotion_rate": "0.5", "previous_1_n": "22", "promoted_1_n": "2",
                                  "broken_1_n": "19", "promotion_1_rate": "0.09"}], "2026-10-15")
    assert rows[0]["top_symbols"] == "002343|002696"
    assert rows[0]["first_board_count"] == "30"
    assert rows[1]["promoted_n"] == "2"
    assert "炸板路径 UNKNOWN" in rows[1]["status"]
    assert _load_ladder_summary(tmp_path, "2026-10-14") == []


def test_yesterday_streaks_preserve_missing_as_missing():
    from engine_core.daily_pipeline import _prior_streak, _yesterday_lb_days
    report = {"stocks": [{"symbol": "600001", "lb_days": 4}, {"symbol": "600002"}],
              "chapters": {"yesterday_limit_pool": [{"code": "000001", "streak": 2}]}}
    assert _yesterday_lb_days(report) == {"600001": 4, "000001": 2}
    assert _prior_streak("600001", _yesterday_lb_days(report)) == 4
    assert _prior_streak("600002", _yesterday_lb_days(report)) is None


def test_daily_pipeline_json_reader_accepts_real_same_day_and_previous_reports():
    from pathlib import Path
    from engine_core.daily_pipeline import _read_optional_json

    # Copied from C341's real TD runs: /tmp/c341-20261020/close-1019/.../report.json
    # and /tmp/c341-20261020/close-1016/.../report.json.
    fixtures = Path(__file__).parent / "fixtures" / "daily_pipeline" / "c343"
    degraded = []
    same_day = _read_optional_json(fixtures / "close-2026-10-19.json", degraded, "SAME_DAY_REPORT")
    previous = _read_optional_json(fixtures / "close-2026-10-16.json", degraded, "PREVIOUS_REPORT")

    assert same_day["trade_date"] == "2026-10-19"
    assert previous["trade_date"] == "2026-10-16"
    assert degraded == []


def test_daily_pipeline_json_reader_degrades_corrupt_previous_report(tmp_path):
    from engine_core.daily_pipeline import _read_optional_json

    corrupt = tmp_path / "report.json"
    corrupt.write_text('{"trade_date":', encoding="utf-8")
    degraded = []

    assert _read_optional_json(corrupt, degraded, "PREVIOUS_REPORT") is None
    assert degraded == ["INVALID_REFERENCE_JSON:PREVIOUS_REPORT"]

    missing = _read_optional_json(tmp_path / "missing.json", degraded, "SAME_DAY_REPORT")
    assert missing is None
    assert degraded == ["INVALID_REFERENCE_JSON:PREVIOUS_REPORT",
                        "MISSING_REFERENCE_JSON:SAME_DAY_REPORT"]


def test_three_stages_write_reports_and_explicit_missing_references(tmp_path, monkeypatch):
    from engine_core.slices import SliceReader, SliceResult

    references = tmp_path / "references"
    output = tmp_path / "reports"
    slice_ids = ("stock_name_map", "plate_registry", "plate_members", "daily_history",
                 "auction_baseline", "yest_limit_pool", "limit_reason_history",
                 "hot_plates_today", "hot_plates_yesterday", "hot_rank",
                 "trading_calendar", "path_stats")
    calls = []

    def fixture_provider(reader, request):
        calls.append(request.slice_id)
        if request.slice_id == "stock_name_map":
            return SliceResult.create([], {
                "slice_id": request.slice_id, "trade_date": request.trade_date,
                "asof": request.asof, "source": "fixture", "version": "test-v1",
                "built_at": "2026-10-15T09:00:00+08:00", "available_at": None,
                "quality": {"coverage": None, "missing": "FIXTURE_MISSING", "partial": False},
                "status": "MISSING", "lineage": []})
        return SliceResult.create([], {
            "slice_id": request.slice_id, "trade_date": request.trade_date,
            "asof": request.asof, "source": "fixture", "version": "test-v1",
            "built_at": "2026-10-15T09:00:00+08:00", "available_at": None,
            "quality": {"coverage": 0, "missing": "FIXTURE_EMPTY", "partial": True},
            "status": "PARTIAL", "lineage": []})

    slices = SliceReader({name: [fixture_provider] for name in slice_ids})
    sources = DailySources(td=FakeTD(), reference_root=references, slice_reader=slices)
    for stage in ("auction", "open", "close"):
        if stage == "open":
            from engine_core import live_session
            original_check = live_session.assert_report_checkpoint_invariant
            injected = False

            def inject_future_asof(report, checkpoint):
                nonlocal injected
                if not injected:
                    report.setdefault("chapters", {}).setdefault("market_price_volume", {})[
                        "asof"] = "2026-10-16T09:45:30+08:00"
                    report["chapters"]["market_price_volume"]["median_pct_change"] = 9.876543
                    report["chapters"]["market_price_volume"]["market_match_amount_yuan"] = 123456789
                    injected = True
                return original_check(report, checkpoint)

            monkeypatch.setattr(live_session, "assert_report_checkpoint_invariant", inject_future_asof)
        result = run_daily_pipeline(stage=stage, trade_date="2026-10-15", out_root=output,
                                    reference_root=references, sources=sources, dry_run=True)
        folder = result["out_dir"]
        assert (folder / "report.html").is_file()
        assert (folder / "report.json").is_file()
        if stage == "close":
            close_report = json.loads((folder / "report.json").read_text())
            assert "a_to_b" in close_report and "ladder_section" in close_report
        manifest = json.loads((folder / "manifest.json").read_text())
        report = json.loads((folder / "report.json").read_text())
        assert manifest["degraded"]
        assert any(item.startswith("MISSING_SLICE:") for item in manifest["degraded"])
        assert manifest["report_json_bytes"] <= 300_000
        assert isinstance(manifest.get("timings"), dict)
        if stage in ("auction", "open"):
            assert {"td_fetch", "compute", "render", "prepare_output", "write_json"} <= set(manifest["timings"])
        if stage in ("auction", "open"):
            if stage == "auction":
                assert result["report"]["chapters"]["reference_data"]["stock_names"]["status"] == "MISSING"
                assert result["report"]["chapters"]["reference_data"]["stock_caps"]["status"] == "MISSING"
                assert "plate_strength" in report["front_page"]
                assert "ladder" in report["front_page"]
                assert "extremes_board" in report["chapters"]
            if stage == "open":
                assert report["status"] == "DEGRADED"
                assert "chapters" not in report and "stocks" not in report
                assert "9.876543" not in json.dumps(report)
                assert "123456789" not in json.dumps(report)
                assert "REPORT_CHECKPOINT_INVARIANT_VIOLATION" in manifest["degraded"]
                assert manifest["degraded_counts"]["REPORT_CHECKPOINT_INVARIANT_VIOLATION"] == 1
                html = (folder / "report.html").read_text()
                assert "报告包含时点校验失败字段" in html
                assert "9.876543" not in html
            if stage != "open":
                assert "a_to_b" in report and "front_page" in report
            assert all({"source", "status", "elapsed_seconds", "rows"} <= set(row)
                       for row in manifest["slices"].values())
            assert any(row["source"] == "fixture" for row in manifest["slices"].values())
        else:
            assert result["report"]["reference_data"]["stock_names"]["status"] == "MISSING"
            assert "a_to_b" in report and "ladder_section" in report
    assert set(calls) == set(slice_ids)
    assert (output / "2026-10-15" / "auction_0926" / "report.json").exists()
    assert (output / "2026-10-15" / "open_0946" / "report.json").exists()
    assert (output / "2026-10-15" / "close_review" / "report.json").exists()


def test_core_catalog_and_bytes_redis_daily_pipeline_auction_and_open(tmp_path):
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.slices import SliceProviders, SliceReader

    trade_date = "2026-10-16"
    prior_date = "2026-10-15"
    catalog = ArtifactCatalog(tmp_path / "core-data")
    available = f"{trade_date}T09:00:00+08:00"
    common = {"available_at": available, "point_in_time_date": prior_date}
    catalog.commit("stock_static", prior_date, [
        {"symbol": "600001", "name": "封停股", "real_market_cap": 100, **common},
        {"symbol": "600002", "name": "接近封停股", "real_market_cap": 100, **common}],
        common, source="test")
    catalog.commit("plate_registry", prior_date, [
        {"code": "801001", "name": "通信", "class": "industry", **common}],
        common, source="test")
    catalog.commit("plate_members", prior_date, [
        {"symbol": "600001", "plate_code": "801001", **common},
        {"symbol": "600002", "plate_code": "801001", **common}],
        common, source="test")
    catalog.commit("daily_history_compact", prior_date, [
        {"date": "2026-10-13", "symbol": symbol, "close": 9.09, "pct_chg": 1,
         "amount": 100000, **common}
        for symbol in ("600001", "600002")] + [
        {"date": "2026-10-14", "symbol": symbol, "close": 10, "pct_chg": 10,
         "amount": 100000, **common}
        for symbol in ("600001", "600002")] + [
        {"date": prior_date, "symbol": symbol, "close": 11, "pct_chg": 10,
         "amount": 100000, **common}
        for symbol in ("600001", "600002")], common, source="test")
    catalog.commit("auction_baseline", trade_date, [
        {"date": trade_date, "symbol": "600001", "match_amount_yuan": 100000,
         "change_pct": 1, **common}], common, source="test")
    catalog.commit("limit_up_pool", prior_date, [
        {"symbol": "600001", "name": "封停股", "lb_days": 2, **common},
        {"symbol": "600002", "name": "接近封停股", "lb_days": 2, **common}], common, source="test")
    catalog.commit("limit_reasons", prior_date, [
        {"symbol": "600001", "Date": prior_date, "ZSCode": ["801001"],
         "Reason": "通信", **common}], common, source="test")
    for day in (trade_date, prior_date):
        catalog.commit("hot_plates", day, [
            {"code": "801001", "name": "通信", "strength": 1, "change_pct": 1,
             "amount_yuan": 100000, "sampled_at": f"{day}T09:20:00+08:00",
             "available_at": f"{day}T09:20:00+08:00"}],
            {"available_at": f"{day}T09:20:00+08:00"}, source="test")
    catalog.commit("hot_rank", trade_date, [
        {"symbol": "600001", "rank": 1, **common}], common, source="test")
    catalog.commit("trade_calendar", trade_date, [
        {"date": prior_date}, {"date": trade_date}], common, source="test")
    catalog.commit("path_stats", prior_date, [
        {"symbol": "600001", "outcome_status": "CLOSED_UP_PRICE_PROXY", **common}],
        common, source="test")

    class BytePipeline:
        def execute(self):
            return [{b"px": b"11000", b"pc": b"10000", b"amt": b"5000000",
                     b"vol": b"100", b"ts": b"1792113960000"},
                    {b"px": b"10990", b"pc": b"10000", b"amt": b"4000000",
                     b"vol": b"100", b"ts": b"1792113960000"}]

    class ByteRedis:
        def scan_iter(self, **kwargs):
            return iter([b"q2:600001", b"q2:600002"])

        def pipeline(self):
            class Pipe:
                def hgetall(self, _key):
                    return self
                execute = BytePipeline.execute
            return Pipe()

        def get(self, _key):
            return None

        def hgetall(self, _key):
            return {b"600001": b'{"name":"legacy bytes"}',
                    b"600002": b'{"name":"legacy bytes"}'}

    class DataTD(FakeTD):
        def states_at(self, cutoff):
            opening = cutoff.strftime("%H:%M:%S") >= "09:30:00"
            return {symbol: {"symbol": symbol, "price": (10500 if opening else price_milli) / 1000,
                             "price_milli": 10500 if opening else price_milli,
                             "pre_close": 10,
                             "prev_close_milli": 10000, "rest_bid_shares": 0,
                             "rest_ask_shares": 0, "pct_change": 5.0 if opening and symbol == "600001" else pct,
                             "match_amount_yuan": amount, "auction_amount_yuan": amount,
                             "status": "FINAL"}
                    for symbol, price_milli, pct, amount in (
                        ("600001", 11000, 10.0, 5000000),
                        ("600002", 10990, 9.99, 4000000))}

    redis = ByteRedis()
    reader = SliceReader(SliceProviders(catalog=catalog, redis=redis, file_fallback=False).providers())
    sources = DailySources(td=DataTD(), redis=redis, slice_reader=reader)
    for stage, asof in (("auction", "09:26:00"), ("open", "09:32:00")):
        result = run_daily_pipeline(stage=stage, trade_date=trade_date, asof=asof,
                                    out_root=tmp_path / "reports", sources=sources, dry_run=True)
        report_path = result["out_dir"] / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        manifest = json.loads((result["out_dir"] / "manifest.json").read_text(encoding="utf-8"))
        assert {row["status"] for name, row in manifest["slices"].items()
                if name != "stock_theme_assignment"} <= {"READY", "PARTIAL"}, {
            name: row for name, row in manifest["slices"].items()
            if name != "stock_theme_assignment" and row["status"] == "MISSING"}
        assert all(row["source"].startswith("core_data") for name, row in manifest["slices"].items()
                   if name != "stock_theme_assignment")
        assert "front_page" in report and "plate_strength" in report["front_page"]
        assert "ladder" in report["front_page"]
        paths = [path for cluster in report["front_page"]["ladder"]["clusters"]
                 for path in cluster["paths"]]
        labels = {path["symbol"]: path["status"] for path in paths}
        if stage == "auction":
            assert labels["600001"] == "封停价代理"
        else:
            assert report["ladder_section"]["promotion_checkpoint"] == "0932"
            assert report["ladder_section"]["promotion_status"] in {"READY", "UNKNOWN"}
            assert labels["600001"] != "封停价代理"
        assert labels["600002"] != "封停价代理"
def test_repeat_run_uses_numbered_directory_without_overwriting(tmp_path):
    kwargs = dict(stage="auction", trade_date="2026-10-15", out_root=tmp_path / "out",
                  reference_root=tmp_path / "refs", sources=DailySources(td=FakeTD()))
    first = run_daily_pipeline(**kwargs)
    marker = first["out_dir"] / "marker"
    marker.write_text("keep")
    second = run_daily_pipeline(**kwargs)
    assert second["out_dir"] == tmp_path / "out" / "2026-10-15" / "auction_0926-001"
    assert marker.read_text() == "keep"


def test_publish_only_uses_report_prefix_and_two_day_ttl(tmp_path):
    redis = FakeRedis()
    run_daily_pipeline(stage="auction", trade_date="2026-10-15", out_root=tmp_path / "out",
                       reference_root=tmp_path / "refs",
                       sources=DailySources(td=FakeTD(), redis=redis), publish=True)
    assert len(redis.writes) == 1
    key, value, options = redis.writes[0]
    assert key == "core:cache:report:2026-10-15:auction"
    assert key.startswith("core:cache:")
    assert options == {"ex": 172800}
    assert json.loads(value)["stage"] == "auction_0926"


def test_publish_and_dry_run_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError, match="cannot be combined"):
        run_daily_pipeline(stage="auction", trade_date="2026-10-15", out_root=tmp_path,
                           sources=DailySources(td=FakeTD()), publish=True, dry_run=True)


def test_close_stage_captures_injected_td_daily_kline(tmp_path):
    sources = DailySources(td=FakeTD(), reference_root=tmp_path / "refs",
                           daily_kline_rows=lambda day: [{
                               "ts": f"{day} 00:00:00", "open": 10, "high": 11, "low": 9,
                               "close": 10.5, "volume": 100, "amount": 1000,
                               "turnover": 1.2, "pct_chg": 5, "tbname": "d_000001"}])
    result = run_daily_pipeline(stage="close", trade_date="2026-10-15", out_root=tmp_path / "out",
                                reference_root=tmp_path / "refs", sources=sources, dry_run=True)
    capture = result["out_dir"] / "daily_capture"
    meta = json.loads((capture / "manifest.json").read_text())
    row = json.loads((capture / "daily_kline.jsonl").read_text())
    assert meta["source"] == "td_daily_kline"
    assert row["date"] == "2026-10-15"
    assert row["symbol"] == "000001"


def test_reference_catalog_is_explicit_and_recursive_fallback_is_disabled(tmp_path):
    from engine_core.daily_pipeline import resolve_references

    root = tmp_path / "refs"
    old = root / "auction-volume-baseline-20261010" / "auction_amount_by_day.csv"
    new = root / "auction-volume-baseline-20261014" / "auction_amount_by_day.csv"
    future = root / "auction-volume-baseline-20261016" / "auction_amount_by_day.csv"
    for path in (old, new, future):
        path.parent.mkdir(parents=True)
        path.write_text("date,amount\n")
    selected, meta = resolve_references(root, "2026-10-15")
    assert selected["auction_baseline"] is None
    assert meta["auction_baseline"]["discovered_by"] == "fallback"

    old_history = root / "study-20261014" / "daily_kline.csv"
    latest_history = root / "study-20261015" / "daily_kline_73d.csv"
    old_history.parent.mkdir(parents=True)
    latest_history.parent.mkdir(parents=True)
    old_history.write_text("date,close\n2026-10-13,1\n")
    latest_history.write_text("date,close\n2026-10-13,1\n2026-10-14,2\n2026-10-15,3\n")
    selected, _ = resolve_references(root, "2026-10-15")
    assert selected["daily_history"] is None

    catalog = root / "reference_catalog.json"
    catalog.write_text(json.dumps({"references": {"auction_baseline": {
        "path": str(old), "as_of": "2026-10-10"}}}))
    selected, meta = resolve_references(root, "2026-10-15")
    assert selected["auction_baseline"] == old
    assert meta["auction_baseline"]["discovered_by"] == "catalog"


def test_stage_daily_capture_and_history_are_point_in_time(tmp_path):
    from engine_core.daily_pipeline import resolve_references

    root = tmp_path / "refs"
    for day in ("20261014", "20261015"):
        folder = root / f"capture-{day}"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "row_count": 1}))
        (folder / "daily_kline.jsonl").write_text("{}\n")
    auction, _ = resolve_references(root, "2026-10-15", stage="auction")
    close, _ = resolve_references(root, "2026-10-15", stage="close")
    assert auction["daily_capture_manifest"] is None
    assert close["daily_capture_manifest"] is None


def test_intraday_relation_reference_after_report_date_is_excluded(tmp_path):
    from engine_core.daily_pipeline import resolve_references

    root = tmp_path / "refs"
    older = root / "registry-pit-20261013"
    same_day = root / "registry-pit-20261016"
    for folder in (older, same_day):
        folder.mkdir(parents=True)
        (folder / "registry.json").write_text("{}", encoding="utf-8")
        (folder / "relation.csv").write_text("stockid,plateid\n", encoding="utf-8")
    (root / "reference_catalog.json").write_text(json.dumps({"references": {
        "relation_dir": {"path": str(same_day), "as_of": "2026-10-16"}}}), encoding="utf-8")

    auction, _ = resolve_references(root, "2026-10-16", stage="auction")
    close, _ = resolve_references(root, "2026-10-16", stage="close")
    assert auction["relation_dir"] is None
    assert close["relation_dir"] == same_day


def test_relation_asof_and_trading_day_lag_are_not_assumed_current(tmp_path):
    from engine_core.daily_pipeline import _relation_lag_trading_days, _relation_snapshot_date

    relation = tmp_path / "relation_pit_20261013"
    relation.mkdir()
    assert _relation_snapshot_date(relation) == "PIT 20261013"
    assert _relation_lag_trading_days("2026-10-13", "2026-10-15", ["2026-10-14"]) == 2
    assert _relation_snapshot_date(tmp_path / "relation") == "UNKNOWN"


def test_relation_loader_returns_plate_to_member_mapping(tmp_path):
    from engine_core.daily_pipeline import _load_relations

    folder = tmp_path / "relation_pit_20261013"
    folder.mkdir()
    (folder / "registry.json").write_text(json.dumps({"plates": [
        {"code": "801001", "name": "通信"}, {"code": "801002", "name": "芯片"}]}))
    (folder / "relation.csv").write_text(
        "stockid,plateid\n000001,801001\n000001,801002\n000002,801001\n")
    relations, names, asof = _load_relations(folder)
    assert relations == {"801001": ["000001", "000002"], "801002": ["000001"]}
    assert names == {"801001": "通信", "801002": "芯片"}
    assert asof == "2026-10-13"


@pytest.mark.parametrize("stage", ["auction", "open"])
def test_intraday_manifest_never_references_same_day_daily_capture(tmp_path, stage):
    root = tmp_path / "refs"
    folder = root / "close-review-20261015" / "capture_20261015"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"date": "2026-10-15", "row_count": 1}))
    (folder / "daily_kline.jsonl").write_text("{}\n")
    result = run_daily_pipeline(stage=stage, trade_date="2026-10-15", out_root=tmp_path / "out",
                                reference_root=root, sources=DailySources(td=FakeTD()), dry_run=True)
    assert result["manifest"]["sources"]["daily_kline"]["path"] is None


def test_email_failure_is_recorded_without_failing_pipeline(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine_core import report_mailer

    monkeypatch.setattr(report_mailer, "load_smtp_config", lambda: SimpleNamespace(from_addr="reports@example.test"))
    monkeypatch.setattr(report_mailer, "send_message", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("provider failure")))
    result = run_daily_pipeline(stage="auction", trade_date="2026-10-15", out_root=tmp_path / "out",
                                reference_root=tmp_path / "refs", sources=DailySources(td=FakeTD()),
                                dry_run=True, email_to=["me@example.test"], email_send=True)
    assert result["manifest"]["email"] == {"status": "failed", "error_class": "RuntimeError"}
    manifest = json.loads((result["out_dir"] / "manifest.json").read_text())
    assert manifest["email"]["status"] == "failed"
    assert (result["out_dir"] / "report.json").is_file()


def test_shrink_live_report_caps_lists_and_records_totals():
    from engine_core.daily_pipeline import _shrink_live_report
    report = {"chapters": {"market_price_volume": {"top_plates_by_share": list(range(100))},
                           "plate_quadrants": {"rows": list(range(100))},
                           "watchlist": {"behavior_candidates": list(range(100))}},
              "snapshots": [{"top_plates_by_share": list(range(50))}],
              "auction_terminal_0925": {"top_plates_by_share": list(range(50))}}
    out = _shrink_live_report(report)
    assert len(out["chapters"]["market_price_volume"]["top_plates_by_share"]) == 20
    assert out["chapters"]["market_price_volume"]["top_plates_by_share_total"] == 100
    assert len(out["chapters"]["plate_quadrants"]["rows"]) == 30
    assert len(out["chapters"]["watchlist"]["behavior_candidates"]) <= 5
    assert len(out["snapshots"][0]["top_plates_by_share"]) == 20
    assert len(out["auction_terminal_0925"]["top_plates_by_share"]) == 20


def test_shrink_live_report_compacts_auction_extremes_and_behavior_candidates():
    from engine_core.daily_pipeline import _shrink_live_report
    report = {"stage": "auction_0926", "chapters": {
        "watchlist": {"behavior_candidates": [{"symbol": str(i), "name": "甲", "extra": "x" * 100}
                                                for i in range(12)]},
        "extremes_board": {"auction": {"rows": [{"symbol": "1"}],
                            "rebound_top8": [], "weak_top8": [], "amount_cohort": []},
                           "open": {"rows": [{"symbol": "2"}], "rebound_top8": [{"symbol": "3"}],
                                    "weak_top8": [], "amount_cohort": []}}}}
    out = _shrink_live_report(report)
    assert len(out["chapters"]["watchlist"]["behavior_candidates"]) <= 5
    assert out["chapters"]["extremes_board"]["open"]["rows"] == []


def test_live_report_json_drops_full_universe_rows_after_basis_note():
    """Realistic-scale regression (SMOKE-1): behavior_rows for ~5k stocks must never reach report.json."""
    from engine_core import live_pages
    payload = {"stage": "auction_0926", "chapters": {"behavior_rows": [{"symbol": f"{i:06d}", "name": "n" * 20, "pad": "x" * 900} for i in range(5200)],
                                                      "watchlist": {}}, "snapshots": [], "limitations": "x"}
    out = live_pages._json_report_payload(payload)
    import json
    assert "behavior_rows" not in out.get("chapters", {})
    assert len(json.dumps(out, ensure_ascii=False)) < 250_000


def test_auction_baseline_uses_only_pit_td0925_market_rows_and_deduplicates_days():
    from engine_core.daily_pipeline import _auction_baseline_history

    rows = [
        {"date": "2026-10-01", "row_kind": "market_observation", "source": "td_0925",
         "auction_amount_yuan": 100, "available_at": "2026-10-02T08:00:00+08:00"},
        {"date": "2026-10-01", "row_kind": "market_observation", "source": "td_0925",
         "auction_amount_yuan": 100, "available_at": "2026-10-02T08:00:00+08:00"},
        {"date": "2026-10-02", "row_kind": None, "source": "td_0925",
         "auction_amount_yuan": 200, "available_at": "2026-10-03T08:00:00+08:00"},
        {"date": "2026-10-03", "row_kind": "market_observation", "source": "daily_kline",
         "auction_amount_yuan": 300, "available_at": "2026-10-04T08:00:00+08:00"},
        {"date": "2026-10-04", "row_kind": "market_observation", "source": "td_0925",
         "sum_yi": 4, "available_at": "2026-10-05T08:00:00+08:00"},
        {"date": "2026-10-07", "row_kind": None, "source": "td_0925",
         "sum_yi": 9, "available_at": "2026-10-08T08:00:00+08:00"},
        {"date": "2026-10-05", "row_kind": "market_observation", "source": "td_0925",
         "auction_amount_yuan": 500, "available_at": "2026-10-17T08:00:00+08:00"},
        {"date": "2026-10-06", "row_kind": "market_observation", "source": "td_0925",
         "auction_amount_yuan": 550, "available_at": "2026-10-16T01:26:01+00:00"},
        {"date": "2026-10-16", "row_kind": "market_observation", "source": "td_0925",
         "auction_amount_yuan": 600, "available_at": "2026-10-15T08:00:00+08:00"},
    ]
    assert _auction_baseline_history(rows, trade_date="2026-10-16",
                                    asof="2026-10-16T09:26:00+08:00") == [100.0, 400_000_000.0]


def test_auction_baseline_historical_replay_accepts_late_rows_and_marks_basis():
    from engine_core.daily_pipeline import _auction_baseline_history
    from engine_core.auction_panorama import build_auction_panorama

    rows = [
        {"date": "2026-10-01", "row_kind": "market_observation", "source": "td_0925",
         "auction_amount_yuan": 100, "available_at": "2026-10-17T08:00:00+08:00"},
        {"date": "2026-10-01", "row_kind": "market_observation", "source": "td_0925",
         "auction_amount_yuan": 200, "available_at": "2026-10-17T08:00:00+08:00"},
        {"date": "2026-10-02", "row_kind": "market_observation", "source": "other",
         "auction_amount_yuan": 300, "available_at": "2026-10-17T08:00:00+08:00"},
        {"date": "2026-10-03", "row_kind": None, "source": "td_0925",
         "sum_yi": 9, "available_at": "2026-10-17T08:00:00+08:00"},
        {"date": "2026-10-16", "row_kind": "market_observation", "source": "td_0925",
         "auction_amount_yuan": 400, "available_at": "2026-10-17T08:00:00+08:00"},
    ]
    history = _auction_baseline_history(rows, trade_date="2026-10-16",
        asof="2026-10-16T09:26:00+08:00", replay_event_time=True)
    assert history == [200.0]
    product = build_auction_panorama({"trade_date": "2026-10-16",
        "asof": "2026-10-16T09:26:00+08:00", "rows": [{"symbol": "000001",
        "auction_amount_yuan": 100, "valid_price": True, "pct_change": 1.0}]},
        plate_members=[], yesterday_pool=[], market_amount_history=history,
        amount_baseline_basis="REPLAY_EVENT_TIME")
    assert product["overview"]["amount_multiple_64d"]["basis"] == "REPLAY_EVENT_TIME"
    assert product["overview"]["amount_multiple_64d"]["status"] == "PARTIAL"


def test_v6_report_projection_omits_full_stocks_but_keeps_top_stocks():
    import json
    from engine_core import live_pages

    rows = [{"symbol": f"{i:06d}", "name": "股票" * 5, "auction_amount_yuan": i}
            for i in range(5200)]
    payload = {"manifest": {"assembly": "v6"}, "stocks": rows,
               "snapshots": [{"top_stocks_by_amount": rows[:20]}]}
    report = live_pages._json_report_payload(payload)
    assert "stocks" not in report
    assert report["manifest"]["omitted_blocks"]["stocks"]["row_count"] == 5200
    assert "snapshots" not in report
    assert report["manifest"]["omitted_blocks"]["snapshots"]["row_count"] == 1
    assert len(json.dumps(report, ensure_ascii=False)) < 250_000


def test_relation_snapshot_date_ignores_dates_in_parent_path(tmp_path):
    from engine_core.daily_pipeline import _relation_snapshot_date

    parent = tmp_path / "run_20261016" / "relation_pit_20261013"
    parent.mkdir(parents=True)
    assert _relation_snapshot_date(parent) == "PIT 20261013"
    undated = tmp_path / "run_20261016" / "relation"
    undated.mkdir()
    assert _relation_snapshot_date(undated) == "UNKNOWN"
