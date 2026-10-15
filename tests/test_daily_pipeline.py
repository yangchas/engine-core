import json

import pytest

from engine_core.daily_pipeline import DailySources, discover_references, run_daily_pipeline


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


def test_three_stages_write_reports_and_explicit_missing_references(tmp_path):
    references = tmp_path / "references"
    output = tmp_path / "reports"
    sources = DailySources(td=FakeTD(), reference_root=references)
    for stage in ("auction", "open", "close"):
        result = run_daily_pipeline(stage=stage, trade_date="2026-10-15", out_root=output,
                                    reference_root=references, sources=sources, dry_run=True)
        folder = result["out_dir"]
        assert (folder / "report.html").is_file()
        assert (folder / "report.json").is_file()
        manifest = json.loads((folder / "manifest.json").read_text())
        assert manifest["degraded"]
        assert any(item.startswith("MISSING_REFERENCE:") for item in manifest["degraded"])
        assert manifest["report_json_bytes"] <= 300_000
        if stage in ("auction", "open"):
            assert result["report"]["chapters"]["reference_data"]["stock_names"]["status"] == "MISSING"
            assert result["report"]["chapters"]["reference_data"]["stock_caps"]["status"] == "MISSING"
        else:
            assert result["report"]["reference_data"]["stock_names"]["status"] == "MISSING"
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


def test_catalog_wins_and_fallback_uses_latest_not_after_trade_date(tmp_path):
    from engine_core.daily_pipeline import resolve_references

    root = tmp_path / "refs"
    old = root / "auction-volume-baseline-20261010" / "auction_amount_by_day.csv"
    new = root / "auction-volume-baseline-20261014" / "auction_amount_by_day.csv"
    future = root / "auction-volume-baseline-20261016" / "auction_amount_by_day.csv"
    for path in (old, new, future):
        path.parent.mkdir(parents=True)
        path.write_text("date,amount\n")
    selected, meta = resolve_references(root, "2026-10-15")
    assert selected["auction_baseline"] == new
    assert meta["auction_baseline"]["discovered_by"] == "fallback"

    old_history = root / "study-20261014" / "daily_kline.csv"
    latest_history = root / "study-20261015" / "daily_kline_73d.csv"
    old_history.parent.mkdir(parents=True)
    latest_history.parent.mkdir(parents=True)
    old_history.write_text("date,close\n2026-10-13,1\n")
    latest_history.write_text("date,close\n2026-10-13,1\n2026-10-14,2\n2026-10-15,3\n")
    selected, _ = resolve_references(root, "2026-10-15")
    assert selected["daily_history"] == latest_history

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
    assert auction["daily_capture_manifest"].parent.name == "capture-20261014"
    assert close["daily_capture_manifest"].parent.name == "capture-20261015"


def test_relation_asof_and_trading_day_lag_are_not_assumed_current(tmp_path):
    from engine_core.daily_pipeline import _relation_lag_trading_days, _relation_snapshot_date

    relation = tmp_path / "relation_pit_20261013"
    relation.mkdir()
    assert _relation_snapshot_date(relation) == "PIT 20261013"
    assert _relation_lag_trading_days("2026-10-13", "2026-10-15", ["2026-10-14"]) == 2
    assert _relation_snapshot_date(tmp_path / "relation") == "UNKNOWN"


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
