import json

import pytest

from engine_core.data_pipeline.registry import DATASETS, get_dataset
from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
from engine_core.data_pipeline.reader import load_dataset
from engine_core.data_pipeline.storage import ArtifactCatalog


DATASET_NAMES = {
    "daily_kline", "limit_up_pool", "hot_plates", "hot_rank", "plate_members",
    "limit_reasons", "auction_baseline", "ladder_daily", "stock_static",
}


def test_registry_declares_tiered_storage_and_bounded_redis_views():
    assert set(DATASETS) >= DATASET_NAMES
    assert get_dataset("daily_kline").primary_backend == "artifact"
    assert get_dataset("daily_kline").redis_ttl_seconds == 7 * 86400
    assert get_dataset("hot_plates").redis_ttl_seconds == 1800
    assert get_dataset("hot_rank").source == "ths_hot_rank"
    assert get_dataset("plate_members").max_redis_payload_bytes == 64 * 1024


@pytest.mark.parametrize("name", sorted(DATASET_NAMES))
def test_job_pipeline_persists_immutable_artifact_and_manifest(name, tmp_path):
    redis = None
    runner = JobRunner(artifact_root=tmp_path, redis=redis,
                       budget=RequestBudget(min_interval_seconds=0))
    rows = [{"symbol": "1", "value": 3, "missing": None}]
    result = runner.run(name, "2026-10-15", fetch=lambda _: rows, now="2026-10-16T10:00:00Z")
    assert result.manifest["row_count"] == 1
    assert result.manifest["storage_backend"] == "artifact"
    loaded = load_dataset(name, "2026-10-15", catalog=ArtifactCatalog(tmp_path))
    assert loaded.rows[0]["symbol"] == "000001"
    assert loaded.rows[0]["missing"] is None
    rerun = runner.run(name, "2026-10-15", fetch=lambda _: rows, now="2026-10-16T11:00:00Z")
    assert rerun.manifest["version"] == result.manifest["version"]


def test_validation_failure_does_not_publish_and_partial_fetch_circuits(tmp_path):
    runner = JobRunner(artifact_root=tmp_path, redis=None, breaker_hours=6)
    with pytest.raises(ValueError):
        runner.run("daily_kline", "2026-10-15", fetch=lambda _: [{"symbol": "bad"}],
                   now="2026-10-16T01:00:00Z")
    assert not (tmp_path / "catalog.json").exists()
    with pytest.raises(RuntimeError):
        runner.run("hot_plates", "2026-10-15",
                   fetch=lambda _: (_ for _ in ()).throw(RuntimeError("HTTP 403 token=secret")),
                   now="2026-10-16T01:00:00Z")
    with pytest.raises(RuntimeError, match="circuit_open"):
        runner.run("hot_plates", "2026-10-15", fetch=lambda _: [],
                   now="2026-10-16T02:00:00Z")
    assert "secret" not in (tmp_path / "job_state.json").read_text()


def test_backfill_does_not_move_latest_and_redis_keeps_only_manifest_when_large(tmp_path):
    class Redis:
        def __init__(self): self.values, self.ttls = {}, {}
        def set(self, key, value, ex=None):
            self.values[key] = value
            self.ttls[key] = ex
        def get(self, key): return self.values.get(key)

    redis = Redis()
    runner = JobRunner(artifact_root=tmp_path, redis=redis,
                       budget=RequestBudget(max_requests=30, min_interval_seconds=0))
    runner.run("daily_kline", "2026-10-15", fetch=lambda _: [{"symbol": "600001"}],
               now="2026-10-16T10:00:00Z", publish=True)
    latest = redis.values["core:cache:data:daily_kline:latest"]
    runner.run("daily_kline", "2026-10-14", fetch=lambda _: [{"symbol": "600002"}],
               now="2026-10-16T11:00:00Z", publish=True)
    assert json.loads(redis.values["core:cache:data:daily_kline:latest"]) == json.loads(latest)
    assert redis.ttls["core:cache:data:daily_kline:latest"] == 7 * 86400


