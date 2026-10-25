from datetime import datetime

import pytest


def test_baostock_login_hang_on_local_socket_is_retryable_timeout():
    import socket
    import threading
    import time
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted = threading.Event()
    release = threading.Event()

    def server():
        connection, _ = listener.accept()
        accepted.set()
        release.wait(2)
        connection.close()

    server_thread = threading.Thread(target=server, daemon=True)
    server_thread.start()

    class Client:
        def login(self):
            sock = socket.create_connection(listener.getsockname())
            try:
                sock.recv(1)
            finally:
                sock.close()

        def logout(self):
            pass

    provider = BaostockDailyProvider(client_factory=Client, symbols=["600000"], timeout_seconds=0.1,
                                     sleep_fn=lambda _: None)
    assert provider.timeout_seconds == 0.1
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="BAOSTOCK_TIMEOUT"):
            provider.fetch("2026-10-19")
        assert accepted.wait(0.2)
        # login gets 5x the per-query bound and 3 tries (C395): bounded, but no longer sub-second
        assert time.monotonic() - started < 5
    finally:
        release.set()
        listener.close()
        server_thread.join(0.5)


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


def _c541_real_daily_rows():
    import gzip
    import json
    from pathlib import Path

    path = (Path(__file__).parent / "fixtures" / "real" / "core_artifacts" / "c522" /
            "daily_kline" / "2026-10-23" / "formal" / "rows.jsonl.gz")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _c541_as_baostock_response(row):
    """Adapt a previously captured real Baostock bar to the provider's raw field names."""
    return {
        "date": row["date"], "code": row["symbol"], "open": row["open"],
        "high": row["high"], "low": row["low"], "close": row["close"],
        "preclose": row["pre_close"], "volume": row["volume"],
        "amount": row["amount_yuan"], "turn": row.get("turnover_pct"),
        "pctChg": row["pct_chg"],
    }


class _C541Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_budget_truncates_baostock_saves_checkpoint_and_replays_q2_then_resumes(monkeypatch, tmp_path):
    import time
    from conftest import seed_real_symbol_universe
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider
    from engine_core.data_pipeline.providers.daily_kline_chain import (
        DailyKlineProviderChain, Q2FinalProvider,
    )
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from replay_quote_slice import load_v0, replay_clients

    captured = _c541_real_daily_rows()
    fields = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")
    selected = [row for row in captured if all(row.get(field) is not None for field in fields)][:10]
    rows_by_symbol = {row["symbol"]: row for row in selected}
    symbols = list(rows_by_symbol)
    seed_real_symbol_universe(ArtifactCatalog(tmp_path), "2026-10-23", symbols=symbols)
    replay_redis = replay_clients(load_v0())[0]
    clock = _C541Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    checkpoint = tmp_path / ".checkpoints" / "daily_kline-2026-10-23.json"
    calls = []
    holder = {}

    class Client:
        def login(self):
            return None

        def logout(self):
            return None

        def query(self, code, day, timeout_seconds):
            symbol = code.rsplit(".", 1)[-1]
            return [_c541_as_baostock_response(rows_by_symbol[symbol])]

    def timeout_call(fn, *args):
        if fn.__name__ == "query":
            provider = holder["provider"]
            remaining = provider.deadline - clock.monotonic()
            if remaining <= 0:
                raise TimeoutError("BAOSTOCK_TIMEOUT")
            calls.append(args[0].rsplit(".", 1)[-1])
            elapsed = min(1.0, remaining)
            clock.advance(elapsed)
            if elapsed < 1.0:
                raise TimeoutError("BAOSTOCK_TIMEOUT")
        return fn(*args)

    def make_chain():
        provider = BaostockDailyProvider(
            client_factory=Client, symbols=symbols, timeout_seconds=30, retries=2,
            sleep_fn=lambda seconds: clock.advance(seconds), timeout_call=timeout_call,
            checkpoint_path=checkpoint,
        )
        holder["provider"] = provider
        return provider, DailyKlineProviderChain(
            providers={"baostock": provider, "q2_final": Q2FinalProvider(replay_redis)},
            artifact_root=tmp_path,
        )

    first_provider, first_chain = make_chain()
    first = first_chain.fetch_chain(
        "2026-10-23", now="2026-10-23T18:00:00+08:00", persist=True,
        budget_seconds=10, baostock_budget_fraction=0.6,
    )
    first_attempt = first.manifest["provider_attempts"][0]
    import json
    saved_rows = json.loads(checkpoint.read_text(encoding="utf-8"))["rows"]
    assert len(calls) == 6
    assert first_provider.total_timeout_seconds is None
    assert first.manifest["provider"] == "q2_final"
    assert first.manifest["business_status"] == "PROVISIONAL"
    assert first_attempt["result"] == "budget_exhausted"
    assert first_attempt["completed_symbols"] == 6
    assert first_attempt["expected_symbols"] == 10
    assert first_attempt["checkpoint_saved"] is True
    assert first_attempt["baostock_budget_seconds"] == 6.0
    assert len(saved_rows) == 6
    assert first.manifest["provider_attempts"][1]["provider"] == "q2_final"
    assert clock.monotonic() <= 6.0

    calls.clear()
    second_provider, second_chain = make_chain()
    second = second_chain.fetch_chain(
        "2026-10-23", now="2026-10-23T18:00:00+08:00", persist=True,
        budget_seconds=10, baostock_budget_fraction=0.6,
    )
    assert second.manifest["provider"] == "baostock"
    assert second.manifest["business_status"] == "FORMAL"
    assert second.manifest["quality"]["coverage"] == 1.0
    assert second_provider.total_timeout_seconds is None
    assert len(calls) == 4
    assert second.manifest["provider_attempts"][0]["resumed_count"] == 6
    assert second.manifest["provider_attempts"][0]["completed_symbols"] == 10


