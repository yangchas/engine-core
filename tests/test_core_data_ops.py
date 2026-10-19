from __future__ import annotations

import json
import hashlib
from pathlib import Path
import pytest

from engine_core.data_pipeline.ops import (
    FileLock, backfill_daily_kline, dataset_health, is_trading_day,
    reconcile_daily_rows, retry_window_for, run_daily_phase,
)

def _seed_c325_calendar(root):
    fixture = Path(__file__).parent / "fixtures" / "trade_calendar"
    payload = json.loads((fixture / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    dates = [row["calendar_date"] for row in payload["rows"] if row["is_trading_day"] == "1"]
    raw = {path.name: path.read_bytes() for path in fixture.glob("*.json")}
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
    TradeCalendarStore(root).publish_confirmed(dates, source="C325 BaoStock+SZSE",
        fetched_at="2026-10-20T00:00:00+00:00", coverage_start="2026-01-01",
        coverage_end="2026-12-31", raw_responses=raw)


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
    _seed_c325_calendar(tmp_path)
    calls = []
    result = run_daily_phase("premarket", "2026-10-18", root=tmp_path,
                             adapters={}, trading_dates=None,
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


def test_symbol_universe_rejects_fixture_derived_empty_duplicate_and_missing_fields(tmp_path):
    from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
    from examples.run_data_jobs import _normalize_universe_rows
    fixture = Path(__file__).parent / "fixtures" / "symbol_universe" / "baostock_query_all_stock_20261016.json"
    source = json.loads(fixture.read_text(encoding="utf-8"))
    rows = _normalize_universe_rows(source)
    for row in rows:
        row["universe_asof"] = "2026-10-16"
    with pytest.raises(ValueError, match="quality_invalid:empty_response"):
        JobRunner(artifact_root=tmp_path, budget=RequestBudget(min_interval_seconds=0)).run(
            "symbol_universe", "2026-10-16", fetch=lambda _: [], now="2026-10-16T08:30:00+08:00", dry_run=True)
    duplicate = [rows[0], rows[0]]
    with pytest.raises(ValueError, match="quality_invalid:duplicate_symbol"):
        JobRunner(artifact_root=tmp_path, budget=RequestBudget(min_interval_seconds=0)).run(
            "symbol_universe", "2026-10-16", fetch=lambda _: duplicate,
            now="2026-10-16T08:30:00+08:00", dry_run=True)
    missing = [dict(rows[0])]
    missing[0].pop("name")
    runner = JobRunner(artifact_root=tmp_path, budget=RequestBudget(min_interval_seconds=0))
    with pytest.raises(ValueError, match="schema_invalid:required_field_missing"):
        runner.run("symbol_universe", "2026-10-16", fetch=lambda _: missing,
                   now="2026-10-16T08:30:00+08:00", dry_run=True)


def test_symbol_universe_rejects_excessive_change_from_previous_snapshot():
    from examples.run_data_jobs import _normalize_universe_rows, _universe_delta
    fixture = Path(__file__).parent / "fixtures" / "symbol_universe" / "baostock_query_all_stock_20261016.json"
    actual_rows = _normalize_universe_rows(json.loads(fixture.read_text(encoding="utf-8")))
    previous = {row["symbol"] for row in actual_rows[:100]}
    current = {actual_rows[0]["symbol"]}
    with pytest.raises(RuntimeError, match="quality_invalid:universe_change_over_10pct"):
        _universe_delta(current, previous)


def test_baostock_fixture_adapter_filters_exchange_security_type_and_rejects_identity_conflict():
    from examples.run_data_jobs import _normalize_universe_rows
    fixture = Path(__file__).parent / "fixtures" / "symbol_universe" / "baostock_query_all_stock_20261016.json"
    raw = fixture.read_bytes()
    rows = json.loads(raw)
    provenance = json.loads(fixture.with_suffix(".provenance.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == provenance["sha256"]
    # Transform actual source rows, retaining their provenance and original exchange code.
    stock = next(row for row in rows if row[0].startswith(("sh.60", "sh.68", "sz.00", "sz.30")))
    index = next(row for row in rows if row[0].startswith(("sh.000", "sz.399")))
    converted = _normalize_universe_rows([*rows, index])
    assert all(row["code"].startswith(("sh.60", "sh.68", "sz.00", "sz.30")) for row in converted)
    assert any(row["code"] == stock[0] for row in converted)
    sh_stock = next(row for row in rows if row[0].startswith("sh.60"))
    with pytest.raises(RuntimeError, match="quality_invalid:universe_identity_conflict"):
        _normalize_universe_rows([*rows, ["sz." + sh_stock[0][3:], *sh_stock[1:]]])
    conflict = [*rows, [stock[0], stock[1], stock[2] + " conflicting identity"]]
    with pytest.raises(RuntimeError, match="quality_invalid:universe_duplicate_conflict"):
        _normalize_universe_rows(conflict)


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


def test_postmarket_appends_injected_auction_total_to_core_baseline(tmp_path):
    _seed_c325_calendar(tmp_path)
    from engine_core.data_pipeline.storage import ArtifactCatalog

    catalog = ArtifactCatalog(tmp_path)
    catalog.commit("auction_baseline", "2026-10-15", [
        {"date": "2026-10-14", "sum_yi": 100.0, "available_at": "2026-10-15T17:30:00+08:00"},
    ], {"available_at": "2026-10-15T17:30:00+08:00"}, source="test")
    result = run_daily_phase("postmarket", "2026-10-16", root=tmp_path,
        adapters={"auction_amount_provider": lambda day: {
            "amount_yuan": 250_000_000, "available_at": "2026-10-16T17:30:00+08:00"}},
        trading_dates=None, write=True,
        now="2026-10-16T18:00:00+08:00", lock=False)
    assert result["auction_baseline_append"]["status"] == "READY"
    rows = catalog.read_rows(catalog.locate("auction_baseline", "2026-10-16"))
    assert rows[-1] == {"date": "2026-10-16", "sum_yi": 2.5,
                        "available_at": "2026-10-16T17:30:00+08:00",
                        "source": "postmarket_auction_summary"}


def test_premarket_jobs_use_previous_trading_date_for_kaipan_products(tmp_path):
    _seed_c325_calendar(tmp_path)
    called = []

    def fetch(day):
        called.append(day)
        return [{"date": day, "symbol": "000001"}]

    run_daily_phase("premarket", "2026-10-16", root=tmp_path,
        adapters={name: fetch for name in ("plate_members", "limit_up_pool",
                                           "limit_reasons", "hot_plates")},
        trading_dates=None, write=False,
        now="2026-10-16T08:50:00+08:00", lock=False)
    assert called == ["2026-10-15"] * 4
