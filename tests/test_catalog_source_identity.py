import gzip
import json
from pathlib import Path

from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.guardian.registry import catalog_manifest, daily_kline_needs_upgrade
from conftest import seed_real_symbol_universe


FIXTURES = Path(__file__).parent / "fixtures" / "real" / "core_artifacts" / "c522" / "daily_kline" / "2026-10-23"


def _real_version(source: str):
    path = FIXTURES / source
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    with gzip.open(path / "rows.jsonl.gz", "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return manifest, rows


def test_identical_real_rows_from_different_sources_keep_both_catalog_entries(tmp_path):
    provisional, provisional_rows = _real_version("provisional")
    formal, formal_rows = _real_version("formal")
    assert provisional_rows == formal_rows
    catalog = ArtifactCatalog(tmp_path / "core")
    seed_real_symbol_universe(catalog, "2026-10-23")

    catalog.commit("daily_kline", "2026-10-23", provisional_rows, provisional,
                   source=provisional["source"])
    catalog.commit("daily_kline", "2026-10-23", formal_rows, formal,
                   source=formal["source"])

    versions = catalog.read()["datasets"]["daily_kline"]["dates"]["2026-10-23"]
    assert len(versions) == 2
    assert [(item["source"], item["business_status"]) for item in versions] == [
        ("baostock>wencai>q2_final", "PROVISIONAL"), ("baostock", "FORMAL")]
    selected = catalog_manifest(catalog.read(), "daily_kline", "2026-10-23")
    assert selected["source"] == "baostock" and selected["business_status"] == "FORMAL"
    assert daily_kline_needs_upgrade(selected) is False

    catalog.commit("daily_kline", "2026-10-23", formal_rows, formal,
                   source=formal["source"])
    assert len(catalog.read()["datasets"]["daily_kline"]["dates"]["2026-10-23"]) == 2


def test_same_source_same_real_version_commit_is_idempotent(tmp_path):
    formal, rows = _real_version("formal")
    catalog = ArtifactCatalog(tmp_path / "core")
    seed_real_symbol_universe(catalog, "2026-10-23")
    catalog.commit("daily_kline", "2026-10-23", rows, formal, source="baostock")
    catalog.commit("daily_kline", "2026-10-23", rows, formal, source="baostock")
    versions = catalog.read()["datasets"]["daily_kline"]["dates"]["2026-10-23"]
    assert len(versions) == 1
    assert versions[0]["source"] == "baostock" and versions[0]["business_status"] == "FORMAL"