def test_provider_budget_and_td_adapter_are_offline_only():
    from engine_core.data_pipeline.jobs import RequestBudget
    from engine_core.data_pipeline.td_adapter import DDL, validate_database_name

    budget = RequestBudget(max_requests=2, min_interval_seconds=10)
    budget.consume("kaipan", at=0)
    with pytest.raises(RuntimeError, match="rate_limited"):
        budget.consume("kaipan", at=9)
    assert len(DDL) >= 2
    with pytest.raises(ValueError):
        validate_database_name("market_data1")


def test_td_writer_maps_daily_trade_date_to_ts_primary_key():
    from engine_core.data_pipeline.registry import get_dataset
    from engine_core.data_pipeline.td_adapter import TDWriter

    class Client:
        def upsert_rows(self, database, spec, trade_date, rows, manifest, primary_key):
            assert database == "core_data"
            assert primary_key == ("ts", "symbol")
            assert rows[0]["ts"] == "2026-10-15T00:00:00"
        def count_rows(self, database, spec, trade_date): return 1

    TDWriter(Client(), "core_data").write_dataset(get_dataset("daily_kline"),
        "2026-10-15", [{"symbol": "600001", "close": 10}], {})


def test_historical_file_read_is_not_degraded_by_current_redis_ttl(tmp_path):
    runner = JobRunner(artifact_root=tmp_path, redis=None,
                       budget=RequestBudget(min_interval_seconds=0))
    runner.run("daily_kline", "2026-10-01", fetch=lambda _: [{"symbol": "600001"}],
               now="2026-10-01T18:00:00Z")
    result = load_dataset("daily_kline", "2026-10-01", prefer="file",
                          catalog=ArtifactCatalog(tmp_path), now="2026-10-16T18:00:00Z")
    assert result.status == "READY"


def test_secret_fields_are_removed_from_persisted_rows(tmp_path):
    runner = JobRunner(artifact_root=tmp_path, redis=None,
                       budget=RequestBudget(min_interval_seconds=0))
    result = runner.run("stock_static", "2026-10-15", fetch=lambda _: [{
        "symbol": "600001", "cookie": "hidden", "nested": {"authorization": "hidden"}}],
        now="2026-10-16T10:00:00Z")
    encoded = json.dumps(result.manifest) + json.dumps(result.rows)
    assert "hidden" not in encoded
    assert result.rows == [{"symbol": "600001", "nested": {}}]


def test_job_runner_dry_run_fetches_validates_and_does_not_write(tmp_path):
    runner = JobRunner(artifact_root=tmp_path, budget=RequestBudget(min_interval_seconds=0))
    calls = []
    result = runner.run("daily_kline", "2026-10-15",
                        fetch=lambda day: calls.append(day) or [{"symbol": "600001", "close": 10}],
                        now="2026-10-16T10:00:00Z", dry_run=True)
    assert calls == ["2026-10-15"]
    assert result.manifest["quality"]["status"] == "READY"
    assert not (tmp_path / "catalog.json").exists()


def test_redis_publish_reports_plan_and_verifies_readback(tmp_path, capsys):
    class Redis:
        def __init__(self): self.values = {}; self.ttls = {}
        def set(self, key, value, ex=None): self.values[key] = value; self.ttls[key] = ex
        def get(self, key): return self.values.get(key)
        def delete(self, key): self.values.pop(key, None)

    redis = Redis()
    runner = JobRunner(artifact_root=tmp_path, redis=redis,
                       budget=RequestBudget(min_interval_seconds=0))
    runner.run("daily_kline", "2026-10-15", fetch=lambda _: [{"symbol": "600001"}],
               now="2026-10-16T10:00:00Z", publish=True)
    assert "WRITE_PLAN" in capsys.readouterr().out
    assert redis.values["core:cache:data:daily_kline:latest"]
    assert all(key.startswith("core:cache:data:") for key in redis.values)