def test_q2_final_replay_uses_real_terminal_hashes_and_active_universe_denominator(tmp_path):
    from conftest import seed_real_symbol_universe
    from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain, Q2FinalProvider
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from replay_quote_slice import load_v0, replay_clients

    replay_redis = replay_clients(load_v0())[0]
    catalog = ArtifactCatalog(tmp_path)
    universe = seed_real_symbol_universe(catalog, "2026-10-23")
    active_count = sum(row.get("trade_status") != "0" for row in universe)
    result = DailyKlineProviderChain(
        providers={"q2_final": Q2FinalProvider(replay_redis)}, artifact_root=tmp_path,
    ).fetch_chain("2026-10-23", now="2026-10-23T18:00:00+08:00", persist=True)

    assert result.manifest["provider"] == "q2_final"
    assert result.manifest["business_status"] == "PROVISIONAL"
    assert result.manifest["quality"]["expected_basis"] == "universe_active"
    assert result.manifest["quality"]["expected_count"] == active_count
    assert result.manifest["quality"]["coverage"] == 1.0
    assert result.manifest["provider_attempts"][-1]["provider"] == "q2_final"
    assert result.manifest["provider_attempts"][-1]["chain_result"] == "ACCEPTED"
    assert len(result.rows) == 5221


def test_baostock_fast_path_finishes_formal_with_real_captured_bars(monkeypatch, tmp_path):
    import time
    from conftest import seed_real_symbol_universe
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider
    from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain
    from engine_core.data_pipeline.storage import ArtifactCatalog

    captured = _c541_real_daily_rows()
    fields = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")
    selected = [row for row in captured if all(row.get(field) is not None for field in fields)][:1]
    rows_by_symbol = {row["symbol"]: row for row in selected}
    symbols = list(rows_by_symbol)
    seed_real_symbol_universe(ArtifactCatalog(tmp_path), "2026-10-23", symbols=symbols)
    clock = _C541Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    calls = []
    holder = {}

    class Client:
        def login(self): pass
        def logout(self): pass
        def query(self, code, day, timeout_seconds):
            calls.append(code)
            return [_c541_as_baostock_response(rows_by_symbol[code.rsplit(".", 1)[-1]])]

    def timeout_call(fn, *args):
        if fn.__name__ == "query":
            clock.advance(4.99)
            return fn(*args)
        if fn.__name__ == "logout":
            clock.advance(holder["provider"].deadline - clock.monotonic())
            raise TimeoutError("BAOSTOCK_TIMEOUT")
        return fn(*args)

    provider = BaostockDailyProvider(
        client_factory=Client, symbols=symbols, timeout_seconds=30,
        timeout_call=timeout_call,
        total_timeout_seconds=5,
    )
    holder["provider"] = provider
    result = DailyKlineProviderChain(
        providers={"baostock": provider}, artifact_root=tmp_path,
    ).fetch_chain("2026-10-23", now="2026-10-23T18:00:00+08:00", persist=True,
                  budget_seconds=10, baostock_budget_fraction=0.6)

    assert len(calls) == 1
    assert result.manifest["provider"] == "baostock"
    assert result.manifest["business_status"] == "FORMAL"
    assert result.manifest["provider_attempts"][0]["result"] == "ok"


