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


def test_ladder_daily_backfill_accepts_real_daily_bar_fixture(tmp_path):
    from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
    from engine_core.data_pipeline.ops import backfill_ladder_daily
    from engine_core.data_pipeline.storage import ArtifactCatalog

    fixture = json.loads((Path(__file__).parent / "fixtures/data/daily_kline_20261019_ladder_subset.json")
                         .read_text(encoding="utf-8"))
    assert fixture["provenance"]["source_sha256"].startswith("cb546c396601fe48")
    catalog = ArtifactCatalog(tmp_path)
    catalog.commit("daily_kline", "2026-10-19", fixture["rows"],
        {"trade_date": "2026-10-19", "available_at": "2026-10-19T14:34:56Z",
         "quality": {"partial": True}}, source="fixture_real_daily_kline")
    runner = JobRunner(artifact_root=tmp_path,
                       budget=RequestBudget(max_requests=10, min_interval_seconds=0))
    # C577: the relay accumulation / close-price proxy is gone. A single isolated day of bars cannot be proven
    # continuous with its predecessor and no other rung answers (live rungs off): nothing is written or invented.
    cfg = {"kaipan": {"live": False}, "wencai": {"live": False}}
    report = backfill_ladder_daily(runner, "2026-10-19", "2026-10-19", config=cfg)
    assert report["processed"] == [] and report["skipped"] == ["2026-10-19"]
    assert report["status"] == "LADDER_V1"
    assert catalog.locate("ladder_daily", "2026-10-19") is None
    _seed_c325_calendar(tmp_path)
    report = backfill_ladder_daily(runner, "2026-10-19", "2026-10-19", config=cfg)
    assert report["processed"] == [] and report["skipped"] == ["2026-10-19"]
    assert catalog.locate("ladder_daily", "2026-10-19") is None


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
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from conftest import read_real_artifact, seed_real_symbol_universe

    class Redis:
        def __init__(self): self.values = {}
        def set(self, key, value, ex=None): self.values[key] = value
        def get(self, key): return self.values.get(key)
        def ttl(self, key): return 100

    redis = Redis()
    day = "2026-10-23"
    seed_real_symbol_universe(ArtifactCatalog(tmp_path), day)
    _, rows = read_real_artifact("daily_kline", day, "formal")
    runner = JobRunner(artifact_root=tmp_path, redis=redis, budget=RequestBudget(min_interval_seconds=0))
    runner.run("daily_kline", day,
               fetch=lambda _: {"rows": rows, "source": "baostock", "provider": "baostock",
                                "provider_attempts": [{"provider": "baostock", "result": "ACCEPTED",
                                    "attempted_at": "2026-10-25T17:30:00+08:00", "elapsed_ms": 1,
                                    "reason": "real fixture replay"}]},
               now="2026-10-26T10:00:00+08:00", publish=True, source="baostock")
    report = dataset_health(tmp_path, redis=redis, trade_dates=[day])
    assert report["datasets"]["daily_kline"]["artifact_redis_consistent"] is True
    assert report["datasets"]["daily_kline"]["status"] == "GREEN"
    assert report["summary"] == "RED"  # other registered datasets have no artifact yet
    redis.values["core:cache:data:daily_kline:latest"] = "{}"
    broken = dataset_health(tmp_path, redis=redis, trade_dates=[day])
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


def _c549_seed_daily_inputs(root):
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from conftest import read_real_artifact, seed_real_symbol_universe

    day = "2026-10-23"
    _seed_c325_calendar(root)
    _, rows = read_real_artifact("daily_kline", day, "formal")
    row = rows[0]
    catalog = ArtifactCatalog(root)
    seed_real_symbol_universe(catalog, day, symbols=[row["symbol"]])
    return day, row


def _c549_default_daily_adapter(monkeypatch, root, row):
    import engine_core.data_pipeline.providers.baostock_daily_provider as provider_module
    from examples.run_data_jobs import _default_adapters

    class FakeBaostockProvider:
        def __init__(self, **_kwargs):
            pass

        def fetch(self, day, context=None):
            return {"rows": [dict(row)], "expected_count": 1,
                    "provider": "baostock", "source": "baostock",
                    "available_at": f"{day}T17:30:00+08:00"}

    monkeypatch.setattr(provider_module, "BaostockDailyProvider", FakeBaostockProvider)
    return _default_adapters(str(root), "/unused", 30, False)["daily_kline"]


