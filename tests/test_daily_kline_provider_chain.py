from datetime import datetime

import pytest


def test_daily_kline_chain_gates_by_available_at_and_uses_quality_priority(tmp_path):
    from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain

    calls = []
    chain = DailyKlineProviderChain(
        providers={
            "baostock": lambda day: calls.append("baostock") or {"rows": [{"symbol": "600000", "close": 10}], "expected_count": 1},
            "wencai": lambda day: calls.append("wencai") or {"rows": [{"symbol": "600000", "close": 9}], "expected_count": 1},
            "q2_final": lambda day: calls.append("q2_final") or {"rows": [{"symbol": "600000", "close": 8}], "expected_count": 1},
        },
        artifact_root=tmp_path,
    )

    early = chain.fetch_chain("2026-10-16", now="2026-10-16T15:04:00+08:00")
    assert early is None
    assert calls == []
    result = chain.fetch_chain("2026-10-16", now="2026-10-16T15:05:00+08:00")
    assert result.manifest["source"] == "wencai"
    assert result.manifest["degraded"] is True
    assert calls == ["wencai"]


def test_q2_provider_is_read_only_converts_units_and_rejects_stale_rows():
    from engine_core.data_pipeline.providers.daily_kline_chain import Q2FinalProvider

    class Pipeline:
        def __init__(self, redis):
            self.redis = redis
            self.keys = []

        def hgetall(self, key):
            self.keys.append(key)
            return self.redis.rows[key]

        def execute(self):
            return [self.redis.rows[key] for key in self.keys]

    class Redis:
        def __init__(self):
            self.rows = {"q2:600000": {"px": "11540", "pc": "11000", "amt": "1234", "vol": "100", "mx": "11600", "mn": "10900", "ts": "1792159500000"}}
            self.writes = []

        def smembers(self, key):
            assert key == "q2:active:2026-10-16"
            return {"600000"}

        def pipeline(self, transaction=False):
            return Pipeline(self)

        def __getattr__(self, name):
            if name.startswith(("set", "hset", "delete", "expire", "sadd")):
                self.writes.append(name)
                raise AssertionError("Redis writes are forbidden")
            raise AttributeError(name)

    redis = Redis()
    result = Q2FinalProvider(redis).fetch("2026-10-16", now="2026-10-16T15:05:00+08:00")
    row = result["rows"][0]
    assert row["close"] == 11.54
    assert row["pre_close"] == 11.0
    assert row["pct_chg"] == pytest.approx((11.54 / 11.0 - 1) * 100)
    assert row["open"] is None and row["open_missing"] is True
    assert result["field_units"]["volume"] == "UNKNOWN"
    assert redis.writes == []


def test_daily_kline_chain_requires_coverage_and_supersedes_degraded_version(tmp_path):
    from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain

    low = lambda day: {"rows": [{"symbol": "600000", "close": 9}], "expected_count": 2}
    chain = DailyKlineProviderChain(providers={"wencai": low, "q2_final": low}, artifact_root=tmp_path,
                                    coverage_threshold=0.98)
    result = chain.fetch_chain("2026-10-16", now="2026-10-16T16:00:00+08:00", persist=True)
    assert result is None
    unknown_coverage = DailyKlineProviderChain(
        providers={"wencai": lambda day: {"rows": [{"symbol": "600000", "close": 9}]}},
        artifact_root=tmp_path / "unknown",
    )
    assert unknown_coverage.fetch_chain("2026-10-16", now="2026-10-16T16:00:00+08:00") is None

    chain.providers["wencai"] = lambda day: {"rows": [{"symbol": "600000", "close": 9}], "expected_count": 1}
    degraded = chain.fetch_chain("2026-10-16", now="2026-10-16T16:00:00+08:00", persist=True)
    chain.providers["baostock"] = lambda day: {"rows": [{"symbol": "600000", "close": 10}], "expected_count": 1}
    formal = chain.fetch_chain("2026-10-16", now="2026-10-16T17:30:00+08:00", persist=True)

    assert degraded.manifest["degraded"] is True
    assert formal.manifest["source"] == "baostock"
    assert formal.manifest["degraded"] is False
    assert formal.manifest["reconciliation"]["close"]["different_count"] == 1
    assert degraded.manifest["superseded_by"] == formal.manifest["version"]
    from engine_core.data_pipeline.reader import load_dataset
    from engine_core.data_pipeline.storage import ArtifactCatalog
    loaded = load_dataset("daily_kline", catalog=ArtifactCatalog(tmp_path))
    assert loaded.manifest["source"] == "baostock"
    assert loaded.manifest["degraded"] is False


def test_wencai_daily_provider_opens_circuit_on_first_403(tmp_path):
    from engine_core.data_pipeline.providers.daily_kline_chain import WencaiDailyProvider

    calls = []

    def fetch(day, context):
        calls.append(day)
        raise RuntimeError("403 Forbidden")

    provider = WencaiDailyProvider(fetch, state_path=tmp_path / "state.json", sleep_fn=lambda _: None)
    normalized = provider._normalize_rows([{
        "股票代码": "600000", "日期": "2026-10-16", "开盘价": "10.00",
        "最高价": "11.00", "最低价": "9.90", "收盘价": "10.50",
        "昨收": "10.00", "涨跌幅(前复权)[20261016]": "5.00%",
        "成交量": "1000", "成交额(元)": "123456.7",
    }], "2026-10-16")
    assert normalized[0]["pct_chg"] == 5.0
    assert normalized[0]["amount_yuan"] == 123456.7
    assert provider.fetch("2026-10-16", now="2026-10-16T16:00:00+08:00")["status"] == "PROVIDER_UNAVAILABLE"
    assert provider.fetch("2026-10-16", now="2026-10-16T16:00:01+08:00")["status"] == "PROVIDER_UNAVAILABLE"
    assert calls == ["2026-10-16"]


def test_baostock_availability_gate_and_q2_stale_timestamp(tmp_path):
    from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain, Q2FinalProvider

    chain = DailyKlineProviderChain(
        providers={"baostock": lambda day: {"rows": [{"symbol": "600000", "close": 10}], "expected_count": 1}},
        artifact_root=tmp_path,
    )
    assert chain.fetch_chain("2026-10-16", now="2026-10-16T17:29:00+08:00") is None
    assert chain.fetch_chain("2026-10-16", now="2026-10-16T17:30:00+08:00").manifest["source"] == "baostock"

    class Pipe:
        def __init__(self, redis): self.redis, self.keys = redis, []
        def hgetall(self, key): self.keys.append(key)
        def execute(self): return [self.redis.rows[key] for key in self.keys]
    class Redis:
        rows = {"q2:600000": {"px": "11540", "pc": "11000", "ts": "1792133940000"}}
        def smembers(self, key): return {"600000"}
        def pipeline(self, transaction=False): return Pipe(self)
    result = Q2FinalProvider(Redis()).fetch("2026-10-16", now="2026-10-16T15:05:00+08:00")
    assert result["rows"] == []
    assert result["stale_symbols"] == ["600000"]
