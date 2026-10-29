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
    from conftest import read_real_artifact, seed_real_symbol_universe

    day = "2026-10-23"
    _, rows = read_real_artifact("daily_kline", day, "formal")
    from conftest import use_declared_time_as_test_commit_clock
    catalog_writer = use_declared_time_as_test_commit_clock(ArtifactCatalog(tmp_path))
    seed_real_symbol_universe(catalog_writer, day)
    catalog_writer.commit("daily_kline", day, rows[:1774],
        {"trade_date": day, "provider": "baostock", "available_at": f"{day}T17:41:00+08:00"},
        source="baostock")
    catalog = ArtifactCatalog(tmp_path).read()
    now = datetime.fromisoformat(f"{day}T18:30:00+08:00")
    assert not catalog_partition_present(catalog, "daily_kline", day, now)
    ok, reason = validate_handler_postcondition("baostock_probe", day, tmp_path)
    assert not ok and reason.startswith("daily_kline_partial:0.340")
    catalog_writer.commit("daily_kline", day, rows,
        {"trade_date": day, "provider": "baostock", "available_at": f"{day}T18:10:00+08:00"},
        source="baostock")
    assert catalog_partition_present(ArtifactCatalog(tmp_path).read(), "daily_kline", day, now)
    assert validate_handler_postcondition("baostock_probe", day, tmp_path) == (True, "")


def test_hot_plate_history_postcondition_requires_each_prior_session_saved_or_gapped(
        tmp_path, monkeypatch):
    from engine_core.data_pipeline.hot_plates_gaps import record_hot_plates_gap
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

    dates = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
    monkeypatch.setattr(TradeCalendarStore, "all_trading_dates", lambda _self: dates)
    datasets = ["hot_plates_history", "hot_plates_history_days=3"]
    assert not validate_handler_postcondition("kaipan_hot_capture", "2026-10-29",
                                              tmp_path, datasets)[0]

    catalog = ArtifactCatalog(tmp_path)
    for day in dates[:2]:
        event_id = f"kaipan_history_{day.replace('-', '')}"
        catalog.commit("hot_plates", day, [{"event_id": event_id}],
            {"event_id": event_id, "captured_at": "2026-10-29T08:20:00+08:00"},
            source="kaipan_getHisPlates")
    record_hot_plates_gap(tmp_path, dates[2], "kaipan_history_20261028",
        "2026-10-29T08:15:00+08:00", "2026-10-29T08:30:00+08:00",
        "2026-10-29T08:31:00+08:00", "provider_failure")

    assert validate_handler_postcondition("kaipan_hot_capture", "2026-10-29",
                                          tmp_path, datasets) == (True, "")
