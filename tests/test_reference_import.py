import importlib.util
import json
from pathlib import Path

from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.slices import SliceProviders, SliceReader


def _module():
    path = Path(__file__).parents[1] / "examples/import_reference_to_core_store.py"
    spec = importlib.util.spec_from_file_location("reference_import_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_standard_import_builds_stock_and_plate_rows_with_lineage_and_idempotence(tmp_path, monkeypatch):
    module = _module()
    fixture = Path(__file__).parent / "fixtures/reference_import"
    catalog = ArtifactCatalog(tmp_path / "core")
    from conftest import patch_catalog_with_declared_test_clock
    patch_catalog_with_declared_test_clock(monkeypatch, ArtifactCatalog)
    sources = {
        "stock_static": [fixture / "names.json", fixture / "caps.json"],
        "plate_registry": [fixture / "registry.json"],
        "plate_members": [fixture / "relation.csv"],
    }
    preview = module.import_reference_sources(sources, catalog=catalog,
        trade_date="2026-10-13", as_of="2026-10-14T08:50:00+08:00",
        pit_date="2026-10-13", execute=False)
    assert [item["rows"] for item in preview] == [2, 1, 2]
    assert catalog.locate("stock_static", "2026-10-13") is None

    first = module.import_reference_sources(sources, catalog=catalog,
        trade_date="2026-10-13", as_of="2026-10-14T08:50:00+08:00",
        pit_date="2026-10-13", execute=True)
    again = module.import_reference_sources(sources, catalog=catalog,
        trade_date="2026-10-13", as_of="2026-10-14T08:50:00+08:00",
        pit_date="2026-10-13", execute=True)
    assert [row["version"] for row in first] == [row["version"] for row in again]
    stocks = catalog.read_rows(catalog.locate("stock_static", "2026-10-13"))
    assert stocks[0]["symbol"] == "000001"
    assert stocks[0]["name"] == "平安银行"
    assert stocks[0]["float_cap_yuan"] == 1_000_000_000
    assert stocks[1]["float_cap_yuan"] is None
    manifest = json.loads(catalog.locate("plate_members", "2026-10-13").read_text())
    assert manifest["point_in_time_date"] == "2026-10-13"
    assert manifest["provenance"] == "import_from_validation"
    assert len(manifest["source_sha256"]) == 64

    # A later correction is stored as another immutable version. Reads choose
    # the latest version available at the requested as-of, not catalog latest.
    catalog.commit("plate_members", "2026-10-13", [
        {"plate_code": "801001", "symbol": "000001", "pit_date": "2026-10-13",
         "available_at": "2026-10-15T08:50:00+08:00"}],
        {"available_at": "2026-10-15T08:50:00+08:00", "point_in_time_date": "2026-10-13"},
        source="import_from_validation")
    reader = SliceReader(SliceProviders(catalog=catalog).providers())
    old_asof = reader.get("plate_members", trade_date="2026-10-14",
                          asof="2026-10-14T08:50:00+08:00")
    assert old_asof.meta["status"] == "PARTIAL"
    assert old_asof.meta["reason"] == "DATE_FALLBACK"
    assert old_asof.rows[0]["symbol"] == "000001"


def test_importer_can_flatten_5000_rows_without_per_row_file_reads(tmp_path):
    module = _module()
    source = tmp_path / "large.csv"
    source.write_text("symbol,value\n" + "".join(f"{i:06d},{i}\n" for i in range(5000)))
    catalog = ArtifactCatalog(tmp_path / "core")
    result = module.import_files([f"path_stats={source}"], catalog=catalog,
        trade_date="2026-10-15", as_of="2026-10-16T08:50:00+08:00",
        pit_date="2026-10-15", execute=True)
    assert result[0]["rows"] == 5000
    assert len(catalog.read_rows(catalog.locate("path_stats", "2026-10-15"))) == 5000


def test_reference_imports_feed_all_six_core_slices_with_pit(tmp_path, monkeypatch):
    module = _module()
    fixture = Path(__file__).parent / "fixtures/reference_import"
    catalog = ArtifactCatalog(tmp_path / "core")
    from conftest import patch_catalog_with_declared_test_clock
    patch_catalog_with_declared_test_clock(monkeypatch, ArtifactCatalog)
    sources = {
        "stock_static": [fixture / "names.json", fixture / "caps.json"],
        "plate_registry": [fixture / "registry.json"],
        "plate_members": [fixture / "relation.csv"],
        "auction_baseline": [fixture / "auction_baseline.csv"],
        "path_stats": [fixture / "path_stats.csv"],
    }
    module.import_reference_sources(sources, catalog=catalog, trade_date="2026-10-15",
        as_of="2026-10-14T08:50:00+08:00", pit_date="2026-10-13", execute=True,
        trade_date_by_dataset={"stock_static": "2026-10-13", "plate_registry": "2026-10-13",
                               "plate_members": "2026-10-13", "auction_baseline": "2026-10-13",
                               "path_stats": "2026-10-13"})
    history = module.import_kaipan_history(
        [fixture / "capture-2026-10-14T085000"], catalog=catalog, execute=True)
    assert [item["dataset"] for item in history] == ["limit_up_pool", "limit_reasons"]

    reader = SliceReader(SliceProviders(catalog=catalog).providers())
    request_date, asof = "2026-10-15", "2026-10-15T09:00:00+08:00"
    requests = {
        "stock_name_map": {}, "plate_registry": {}, "plate_members": {},
        "auction_baseline": {}, "path_stats": {}, "limit_reason_history": {},
        "yest_limit_pool": {"source_date": "2026-10-13"},
    }
    results = {name: reader.get(name, trade_date=request_date, asof=asof, **params)
               for name, params in requests.items()}
    assert all(result.meta["status"] in {"READY", "PARTIAL"} for result in results.values())
    fallbacks = [result for result in results.values()
                 if result.meta.get("reason") == "DATE_FALLBACK"]
    assert fallbacks and all(result.meta["status"] == "PARTIAL" for result in fallbacks)
    assert results["auction_baseline"].rows[0]["threshold_wan"] == 500.0
    assert results["path_stats"].meta["quality"]["partial"] is False
    assert results["limit_reason_history"].rows[0]["symbol"] == "000001"