def test_redis_readback_failure_rolls_back_only_core_data_keys(tmp_path):
    class Redis:
        def __init__(self): self.values = {"legacy:key": "untouched"}; self.corrupt = False; self.writes = 0
        def set(self, key, value, ex=None): self.values[key] = value; self.writes += 1
        def get(self, key):
            if self.corrupt and self.writes and key.startswith("core:cache:data:daily_kline:") and ":sha256-" in key:
                return "{}"
            return self.values.get(key)
        def delete(self, key): self.values.pop(key, None)

    redis = Redis()
    runner = JobRunner(artifact_root=tmp_path, redis=redis,
                       budget=RequestBudget(min_interval_seconds=0))
    redis.corrupt = True
    with pytest.raises(RuntimeError, match="redis_readback_manifest_mismatch"):
        runner.run("daily_kline", "2026-10-15", fetch=lambda _: [{"symbol": "600001"}],
                   now="2026-10-16T10:00:00Z", publish=True)
    assert redis.values == {"legacy:key": "untouched"}


def test_injected_baostock_provider_separates_missing_suspended_and_mismatch():
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    class Client:
        def login(self): pass
        def logout(self): pass
        def query(self, code, day, timeout_seconds):
            if code.endswith("000001"):
                return [{"date": day, "code": code, "open": "10", "high": "11", "low": "9",
                         "close": "10.5", "preclose": "10", "volume": "100", "amount": "1050",
                         "turn": "1", "pctChg": "4.9"}]
            if code.endswith("000002"):
                return [{"date": day, "code": code, "open": "0", "high": "0", "low": "0",
                         "close": "0", "preclose": "10", "volume": "0", "amount": "0",
                         "turn": "0", "pctChg": "0"}]
            return []

    provider = BaostockDailyProvider(client_factory=Client, symbols=["000001", "000002", "000003"],
                                     sleep_fn=lambda _: None)
    result = provider.fetch("2026-10-15")
    assert result["missing_count"] == 1
    assert result["suspended_count"] == 1
    assert result["rows"][0]["mismatch"] is True
    assert result["rows"][0]["turnover"] == 1.0


def test_injected_kaipan_provider_fetches_pool_and_honors_request_limit(tmp_path):
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    class Client:
        def get_his_bans(self, day, level):
            return {"info": [[["600001", "示例", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]]]}

    provider = KaipanProvider(Client(), plate_codes=[], max_requests=3, sleep_fn=lambda _: None)
    result = provider.limit_up_pool("2026-10-15", max_levels=1)
    assert result["rows"][0]["symbol"] == "600001"
    assert result["rows"][0]["lb_days"] == 1

    partial = KaipanProvider(
        type("Reasons", (), {"get_ban_reasons": lambda _self, _symbol: {"List": []}})(),
        symbols=["600001", "600002"], max_requests=1, sleep_fn=lambda _: None,
    ).limit_reasons("2026-10-15")
    assert partial["partial"] is True
    assert partial["unqueried_symbol_count"] == 1

    checkpointed = KaipanProvider(
        type("Reasons", (), {"get_ban_reasons": lambda _self, _symbol: {"List": []}})(),
        symbols=["600001", "600002"], max_requests=1, sleep_fn=lambda _: None,
        reasons_checkpoint_path=tmp_path / "reasons.json",
    )
    assert checkpointed.limit_reasons("2026-10-15")["partial"] is True
    resumed = KaipanProvider(
        type("Reasons", (), {"get_ban_reasons": lambda _self, _symbol: {"List": []}})(),
        symbols=["600001", "600002"], max_requests=1, sleep_fn=lambda _: None,
        reasons_checkpoint_path=tmp_path / "reasons.json",
    ).limit_reasons("2026-10-15")
    assert resumed["partial"] is False
    assert resumed["unqueried_symbol_count"] == 0
