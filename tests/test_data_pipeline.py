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


def test_ths_hot_rank_provider_handles_empty_heat_and_403_circuit():
    from engine_core.data_pipeline.providers.ths_hot_rank import ThsHotRankProvider, ProviderCircuitOpen

    class Response:
        status_code = 200
        def json(self):
            return {"status_code": 0, "data": {"stock_list": [
                {"code": "SZ000001", "name": "样本", "rate": None},
                {"code": "SH600000", "name": "样本二", "rate": "12.5"},
            ]}}
    class Session:
        def __init__(self, response): self.response, self.calls = response, 0
        def get(self, *args, **kwargs): self.calls += 1; return self.response

    session = Session(Response())
    provider = ThsHotRankProvider(session=session, sleep_fn=lambda _: None)
    rows = provider.fetch()
    assert rows == [
        {"symbol": "000001", "code": "SZ000001", "name": "样本", "rank": 1, "source": "ths_hot_rank"},
        {"symbol": "600000", "code": "SH600000", "name": "样本二", "rank": 2, "heat": 12.5, "source": "ths_hot_rank"},
    ]
    assert provider.normalize({"status_code": 0, "data": {"stock_list": []}}) == []
    class EmptyResponse(Response):
        def json(self): return {"status_code": 0, "data": {"stock_list": []}}
    with pytest.raises(RuntimeError, match="ths_hot_rank_fetch_failed"):
        ThsHotRankProvider(session=Session(EmptyResponse()), retries=1,
                           sleep_fn=lambda _: None).fetch()
    blocked = Session(type("Blocked", (), {"status_code": 403})())
    circuit = ThsHotRankProvider(session=blocked, retries=3, sleep_fn=lambda _: None)
    with pytest.raises(ProviderCircuitOpen): circuit.fetch()
    with pytest.raises(ProviderCircuitOpen): circuit.fetch()
    assert blocked.calls == 1


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


def test_daily_kline_without_universe_does_not_publish_latest(tmp_path):
    class Redis:
        def __init__(self): self.values, self.ttls = {}, {}
        def set(self, key, value, ex=None):
            self.values[key] = value
            self.ttls[key] = ex
        def get(self, key): return self.values.get(key)

    redis = Redis()
    runner = JobRunner(artifact_root=tmp_path, redis=redis,
                       budget=RequestBudget(max_requests=30, min_interval_seconds=0))
    result = runner.run("daily_kline", "2026-10-15", fetch=lambda _: [{"symbol": "600001"}],
                        now="2026-10-16T10:00:00Z", publish=True)
    assert result.manifest["quality"]["expected_basis"] == "universe_unavailable"
    assert result.manifest["quality"]["coverage"] is None
    assert result.manifest["business_status"] != "FORMAL"
    runner.run("daily_kline", "2026-10-14", fetch=lambda _: [{"symbol": "600002"}],
               now="2026-10-16T11:00:00Z", publish=True)
    assert "core:cache:data:daily_kline:latest" not in redis.values


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
    from conftest import read_real_artifact, seed_real_symbol_universe

    class Redis:
        def __init__(self): self.values = {}; self.ttls = {}
        def set(self, key, value, ex=None): self.values[key] = value; self.ttls[key] = ex
        def get(self, key): return self.values.get(key)
        def delete(self, key): self.values.pop(key, None)

    redis = Redis()
    day = "2026-10-23"
    seed_real_symbol_universe(ArtifactCatalog(tmp_path), day)
    _, rows = read_real_artifact("daily_kline", day, "formal")
    runner = JobRunner(artifact_root=tmp_path, redis=redis,
                       budget=RequestBudget(min_interval_seconds=0))
    runner.run("daily_kline", day,
               fetch=lambda _: {"rows": rows, "source": "baostock", "provider": "baostock",
                                "provider_attempts": [{"provider": "baostock", "result": "ACCEPTED",
                                    "attempted_at": "2026-10-25T17:30:00+08:00", "elapsed_ms": 1,
                                    "reason": "real fixture replay"}]},
               now="2026-10-26T10:00:00+08:00", publish=True, source="baostock")
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

    class NoisyClient:
        def get_his_bans(self, day, level):
            return {"info": [[
                ["600001", "短元数据"],
                ["600001", "有效", *([""] * 14)],
                ["600002", "重复", *([""] * 14)],
            ]]}

    guarded = KaipanProvider(NoisyClient(), plate_codes=[], max_requests=1,
                             sleep_fn=lambda _: None).limit_up_pool("2026-10-15", max_levels=1)
    assert [row["symbol"] for row in guarded["rows"]] == ["600001", "600002"]
    assert guarded["rows"][0]["name"] == "有效"

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