def test_explicit_baostock_timeout_is_capped_by_chain_fraction(monkeypatch, tmp_path):
    from conftest import seed_real_symbol_universe
    from examples import run_data_jobs
    from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain
    from engine_core.data_pipeline.storage import ArtifactCatalog

    rows = _c541_real_daily_rows()[:1]
    symbols = [rows[0]["symbol"]]
    seen = []
    seed_real_symbol_universe(ArtifactCatalog(tmp_path), "2026-10-23", symbols=symbols)

    class Provider:
        def __init__(self, **kwargs):
            self.total_timeout_seconds = kwargs["total_timeout_seconds"]

        def fetch(self, day, context=None):
            seen.append(self.total_timeout_seconds)
            return {"rows": rows, "expected_count": 1,
                    "baostock_timing": {"result": "ok", "completed_symbols": 1,
                                         "expected_symbols": 1}}

    import engine_core.data_pipeline.providers.baostock_daily_provider as baostock_module
    monkeypatch.setattr(baostock_module, "BaostockDailyProvider", Provider)
    adapters = run_data_jobs._default_adapters(
        str(tmp_path), "", 15, True, baostock_total_timeout_seconds=3.0,
    )
    result = DailyKlineProviderChain(
        providers={"baostock": adapters["daily_kline"]}, artifact_root=tmp_path,
    ).fetch_chain("2026-10-23", now="2026-10-23T18:00:00+08:00",
                  budget_seconds=10, baostock_budget_fraction=0.6)
    assert seen == [3.0]
    assert result.manifest["provider"] == "baostock"


def test_baostock_short_operation_timeout_and_retries_stay_inside_fraction(monkeypatch, tmp_path):
    import time
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider
    from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain

    clock = _C541Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    calls = []
    sleeps = []
    holder = {}

    class Client:
        def login(self): pass
        def logout(self): pass
        def query(self, code, day, timeout_seconds):
            return []

    def timeout_call(fn, *args):
        if fn.__name__ != "query":
            return fn(*args)
        provider = holder["provider"]
        remaining = provider.deadline - clock.monotonic()
        if remaining <= 0:
            raise TimeoutError("BAOSTOCK_TIMEOUT")
        calls.append(args[0])
        clock.advance(min(1.0, remaining))
        raise TimeoutError("BAOSTOCK_TIMEOUT")

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    provider = BaostockDailyProvider(
        client_factory=Client, symbols=["000001"], timeout_seconds=1, retries=2,
        sleep_fn=fake_sleep, timeout_call=timeout_call,
    )
    holder["provider"] = provider
    result = DailyKlineProviderChain(
        providers={"baostock": provider}, artifact_root=tmp_path,
    ).fetch_chain("2026-10-23", now="2026-10-23T18:00:00+08:00",
                  budget_seconds=10, baostock_budget_fraction=0.6)

    assert result is None
    assert calls == ["sz.000001", "sz.000001"]
    assert clock.monotonic() <= 6.0
    assert sum(sleeps) <= 5.0


def test_daily_kline_cli_returns_contract_30_when_all_sources_fail(monkeypatch, capsys, tmp_path):
    import json
    from examples import run_data_jobs
    from engine_core.data_pipeline.storage import ArtifactCatalog

    def fail(_day):
        raise RuntimeError("baostock unavailable")

    monkeypatch.setattr(run_data_jobs, "_default_adapters", lambda *args, **kwargs: {"daily_kline": fail})
    code = run_data_jobs.main([
        "--job", "daily_kline", "--date", "2026-10-23", "--execute",
        "--artifact-root", str(tmp_path), "--providers", "baostock,q2_final",
        "--budget-seconds", "10",
    ])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert code == 30
    assert json.loads(lines[-1])["status"] == "failed"
    assert ArtifactCatalog(tmp_path).locate("daily_kline", "2026-10-23") is None
