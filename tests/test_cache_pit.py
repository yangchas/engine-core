import json
from pathlib import Path

from engine_core.data_pipeline.node_store import NodeStore
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.prep_trade_day import _prepare_theme_snapshot_cache
from conftest import use_declared_time_as_test_commit_clock


def test_prep_persists_theme_snapshot_node_and_versions_visible_inputs(tmp_path):
    catalog = use_declared_time_as_test_commit_clock(ArtifactCatalog(tmp_path))
    day, report_day = "2026-10-15", "2026-10-16"
    fixture = Path(__file__).parent / "fixtures" / "trade_calendar"
    source = json.loads((fixture / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    dates = [row["calendar_date"] for row in source["rows"] if row["is_trading_day"] == "1"]
    raw = {path.name: path.read_bytes() for path in fixture.glob("*.json")}
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
    TradeCalendarStore(tmp_path).publish_confirmed(
        dates, source="C325 BaoStock+SZSE", fetched_at="2026-10-20T00:00:00+00:00",
        coverage_start="2026-01-01", coverage_end="2026-12-31", raw_responses=raw)

    def commit(dataset, rows):
        return catalog.commit(dataset, day, rows,
            {"trade_date": day, "available_at": report_day + "T08:00:00+08:00"}, source="fixture")

    commit("limit_up_pool", [json.loads(line) for line in
        (Path(__file__).parent / "fixtures/real_limit_up_pool_sample.jsonl").read_text().splitlines()[:2]])
    commit("hot_plates", [{"plate_code": "801001", "name": "热板"}])
    commit("plate_registry", [{"code": "801001", "name": "原因题材"}])
    commit("limit_reasons", [{"symbol": "000001", "Date": day, "Type": "0",
                              "ZSCode": ["801001"], "Reason": "原因"}])
    commit("daily_kline", [{"symbol": "000001", "date": day, "close": 10}])

    assert _prepare_theme_snapshot_cache(catalog, report_day, day,
                                         report_day + "T08:50:00+08:00")
    versions = catalog.read()["datasets"]["node__theme_snapshot"]["dates"][report_day]
    node = json.loads(Path(versions[-1]["manifest"]).read_text())
    store = NodeStore(catalog)
    assert store.get("theme_snapshot", report_day, node["node_fingerprint"])
    assert node["code_version"].startswith("ThemeSnapshotNodeV1:")

    original = node["inputs"]
    value, hit = store.compute("theme_snapshot", report_day, node["code_version"], original,
                               lambda: {"wrong": True})
    assert hit and value == store.get("theme_snapshot", report_day, node["node_fingerprint"])
    for changed in ({**original, "yest_limit_pool": "new-version:2"},
                    {**original, "yest_limit_pool": "same-version:3"}):
        _, hit = store.compute("theme_snapshot", report_day, node["code_version"], changed,
                               lambda: value)
        assert not hit
    _, hit = store.compute("theme_snapshot", report_day, "ThemeSnapshotNodeV1:algorithm-v2",
                           original, lambda: value)
    assert not hit