def test_kaipan_provider_uses_sdk_plate_methods_and_normalizes_rows():
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    class SDK:
        def getHisPlates(self, date, index, size):
            assert (date, index, size) == ("2026-10-16", "0", "50")
            return {"list": [["801001", "芯片", 12300, 1.25, 0, 500]],
                    "Count": 1, "Day": ["2026-10-16"]}

        def getHisPlateIds(self, p, date, index, size):
            assert (p, date, index, size) == ("801001", "2026-10-16", "0", "30")
            return {"list": [["600001", "示例", "", 0, "芯片"]],
                    "Count": 1, "Day": ["2026-10-16"]}

    provider = KaipanProvider(SDK(), plate_codes=["801001"], max_requests=3,
                              sleep_fn=lambda _: None)
    plates = provider.hot_plates("2026-10-16")
    members = provider.plate_members("2026-10-16", page_size=30)
    assert [{key: value for key, value in row.items()
             if key not in {"captured_at", "available_at"}} for row in plates["rows"]] == [
        {"plate_code": "801001", "name": "芯片", "strength": 12300,
         "change_pct": 1.25, "amount_yuan": 500, "trade_date": "2026-10-16"}]
    assert plates["rows"][0]["captured_at"] == plates["rows"][0]["available_at"]
    assert plates["rows"][0]["available_at"] == plates["available_at"]
    assert members["rows"] == [{"plate_code": "801001", "symbol": "600001",
                                "plate_class": "UNKNOWN", "plate_member_label": "芯片",
                                "trade_date": "2026-10-16"}]


def test_kaipan_default_job_uses_direct_sdk_and_resumable_member_checkpoints(tmp_path, monkeypatch):
    import importlib
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    import examples.run_data_jobs as run_jobs
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    assert not run_jobs._is_complete_member_manifest({"quality": "REFERENCE", "row_count": 87364})
    assert not run_jobs._is_complete_member_manifest({"quality": {"status": "PARTIAL"}, "row_count": 6815})
    assert run_jobs._is_complete_member_manifest({"quality": {"status": "READY"}, "row_count": 88646})

    # The default adapter must lazily load the separately installed pykaipan
    # SDK directly, without importing the legacy adapter module.
    calls = []
    def get_ids(plate, date, index, size):
        if plate == "801002":
            raise OSError("temporary")
        return {"list": [["600001", "甲"]], "Count": 1}

    sdk = SimpleNamespace(
        getHisPlates=lambda **kwargs: {"list": [["801001", "测试"]], "Count": 1},
        getHisPlateIds=get_ids,
        getHisBans=lambda **kwargs: {"info": []},
        getHisPlateMins=lambda *args, **kwargs: {},
        getBanReasons=lambda *args, **kwargs: {"List": []},
    )
    original_import = importlib.import_module

    def direct_import(name, *args, **kwargs):
        calls.append(name)
        if name == "pykaipan.pykaipan":
            return sdk
        if name == "examples.legacy_adapters":
            raise AssertionError("default Kaipan path imported legacy adapter")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", direct_import)
    monkeypatch.setattr(run_jobs, "_plate_codes", lambda *args: ["801001", "801002"])
    adapters = run_jobs._default_adapters(str(tmp_path), "/unused", 30, True)
    hot = adapters["hot_plates"]("2026-10-16")
    assert hot["rows"][0]["plate_code"] == "801001"
    assert calls == ["pykaipan.pykaipan"]

    provider = KaipanProvider(
        sdk, plate_codes=["801001", "801002"], max_requests=10,
        min_interval_seconds=0.2, sleep_fn=lambda _: None,
        members_checkpoint_path=tmp_path / "members.json",
    )
    assert provider.min_interval_seconds == 0.2
    first = provider.plate_members("2026-10-16", concurrency=4)
    assert first["rows"][0]["symbol"] == "600001"
    assert first["failed_plates"] == [{"plate_code": "801002", "error_type": "RuntimeError"}]
    assert first["partial"] is True

    retried = []
    sdk.getHisPlateIds = lambda plate, date, index, size: retried.append(plate) or {
        "list": [["600002", "乙"]], "Count": 1}
    resumed = KaipanProvider(
        sdk, plate_codes=["801001", "801002"], max_requests=10,
        min_interval_seconds=1, sleep_fn=lambda _: None,
        members_checkpoint_path=tmp_path / "members.json",
    ).plate_members("2026-10-16", concurrency=4)
    assert retried == ["801002"]
    assert {row["symbol"] for row in resumed["rows"]} == {"600001", "600002"}
    assert resumed["partial"] is False

    source = Path(run_jobs.__file__).read_text(encoding="utf-8")
    assert "examples.legacy_adapters" not in source


def test_kaipan_provider_keeps_response_day_as_metadata_not_requested_date():
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    class SDK:
        def getHisPlates(self, date, index, size):
            return {"list": [["801001", "芯片"]], "Count": 1,
                    "Day": ["2026-10-16"], "Time": 1792220400,
                    "Min": "0925", "Max": "1500"}

    result = KaipanProvider(SDK(), sleep_fn=lambda _: None).hot_plates("2026-10-16")
    assert result["rows"][0]["trade_date"] == "2026-10-16"
    assert result["requested_date"] == "2026-10-16"
    assert result["response_time"] == 1792220400
    assert result["response_min"] == "0925"
    assert result["response_max"] == "1500"
    assert result["response_day"] == ["2026-10-16"]


