from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.hot_plates_gaps import (
    reconcile_due_hot_plates_gaps, record_hot_plates_gap,
)
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.guardian.commands import validate_handler_postcondition


@pytest.fixture(autouse=True)
def gap_redis(monkeypatch):
    from fnmatch import fnmatch
    from engine_core.data_pipeline import limit_pool_hot_view, redis_day_view

    class Redis:
        def __init__(self):
            self.values, self.expires = {}, {}
        def scan_iter(self, match, count=100):
            return [key for key in self.values if fnmatch(key, match)]
        def get(self, key):
            return self.values.get(key)
        def set(self, key, value, ex, nx=False):
            if nx and key in self.values:
                return False
            self.values[key] = value
            self.expires[key] = ex
            return True
    redis = Redis()
    monkeypatch.setattr(limit_pool_hot_view, '_client', lambda: redis)
    monkeypatch.setattr(redis_day_view, 'expire_at', lambda _day: 4070908800)
    return redis


def test_gap_only_records_missing_slot_and_references_previous_snapshot(tmp_path, monkeypatch, gap_redis):
    from engine_core.data_pipeline import limit_pool_hot_view

    fixture_dir = Path(__file__).parent / "fixtures/real/c1094"
    raw = (fixture_dir / "view.json").read_bytes()
    view = json.loads(raw)
    key = json.loads((fixture_dir / "PROVENANCE.json").read_text())["view.json"]["key"]

    gap_redis.values[key] = raw
    day = view["trade_date"]
    declared = ["hot_plates", "kaipan_hot_091610"]
    assert validate_handler_postcondition("kaipan_hot_capture", day,
                                          tmp_path, declared) == (False, "hot_plates_event_missing")

    gap = record_hot_plates_gap(tmp_path, day, "kaipan_hot_091610",
        f"{day}T15:16:10+08:00", f"{day}T15:17:30+08:00",
        f"{day}T15:17:31+08:00", "provider_failed")

    assert gap["status"] == "GAP"
    assert gap["late_policy"] == "gap_only"
    assert gap["fallback_status"] == "STALE"
    assert gap["stale_version"] == view["version"]
    assert gap["stale_captured_at"] == view["captured_at"]
    assert validate_handler_postcondition("kaipan_hot_capture", day,
                                          tmp_path, declared) == (True, "")
    repeated = record_hot_plates_gap(tmp_path, day, "kaipan_hot_091610",
        f"{day}T15:16:10+08:00", f"{day}T15:17:30+08:00",
        f"{day}T15:18:00+08:00", "provider_failed")
    assert repeated == gap


def test_gap_backfill_uses_24_hour_floor_and_real_calendar_expiry(tmp_path, monkeypatch, gap_redis):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine_core.data_pipeline import hot_plates_gaps, redis_day_view
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
    assert TradeCalendarStore(Path.home() / "core-data").nth_trade_date("2026-11-10", 5).isoformat() == "2026-11-17"

    now = datetime(2026, 11, 16, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    short_deadline = int(now) + 58_911
    monkeypatch.setattr(redis_day_view, "expire_at", lambda _day: short_deadline)
    monkeypatch.setattr(redis_day_view, "_now_timestamp", lambda: now)
    monkeypatch.setattr(redis_day_view.clock, "time", lambda: now)

    record_hot_plates_gap(tmp_path, "2026-11-09", "backfill", "2026-11-09T09:16:10+08:00",
                          "2026-11-09T09:17:30+08:00", "2026-11-16T06:00:00+08:00", "provider_failed")
    key = "core:cache:data:hot_plates_gaps:2026-11-09:backfill"
    assert gap_redis.values[key]
    assert gap_redis.expires[key] == 86_400



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
        from engine_core.data_pipeline.hot_plates_gaps import _existing_gap_rows
        assert ArtifactCatalog(tmp_path).locate("hot_plates_gaps", day) is None
        assert any(row["event_id"] == f"kaipan_history_{day.replace('-', '')}"
                   for row in _existing_gap_rows(tmp_path, day))


def test_real_legacy_gap_remains_readable_and_new_gap_uses_standard_envelope(tmp_path, monkeypatch, gap_redis):
    import re
    from engine_core.data_pipeline.hot_plates_gaps import _existing_gap_rows
    from engine_core.data_pipeline.reader import load_dataset

    fixture = Path(__file__).parent / 'fixtures/real/c1117'
    raw = (fixture / 'gap.json').read_bytes()
    old = json.loads(raw)
    key = json.loads((fixture / 'PROVENANCE.json').read_text())['gap.json']['key']
    gap_redis.values[key] = raw
    assert _existing_gap_rows(tmp_path, old['trade_date']) == [old]
    assert load_dataset('hot_plates_gaps', old['trade_date'], redis=gap_redis).rows == [old]
    view = json.loads((Path(__file__).parent / 'fixtures/real/c1094/view.json').read_bytes())
    from engine_core.data_pipeline import hot_plates_gaps
    monkeypatch.setattr(hot_plates_gaps, '_snapshot_manifests', lambda *_: [view])
    gap = record_hot_plates_gap(tmp_path, old['trade_date'], old['event_id'] + '_new',
        old['observed_at'], old['observed_at'], old['observed_at'], old['gap_reason'])
    assert gap['schema_version'] == 1
    assert gap['dataset'] == 'hot_plates_gaps'
    assert gap['source'] == 'guardian_gap_only'
    assert gap['version'] == gap['event_id']
    assert gap['captured_at'] == gap['available_at'] == gap['observed_at']
    assert gap['trade_date'] == old['trade_date']
    for field in ('scheduled_at', 'deadline', 'observed_at', 'stale_captured_at', 'captured_at', 'available_at'):
        assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00', gap[field])
    assert json.loads(gap_redis.values[key.rsplit(':', 1)[0] + ':' + gap['event_id']]) == gap
