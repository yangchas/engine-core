from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.hot_plates_builder import build_hot_plates
from engine_core.data_pipeline.hot_plates_gaps import (
    reconcile_due_hot_plates_gaps, record_hot_plates_gap,
)
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.guardian.commands import validate_handler_postcondition


FIXTURE = Path(__file__).parent / "fixtures" / "real" / "kaipan_hot_plates_2026-10-23_1441.json"


def test_gap_only_records_missing_slot_and_references_previous_snapshot(tmp_path):
    response = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prior = build_hot_plates({
        "trade_date": "2026-10-23", "captured_at": "2026-10-23T09:14:00+08:00",
        "response": response, "page_size": 50,
    })
    catalog = ArtifactCatalog(tmp_path)
    manifest = catalog.commit("hot_plates", "2026-10-23", prior.rows,
        {**prior.manifest, "quality": {"status": "READY"}}, source="kaipan_getHisPlates")
    declared = ["hot_plates", "kaipan_hot_091610"]
    assert validate_handler_postcondition("kaipan_hot_capture", "2026-10-23",
                                          tmp_path, declared) == (False, "hot_plates_event_missing")

    gap = record_hot_plates_gap(tmp_path, "2026-10-23", "kaipan_hot_091610",
        "2026-10-23T09:16:10+08:00", "2026-10-23T09:17:30+08:00",
        "2026-10-23T09:17:31+08:00", "provider_failed")

    assert gap["status"] == "GAP"
    assert gap["late_policy"] == "gap_only"
    assert gap["fallback_status"] == "STALE"
    assert gap["stale_version"] == manifest["version"]
    assert gap["stale_captured_at"] == "2026-10-23T09:14:00+08:00"
    assert validate_handler_postcondition("kaipan_hot_capture", "2026-10-23",
                                          tmp_path, declared) == (True, "")
    repeated = record_hot_plates_gap(tmp_path, "2026-10-23", "kaipan_hot_091610",
        "2026-10-23T09:16:10+08:00", "2026-10-23T09:17:30+08:00",
        "2026-10-23T09:18:00+08:00", "provider_failed")
    assert repeated == gap


def test_gap_reconcile_does_not_record_its_own_non_capture_window(tmp_path):
    schedule = tmp_path / "schedule.json"
    schedule.write_text(json.dumps({"events": [
        {"event_id": "kaipan_hot_091610", "handler": "kaipan_hot_capture",
         "at": "09:16:10", "deadline": "09:17:30", "args": {"datasets": ["hot_plates"]}},
        {"event_id": "hot_plates_gap_reconcile_151500", "handler": "kaipan_hot_capture",
         "at": "09:18:00", "deadline": "09:19:00",
         "args": {"reconcile_only": True, "datasets": ["hot_plates_gaps"]}},
    ]}), encoding="utf-8")

    gaps = reconcile_due_hot_plates_gaps(
        tmp_path, "2026-10-23", "2026-10-23T09:20:00+08:00", schedule_path=schedule)

    assert [gap["event_id"] for gap in gaps] == ["kaipan_hot_091610"]


def test_late_history_window_records_one_gap_per_source_date(tmp_path, monkeypatch):
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

    dates = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
    monkeypatch.setattr(TradeCalendarStore, "all_trading_dates", lambda _self: dates)
    schedule = tmp_path / "schedule.json"
    schedule.write_text(json.dumps({"events": [
        {"event_id": "hot_plates_history_0815", "handler": "kaipan_hot_capture",
         "at": "08:15:00", "deadline": "08:30:00",
         "args": {"hot_plates_history_days": 3,
                  "datasets": ["hot_plates_history", "hot_plates_history_days=3"]}},
    ]}), encoding="utf-8")

    gaps = reconcile_due_hot_plates_gaps(tmp_path, "2026-10-29",
        "2026-10-29T08:31:00+08:00", schedule_path=schedule)

    assert [gap["event_id"] for gap in gaps] == [
        "kaipan_history_20261026", "kaipan_history_20261027", "kaipan_history_20261028"]
    assert all(gap["status"] == "GAP" and gap["late_policy"] == "gap_only"
               for gap in gaps)


def test_history_job_started_after_deadline_records_gaps_without_building_provider(
        tmp_path, monkeypatch, capsys):
    from datetime import datetime as RealDateTime
    from zoneinfo import ZoneInfo
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
    from examples import run_data_jobs

    dates = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
    monkeypatch.setattr(TradeCalendarStore, "all_trading_dates", lambda _self: dates)

    monkeypatch.setattr(run_data_jobs, "_default_adapters",
                        lambda *_args, **_kwargs: pytest.fail("provider adapters must not be built"))

    code = run_data_jobs.main([
        "--job", "hot_plates", "--date", "2026-10-29", "--artifact-root", str(tmp_path),
        "--hot-plates-history-days", "3", "--hot-plates-scheduled-at", "08:15:00",
        "--hot-plates-deadline", "08:30:00", "--execute",
    ], now_fn=lambda: RealDateTime(2026, 10, 29, 8, 31, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert code == 0
    assert len([line for line in capsys.readouterr().out.splitlines()
                if '"status": "GAP"' in line]) == 3
    for day in dates[:3]:
        path = ArtifactCatalog(tmp_path).locate("hot_plates_gaps", day)
        assert path is not None
        assert any(row["event_id"] == f"kaipan_history_{day.replace('-', '')}"
                   for row in ArtifactCatalog(tmp_path).read_rows(path))