def test_kaipan_provider_normalizes_historical_plate_minute_trend():
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    class SDK:
        def getHisPlateMins(self, plate_code, trade_date):
            assert (plate_code, trade_date) == ("801001", "2026-10-15")
            return {"trend": [["09:30", 12.3, 4, 5, 0], ["09:31", 12.4, 6, 7, 0]],
                    "begin_px": 12.0, "hprice": 12.4, "lprice": 11.9,
                    "preclose_px": 12.1, "day": "2026-10-15"}

    result = KaipanProvider(SDK(), min_interval_seconds=1,
                            sleep_fn=lambda _: None).plate_minutes("801001", "2026-10-15")
    assert result["rows"] == [
        {"plate_code": "801001", "trade_date": "2026-10-15", "time": "09:30",
         "event_time": "2026-10-15T09:30:00+08:00", "minute_index": 0,
         "price": 12.3, "volume": 4, "amount": 5},
        {"plate_code": "801001", "trade_date": "2026-10-15", "time": "09:31",
         "event_time": "2026-10-15T09:31:00+08:00", "minute_index": 1,
         "price": 12.4, "volume": 6, "amount": 7},
    ]
    assert result["metadata"] == {"begin_px": 12.0, "hprice": 12.4,
                                  "lprice": 11.9, "preclose_px": 12.1,
                                  "day": "2026-10-15"}


def test_limit_reasons_marks_symbols_whose_target_day_reason_was_overwritten_as_partial():
    """STUB client (shape captured from real Kaipan 2026-10-21: only the latest reason is returned, dated today)."""
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    class Client:
        def get_ban_reasons(self, symbol):
            date = "2026-10-21" if symbol == "001239" else "2026-10-20"
            return {"StockID": symbol, "List": [{"Date": date, "Type": "0", "Reason": "r", "ZSCode": []}]}

    result = KaipanProvider(Client(), max_requests=10, min_interval_seconds=0).limit_reasons(
        "2026-10-20", symbols=["001239", "600178"])
    assert [row["symbol"] for row in result["rows"]] == ["600178"]
    assert result["expected_count"] == 2 and result["coverage_count"] == 1
    assert result["overwritten_symbol_count"] == 1 and result["partial"] is True


def test_baostock_capture_resumes_from_checkpoint_after_a_mid_run_timeout_without_losing_progress(tmp_path):
    """STUB client for the unreachable/timeout branch (the real resume was verified live on 2026-10-21: 1774 -> full)."""
    import json
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    symbols = [f"{i:06d}" for i in range(1, 61)]
    calls = []

    class Client:
        stop_after = 40
        def login(self): pass
        def logout(self): pass
        def query(self, code, day, timeout_seconds):
            calls.append(code)
            if len(calls) > self.stop_after:
                raise TimeoutError("BAOSTOCK_TIMEOUT")
            return [{"date": day, "code": code, "open": "10", "high": "11", "low": "9", "close": "10",
                     "preclose": "10", "volume": "100", "amount": "1000", "turn": "1", "pctChg": "0"}]

    checkpoint = tmp_path / "kline.json"
    provider = BaostockDailyProvider(client_factory=Client, symbols=symbols, sleep_fn=lambda _: None,
                                     checkpoint_path=checkpoint)
    with pytest.raises(TimeoutError):
        provider.fetch("2026-10-21")
    # progress up to the timeout is on disk (batched saves + save-on-exit), not lost
    assert len(json.loads(checkpoint.read_text())["rows"]) == 40
    Client.stop_after = 10_000
    calls.clear()
    result = BaostockDailyProvider(client_factory=Client, symbols=symbols, sleep_fn=lambda _: None,
                                   checkpoint_path=checkpoint).fetch("2026-10-21")
    assert result["resumed_count"] == 40 and len(calls) == 20 and len(result["rows"]) == 60
    assert result["partial"] is False


def test_baostock_stalled_call_triggers_relogin_and_retry_before_the_pull_is_abandoned():
    """STUB client: real Baostock 2026-10-21 20:0x answered the first call after a resume with '接收数据异常'/30s hang."""
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    events = []

    class Client:
        def login(self): events.append("login")
        def logout(self): events.append("logout")
        def query(self, code, day, timeout_seconds):
            events.append("q" + code[-6:])
            if events.count("q000001") == 1:
                raise TimeoutError("BAOSTOCK_TIMEOUT")
            return [{"date": day, "code": code, "open": "10", "high": "11", "low": "9", "close": "10",
                     "preclose": "10", "volume": "100", "amount": "1000", "turn": "1", "pctChg": "0"}]

    result = BaostockDailyProvider(client_factory=Client, symbols=["000001", "000002"],
                                   sleep_fn=lambda _: None).fetch("2026-10-21")
    assert len(result["rows"]) == 2 and result["partial"] is False
    assert events[:5] == ["login", "q000001", "logout", "login", "q000001"]
