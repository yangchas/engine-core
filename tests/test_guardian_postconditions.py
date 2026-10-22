import json
from pathlib import Path

from engine_core.guardian.commands import validate_handler_postcondition


def _catalog(root: Path, dataset: str, day: str, rows=(), **manifest):
    from engine_core.data_pipeline.storage import ArtifactCatalog

    ArtifactCatalog(root).commit(dataset, day, rows, manifest, source="test")


def test_kaipan_fetch_requires_nonempty_previous_day_limit_pool(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    real_calendar = Path.home() / "core-data" / "trade_calendar"
    if not real_calendar.is_dir():
        import pytest
        pytest.skip("real Core trade calendar not available")
    shutil.copytree(real_calendar, tmp_path / "trade_calendar")  # real calendar: previous(10-20) == 10-19
    assert validate_handler_postcondition("kaipan_fetch", "2026-10-20", tmp_path) == (False, "limit_up_pool_missing_or_empty")
    _catalog(tmp_path, "limit_up_pool", "2026-10-20", [{"symbol": "600000"}])
    assert not validate_handler_postcondition("kaipan_fetch", "2026-10-20", tmp_path)[0]
    _catalog(tmp_path, "limit_up_pool", "2026-10-19", [{"symbol": "600000"}])
    assert validate_handler_postcondition("kaipan_fetch", "2026-10-20", tmp_path) == (True, "")


def test_daily_kline_postcondition_reads_catalog(tmp_path):
    assert not validate_handler_postcondition("postmarket_data", "2026-10-20", tmp_path)[0]
    _catalog(tmp_path, "daily_kline", "2026-10-20", [{"symbol": "600000"}])
    assert validate_handler_postcondition("baostock_probe", "2026-10-20", tmp_path) == (True, "")


def test_calendar_postcondition_requires_lit_coverage(tmp_path):
    calendar = tmp_path / "trade_calendar" / "calendar.json"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1", "lit_from": "2026-10-01",
                                    "lit_to": "2026-10-30", "days": []}))
    assert validate_handler_postcondition("calendar_refresh", "2026-10-20", tmp_path) == (True, "")
    assert not validate_handler_postcondition("calendar_refresh", "2026-11-01", tmp_path)[0]


def test_premarket_requires_reference_manifests(tmp_path):
    assert not validate_handler_postcondition("premarket_data", "2026-10-20", tmp_path)[0]
    _catalog(tmp_path, "symbol_universe", "2026-10-20", [{"symbol": "600000"}])
    assert validate_handler_postcondition("premarket_data", "2026-10-20", tmp_path) == (True, "")


def test_partial_daily_kline_is_neither_present_nor_a_passed_postcondition(tmp_path):
    """Real shape from 2026-10-21: a timed-out Baostock attempt committed 1774 of 5224 rows (coverage 0.34)."""
    from datetime import datetime
    from engine_core.guardian.registry import catalog_partition_present
    from engine_core.data_pipeline.storage import ArtifactCatalog

    rows = [{"symbol": f"{i:06d}"} for i in range(5)]
    from conftest import use_declared_time_as_test_commit_clock
    use_declared_time_as_test_commit_clock(ArtifactCatalog(tmp_path)).commit("daily_kline", "2026-10-21", rows,
        {"available_at": "2026-10-21T17:41:00+08:00",
         "quality": {"coverage": 0.3396, "missing": 3450, "partial": True, "row_count": 5, "status": "PARTIAL"}},
        source="test")
    catalog = ArtifactCatalog(tmp_path).read()
    now = datetime.fromisoformat("2026-10-21T18:30:00+08:00")
    assert not catalog_partition_present(catalog, "daily_kline", "2026-10-21", now)
    ok, reason = validate_handler_postcondition("baostock_probe", "2026-10-21", tmp_path)
    assert not ok and reason.startswith("daily_kline_partial:0.340")
    use_declared_time_as_test_commit_clock(ArtifactCatalog(tmp_path)).commit("daily_kline", "2026-10-21", rows + [{"symbol": "000099"}],
        {"available_at": "2026-10-21T18:10:00+08:00",
         "quality": {"coverage": 1.0, "missing": 0, "partial": False, "row_count": 6, "status": "READY"}},
        source="test")
    assert catalog_partition_present(ArtifactCatalog(tmp_path).read(), "daily_kline", "2026-10-21", now)
    assert validate_handler_postcondition("baostock_probe", "2026-10-21", tmp_path) == (True, "")
