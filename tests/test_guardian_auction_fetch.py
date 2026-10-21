import json
from datetime import datetime
from pathlib import Path

import engine_core.auction_stock_facts as stock_facts


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/auction_snapshot_20261021_0925.json"


def _real_snapshot_sample():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["source"].startswith("TD market_data1.auction_snapshot_v2")
    assert payload["trade_date"] == "2026-10-21" and payload["auction_tag"] == "0925"
    return payload["rows"]


def test_auction_facts_event_is_scheduled_and_registered_consistently():
    from engine_core.guardian.management import load_schedule

    schedule, _ = load_schedule(ROOT / "src/engine_core/guardian/schedule.json")
    event = next(item for item in schedule if item.event_id == "auction_facts_0925")
    assert event.handler == "auction_report"
    assert event.start == "09:25:10" and event.deadline == "09:29:59"
    assert event.max_attempts == 3 and event.catchup is True
    registry = json.loads((ROOT / "src/engine_core/guardian/datasets.registry.json").read_text())
    items = {item["id"]: item for item in registry["items"]}
    for item_id in ("auction_stock_facts", "auction_points", "auction_panorama"):
        assert items[item_id]["guardian_event"] == event.event_id
        assert items[item_id]["late_policy"]["trigger_at"] == event.start


def test_real_td_snapshot_rows_sort_and_repeat_to_one_version(tmp_path):
    from engine_core.auction_stock_facts import store_auction_stock_facts

    snapshots = _real_snapshot_sample()
    first = stock_facts.build_auction_stock_facts(
        "2026-10-21", "2026-10-21T09:25:10+08:00", snapshots, [])
    second = stock_facts.build_auction_stock_facts(
        "2026-10-21", "2026-10-21T09:25:10+08:00", list(reversed(snapshots)), [])
    assert [row["symbol"] for row in first["rows"]] == sorted(row["symbol"] for row in snapshots)
    first_version = store_auction_stock_facts(
        first, artifact_root=tmp_path, capture_semantics="late_td_rebuild")["version"]
    second_version = store_auction_stock_facts(
        second, artifact_root=tmp_path, capture_semantics="late_td_rebuild")["version"]
    assert first_version == second_version


def test_real_catalog_pit_inputs_and_temporally_missing_case():
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from examples.fetch_auction_item import _pit_rows

    root = Path.home() / "core-data"
    catalog = ArtifactCatalog(root)
    datasets = catalog.read()["datasets"]
    target_cutoff = datetime.fromisoformat("2026-10-21T09:25:10+08:00")
    for dataset in ("plate_members", "limit_up_pool"):
        dates = datasets[dataset]["dates"]
        rows, meta = _pit_rows(catalog, dataset, (day for day in dates if day < "2026-10-21"), target_cutoff)
        assert rows and meta and meta["trade_date"] == "2026-10-20"
    # The real 2026-10-20 plate snapshot was not yet visible at this earlier cutoff.
    rows, meta = _pit_rows(
        catalog, "plate_members", ["2026-10-20"],
        datetime.fromisoformat("2026-10-20T23:59:00+08:00"))
    assert rows is None and meta is None


def test_real_historical_auction_with_no_pit_references_is_saved_partial(tmp_path):
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from examples.fetch_auction_item import _products

    payload = json.loads((ROOT / "tests/fixtures/auction_snapshot_20260918_0925.json").read_text())
    result = _products(payload["trade_date"], "auction_panorama", tmp_path,
                       payload["rows"], [], source_catalog_root=Path.home() / "core-data")
    assert result["status"] == "degraded"
    assert result["missing_inputs"] == ["plate_members", "yesterday_pool"]
    catalog = ArtifactCatalog(tmp_path)
    manifest_path = catalog.locate("auction_panorama", payload["trade_date"])
    manifest = json.loads(manifest_path.read_text())
    product = json.loads(catalog.read_rows(manifest_path)[0]["product_json"])
    assert product["status"] == "PARTIAL"
    assert manifest["quality"]["missing_inputs"] == result["missing_inputs"]
