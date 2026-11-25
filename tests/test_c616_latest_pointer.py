"""C616 regression against the two real Baostock 2026-10-29 partitions."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from examples.guardian_handler import _has_baostock_daily_kline
from engine_core.data_pipeline.storage import ArtifactCatalog


TRADE_DATE = "2026-10-29"


def _real_baostock_versions():
    root = Path(os.getenv("C616_REAL_CORE_DATA_ROOT", "/home/exedev/core-data")).expanduser()
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    entries = catalog.get("datasets", {}).get("daily_kline", {}).get("dates", {}).get(TRADE_DATE, [])
    versions = []
    for entry in entries:
        if entry.get("source") != "baostock":
            continue
        path = Path(entry["manifest"])
        if not path.is_absolute():
            path = root / path
        if path.is_file():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            versions.append((entry, manifest, path))
    return root, catalog, versions


def test_best_real_same_day_version_advances_both_latest_pointers(tmp_path):
    live_root, live_catalog, versions = _real_baostock_versions()
    if len(versions) < 2:
        pytest.skip("two real 2026-10-29 Baostock versions are required")
    partial = next((item for item in versions
                    if item[1].get("business_status") == "PARTIAL"), None)
    complete = next((item for item in versions
                     if (item[1].get("quality") or {}).get("coverage") == 1.0), None)
    if partial is None or complete is None:
        pytest.skip("real partial and complete Baostock versions are required")
    partial_entry, partial_manifest, _ = partial
    complete_entry, complete_manifest, complete_manifest_path = complete
    assert partial_manifest.get("version") == "sha256-0d245326f9238003"
    assert complete_manifest.get("version") == "sha256-b78849e270e3e436"

    # locate() must return the objectively better real manifest even if the inferior version
    # was appended after it, matching the observed 10-29 latest-pointer regression.
    locate_root = tmp_path / "locate"
    locate_root.mkdir()
    locate_catalog = {
        "schema_version": 1,
        "datasets": {"daily_kline": {"dates": {TRADE_DATE: [complete_entry, partial_entry]},
                                      "latest": {"trade_date": TRADE_DATE,
                                                 "version": partial_manifest["version"]}}},
    }
    (locate_root / "catalog.json").write_text(json.dumps(locate_catalog), encoding="utf-8")
    located = ArtifactCatalog(locate_root).locate("daily_kline", TRADE_DATE)
    assert located == Path(complete_entry["manifest"])

    # Recommit the real complete rows after a real partial entry. The test root gets its own
    # catalog and immutable copy; source rows/manifests remain untouched.
    write_root = tmp_path / "promotion"
    write_root.mkdir()
    universe_entry = (live_catalog.get("datasets", {}).get("symbol_universe", {})
                      .get("dates", {}).get(TRADE_DATE, []))
    datasets = {
        "daily_kline": {"dates": {TRADE_DATE: [partial_entry]},
                        "latest": {"trade_date": TRADE_DATE,
                                   "version": partial_manifest["version"]}},
    }
    if universe_entry:
        datasets["symbol_universe"] = {"dates": {TRADE_DATE: universe_entry},
                                       "latest": {"trade_date": TRADE_DATE,
                                                  "version": universe_entry[-1]["version"]}}
    (write_root / "catalog.json").write_text(json.dumps({"schema_version": 1, "datasets": datasets}),
                                             encoding="utf-8")
    live_catalog_reader = ArtifactCatalog(live_root)
    rows = live_catalog_reader.read_rows(complete_manifest_path)
    incoming = dict(complete_manifest)
    incoming.pop("completion_policy", None)
    incoming.pop("source_dates", None)
    committed = ArtifactCatalog(write_root).commit("daily_kline", TRADE_DATE, rows, incoming,
                                                     source="baostock")
    assert committed["business_status"] == "PROVISIONAL"
    assert committed["quality"]["formal_downgrade_reasons"] == [
        "required_fields_incomplete", "source_date_evidence_incomplete"]
    assert _has_baostock_daily_kline(write_root, TRADE_DATE) is False

    after = ArtifactCatalog(write_root)
    catalog_latest = after.read()["datasets"]["daily_kline"]["latest"]
    latest_file = json.loads((write_root / "daily_kline/latest.json").read_text(encoding="utf-8"))
    assert catalog_latest == {"trade_date": TRADE_DATE, "version": committed["version"]}
    assert latest_file["version"] == committed["version"]
    assert after.locate("daily_kline", TRADE_DATE) == Path(committed["manifest_uri"])
    assert (write_root / "daily_kline/trade_date=2026-10-29/source=baostock"
            / committed["version"] / "rows.jsonl.gz").is_file()
