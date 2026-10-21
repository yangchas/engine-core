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