def test_postmarket_default_baostock_adapter_runs_and_persists_daily_kline(monkeypatch, tmp_path):
    from engine_core.data_pipeline.storage import ArtifactCatalog

    day, row = _c549_seed_daily_inputs(tmp_path)
    adapter = _c549_default_daily_adapter(monkeypatch, tmp_path, row)

    result = run_daily_phase("postmarket", day, root=tmp_path,
        adapters={"daily_kline": adapter}, trading_dates=None, write=True,
        now=f"{day}T18:00:00+08:00", lock=False)

    assert result["errors"] == [], result
    daily_job = next(job for job in result["jobs"] if job["job"] == "daily_kline")
    assert daily_job["row_count"] == 1
    manifest_path = ArtifactCatalog(tmp_path).locate("daily_kline", day)
    assert manifest_path is not None
    assert ArtifactCatalog(tmp_path).read_rows(manifest_path)[0]["symbol"] == row["symbol"]


def test_default_baostock_adapter_supports_callable_and_fetch_protocols(monkeypatch, tmp_path):
    day, row = _c549_seed_daily_inputs(tmp_path)
    adapter = _c549_default_daily_adapter(monkeypatch, tmp_path, row)

    called = adapter(day)
    fetched = adapter.fetch(day)

    assert called["rows"][0]["symbol"] == row["symbol"]
    assert fetched["rows"][0]["symbol"] == row["symbol"]


def test_run_daily_phase_passes_callable_adapters_for_every_phase_job(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from engine_core.data_pipeline import ops
    from examples.run_data_jobs import _default_adapters

    _seed_c325_calendar(tmp_path)
    adapters = _default_adapters(str(tmp_path), "/unused", 30, False)
    adapters.update({
        "ladder_daily": lambda day: [],
        "stock_day_facts": lambda day: [],
    })
    called = []

    def capture_run(_runner, name, _day, *, fetch, **_kwargs):
        assert callable(fetch), f"{name} adapter {type(fetch).__name__} is not callable"
        called.append(name)
        return SimpleNamespace(manifest={"quality": {"status": "READY"},
                                           "row_count": 1, "version": "test",
                                           "sha256": "0" * 64})

    monkeypatch.setattr(ops.JobRunner, "run", capture_run)
    for phase in ops._PHASE_JOBS:
        result = run_daily_phase(phase, "2026-10-23", root=tmp_path,
            adapters=adapters, trading_dates=None, write=False,
            now="2026-10-23T18:00:00+08:00", lock=False)
        assert result["status"] == "READY", result

    expected = {name for names in ops._PHASE_JOBS.values() for name in names}
    assert set(called) == expected


def test_core_data_daily_main_runs_postmarket_with_injected_adapter(monkeypatch, tmp_path, capsys):
    import sys
    from types import ModuleType, SimpleNamespace
    import redis
    from conftest import read_real_artifact
    from examples import core_data_daily

    day = "2026-10-23"
    _seed_c325_calendar(tmp_path)
    _, rows = read_real_artifact("daily_kline", day, "formal")
    module = ModuleType("_c549_fake_adapter")
    module.make = lambda: {"daily_kline": lambda target: {
        "rows": [dict(rows[0])], "expected_count": 1, "provider": "baostock",
        "source": "baostock", "available_at": f"{target}T17:30:00+08:00"}}
    monkeypatch.setitem(sys.modules, module.__name__, module)

    class FakeRedis:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, ex=None):
            self.values[key] = value
            return True

        def ttl(self, _key):
            return 60

    fake_redis = FakeRedis()
    monkeypatch.setattr(redis, "Redis", SimpleNamespace(
        from_url=lambda *_args, **_kwargs: fake_redis))
    monkeypatch.setenv("CORE_REDIS_URL", "redis://offline-test")
    code = core_data_daily.main([
        "--phase", "postmarket", "--date", day, "--artifact-root", str(tmp_path),
        "--write", "--no-lock", "--adapter", "_c549_fake_adapter:make",
    ])

    output = capsys.readouterr().out
    assert code == 0, output
    assert json.loads(output.splitlines()[-1])["status"] == "READY"
