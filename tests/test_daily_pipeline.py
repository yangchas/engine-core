import json

import pytest

from engine_core.daily_pipeline import (DailySources, _attach_range_positions, _load_ladder_summary,
                                        discover_references, run_daily_pipeline)


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


def test_live_section_stocks_includes_0945_at_exact_cutoff():
    from engine_core.daily_pipeline import _live_section_stocks

    class States:
        def __init__(self): self.cutoffs = []
        def states_at(self, cutoff):
            self.cutoffs.append(cutoff.strftime("%H:%M:%S"))
            return {"600001": {"pct_change": 1.0, "match_amount_yuan": 10,
                               "amt_yuan": 20}}

    source = States()
    rows = _live_section_stocks(source, trade_date="2026-10-15", stage="open", asof="09:45:00",
                                relations={}, plate_names={}, stock_names={}, history_path=None)
    assert source.cutoffs == ["09:24:10", "09:25:06", "09:32:00", "09:38:00", "09:45:00"]
    assert rows[0]["p0945_pct"] == 1.0
    assert rows[0]["p0945_amount_yuan"] == 20


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


def test_three_stages_write_reports_and_explicit_missing_references(tmp_path):
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
            assert result["report"]["chapters"]["reference_data"]["stock_names"]["status"] == "MISSING"
            assert result["report"]["chapters"]["reference_data"]["stock_caps"]["status"] == "MISSING"
            assert "plate_strength" in report["front_page"]
            assert "ladder" in report["front_page"]
            assert "extremes_board" in report["chapters"]
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
