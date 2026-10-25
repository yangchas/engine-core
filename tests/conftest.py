"""Keep test helper imports stable from both repo and workspace roots."""

from __future__ import annotations

import sys
import gzip
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_ARTIFACTS = PROJECT_ROOT / "tests" / "fixtures" / "real" / "core_artifacts"
project_root_text = str(PROJECT_ROOT)
if project_root_text not in sys.path:
    sys.path.insert(0, project_root_text)


import pytest


def read_real_artifact(dataset: str, day: str, variant: str) -> tuple[dict, list[dict]]:
    """Load a captured upstream artifact used by offline daily_kline tests."""
    base = REAL_ARTIFACTS / "c540" / dataset / day / variant
    if not base.is_dir() and dataset == "daily_kline":
        base = REAL_ARTIFACTS / "c522" / dataset / day / variant
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    with gzip.open(base / "rows.jsonl.gz", "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return manifest, rows


def seed_real_symbol_universe(catalog, day: str, symbols=None) -> list[dict]:
    manifest, rows = read_real_artifact("symbol_universe", day, "actual")
    if symbols is not None:
        selected = set(symbols)
        rows = [row for row in rows if str(row.get("symbol", "")).zfill(6) in selected]
    catalog.commit("symbol_universe", day, rows, manifest,
                   source=manifest.get("source", "baostock_query_all_stock"))
    return rows


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="run live contract tests that call the real upstream sources (network, read-only)")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls a real upstream source; run with --live (see docs/TESTING.md)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live contract test: run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolated_slice_root(tmp_path, monkeypatch, request):
    """Per-test slice/state cache root so past-dated fake TD states never leak between tests or into ~/core-data."""
    monkeypatch.setenv("CORE_SLICE_ROOT", str(tmp_path / "slices"))
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path / "core-data"))
    monkeypatch.setenv("CORE_STATE_CACHE", "off")  # cache tests opt in explicitly
    if not (request.config.getoption("--live") and request.node.get_closest_marker("live")):
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # offline tests never read real Redis
from datetime import datetime, timezone


def use_declared_time_as_test_commit_clock(catalog):
    """Model fixture visibility at its declared instant via the injectable test clock."""
    commit = catalog.commit

    def commit_with_test_clock(dataset, trade_date, rows, manifest, *, source, **kwargs):
        if "clock" not in kwargs:
            value = manifest.get("available_at") or manifest.get("declared_available_at")
            if value:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                kwargs["clock"] = lambda: parsed
        return commit(dataset, trade_date, rows, manifest, source=source, **kwargs)

    catalog.commit = commit_with_test_clock
    return catalog


def patch_catalog_with_declared_test_clock(monkeypatch, catalog_class):
    commit = catalog_class.commit

    def commit_with_test_clock(self, dataset, trade_date, rows, manifest, *, source, **kwargs):
        if "clock" not in kwargs:
            value = manifest.get("available_at") or manifest.get("declared_available_at")
            if value:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                kwargs["clock"] = lambda: parsed
        return commit(self, dataset, trade_date, rows, manifest, source=source, **kwargs)

    monkeypatch.setattr(catalog_class, "commit", commit_with_test_clock)
