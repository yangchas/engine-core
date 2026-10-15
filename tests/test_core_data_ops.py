from __future__ import annotations

import json

from engine_core.data_pipeline.ops import (
    FileLock, backfill_daily_kline, dataset_health, is_trading_day,
    reconcile_daily_rows, retry_window_for, run_daily_phase,
)


def test_file_lock_prevents_overlapping_runs(tmp_path):
    lock = FileLock(tmp_path / "job.lock")
    with lock:
        other = FileLock(tmp_path / "job.lock")
        try:
            other.acquire()
            assert False, "second lock acquired"
        except RuntimeError as exc:
            assert str(exc) == "job_already_running"


def test_non_trading_day_is_recorded_without_fetch(tmp_path):
    calls = []
    result = run_daily_phase("premarket", "2026-10-18", root=tmp_path,
                             adapters={}, trading_dates={"2026-10-16"},
                             write=True, redis=None)
    assert result["status"] == "SKIPPED_NON_TRADING_DAY"
    assert calls == []
    assert json.loads((tmp_path / "daily_status.json").read_text())["status"] == result["status"]


def test_daily_kline_retry_windows_are_bounded_and_ladder_computation_is_gated():
    assert retry_window_for("daily_kline", "17:39") is None
    assert retry_window_for("daily_kline", "17:40") == 0
    assert retry_window_for("daily_kline", "18:30") == 1
    assert retry_window_for("daily_kline", "20:00") == 2
    assert retry_window_for("daily_kline", "20:01") is None


def test_backfill_resume_skips_existing_artifacts_and_respects_budget(tmp_path):
    from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
    runner = JobRunner(artifact_root=tmp_path, budget=RequestBudget(min_interval_seconds=0))
    runner.run("daily_kline", "2026-10-15", fetch=lambda _: [{"symbol": "600001", "close": 10}],
               now="2026-10-16T10:00:00Z")
    called = []
    result = backfill_daily_kline(runner, "2026-10-15", "2026-10-16",
                                  fetch=lambda day: called.append(day) or [{"symbol": "600001", "close": 11}],
                                  max_requests=1, write=True)
    assert called == ["2026-10-16"]
    assert result["skipped_existing"] == ["2026-10-15"]
    assert result["processed"] == ["2026-10-16"]


def test_reconciliation_finds_close_and_amount_mismatches():
    report = reconcile_daily_rows(
        [{"symbol": "600001", "close": 10, "amount": 100}],
        [{"symbol": "600001", "close": 10.2, "amount": 105}],
    )
    assert report["mismatch_count"] == 1
    assert report["mismatches"][0]["fields"] == ["close", "amount"]


def test_health_reports_artifact_redis_consistency_and_red_status(tmp_path):
    from engine_core.data_pipeline.jobs import JobRunner, RequestBudget

    class Redis:
        def __init__(self): self.values = {}
        def set(self, key, value, ex=None): self.values[key] = value
        def get(self, key): return self.values.get(key)
        def ttl(self, key): return 100

    redis = Redis()
    runner = JobRunner(artifact_root=tmp_path, redis=redis, budget=RequestBudget(min_interval_seconds=0))
    runner.run("daily_kline", "2026-10-15", fetch=lambda _: [{"symbol": "600001"}],
               now="2026-10-16T10:00:00Z", publish=True)
    report = dataset_health(tmp_path, redis=redis, trade_dates=["2026-10-15"])
    assert report["datasets"]["daily_kline"]["artifact_redis_consistent"] is True
    assert report["datasets"]["daily_kline"]["status"] == "GREEN"
    assert report["summary"] == "RED"  # other registered datasets have no artifact yet
    redis.values["core:cache:data:daily_kline:latest"] = "{}"
    broken = dataset_health(tmp_path, redis=redis, trade_dates=["2026-10-15"])
    assert broken["datasets"]["daily_kline"]["status"] == "RED"
    assert broken["summary"] == "RED"


def test_trading_day_uses_supplied_calendar():
    assert is_trading_day("2026-10-16", {"2026-10-16"})
    assert not is_trading_day("2026-10-18", {"2026-10-16"})
