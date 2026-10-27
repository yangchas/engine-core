import gzip
import json
import time
from pathlib import Path

import pytest


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "real"
CAPTURED_ROW = json.loads((FIXTURES / "c526/daily_kline_baostock_first200.json").read_text(
    encoding="utf-8"))["rows"][0]
DAY = CAPTURED_ROW["date"]
SYMBOL = CAPTURED_ROW["symbol"]


def _real_universe():
    path = FIXTURES / "core_artifacts/c540/symbol_universe/2026-10-23/actual/rows.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_query_timeout_is_bounded_and_preserves_an_empty_checkpoint(tmp_path):
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    class Client:
        def __init__(self):
            self.logout_calls = 0

        def login(self):
            pass

        def query(self, code, day):
            time.sleep(0.3)
            return [CAPTURED_ROW]

        def logout(self):
            self.logout_calls += 1

    client = Client()
    checkpoint = tmp_path / "checkpoint.json"
    provider = BaostockDailyProvider(client_factory=lambda: client, symbols=[SYMBOL],
                                     timeout_seconds=0.02, retries=2, checkpoint_path=checkpoint,
                                     total_timeout_seconds=1, sleep_fn=lambda _seconds: None)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="BAOSTOCK_TIMEOUT") as raised:
        provider.fetch(DAY)

    assert time.monotonic() - started < 0.2
    assert raised.value.baostock_timing["query_count"] == 1
    assert raised.value.baostock_timing["error_events"][-1]["action"] == "call_timeout_abort"
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["rows"] == []
    assert saved["trade_date"] == DAY
    assert client.logout_calls == 0  # a still-blocked call is not reused or logged out in-process


def test_no_progress_stall_fails_before_the_provider_total_timeout(tmp_path):
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    class HungClient:
        def login(self):
            pass

        def query(self, code, day):
            time.sleep(0.4)
            return [CAPTURED_ROW]

        def logout(self):
            pytest.fail("a blocked SDK socket must not be reused for logout")

    checkpoint = tmp_path / "stall-checkpoint.json"
    provider = BaostockDailyProvider(client_factory=HungClient, symbols=[SYMBOL],
                                     timeout_seconds=0.5, retries=0,
                                     total_timeout_seconds=1,
                                     no_progress_timeout_seconds=0.05,
                                     checkpoint_path=checkpoint)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="STALL_NO_PROGRESS") as raised:
        provider.fetch(DAY)

    assert time.monotonic() - started < 0.2
    assert raised.value.error_code == "STALL_NO_PROGRESS"
    assert raised.value.baostock_timing["error_code"] == "STALL_NO_PROGRESS"
    assert raised.value.baostock_timing["error_events"][-1]["action"] == "stall_no_progress"
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["rows"] == []


def test_slow_baostock_queries_with_row_progress_are_not_stalled(tmp_path):
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    real_rows = json.loads((FIXTURES / "c526/daily_kline_baostock_first200.json").read_text(
        encoding="utf-8"))["rows"][:2]
    by_symbol = {row["symbol"]: row for row in real_rows}
    symbols = list(by_symbol)

    class ProgressClient:
        def login(self):
            pass

        def query(self, code, day):
            time.sleep(0.04)
            return [by_symbol[code.rsplit(".", 1)[-1]]]

        def logout(self):
            pass

    result = BaostockDailyProvider(client_factory=ProgressClient, symbols=symbols,
                                   timeout_seconds=0.2, retries=0,
                                   no_progress_timeout_seconds=0.06,
                                   checkpoint_path=tmp_path / "progress-checkpoint.json").fetch(DAY)

    assert [row["symbol"] for row in result["rows"]] == symbols
    assert result["baostock_timing"]["error_code"] == "0"


def test_connection_failure_relogs_in_on_a_fresh_client_and_retries_the_symbol(tmp_path):
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    state = {"clients": 0, "logins": 0, "queries": 0}

    class Client:
        def __init__(self, client_id):
            self.client_id = client_id

        def login(self):
            state["logins"] += 1

        def logout(self):
            pass

        def query(self, code, day):
            state["queries"] += 1
            if self.client_id == 1:
                raise RuntimeError("Bad file descriptor")
            return [CAPTURED_ROW]

    def factory():
        state["clients"] += 1
        return Client(state["clients"])

    provider = BaostockDailyProvider(client_factory=factory, symbols=[SYMBOL], retries=2,
                                     sleep_fn=lambda _seconds: None, checkpoint_path=tmp_path / "cp.json")
    result = provider.fetch(DAY)

    assert [row["symbol"] for row in result["rows"]] == [SYMBOL]
    assert state == {"clients": 2, "logins": 2, "queries": 2}
    assert result["baostock_timing"]["query_count"] == 2
    assert result["baostock_timing"]["error_events"] == [{
        "stage": "query", "error_type": "RuntimeError", "error_code": "BAOSTOCK_CONNECTION_ERROR",
        "message": "Bad file descriptor", "retryable": True,
        "action": "bounded_logout_fresh_login_retry",
    }]


def test_login_failure_retries_finitely_before_querying(tmp_path):
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    state = {"logins": 0, "queries": 0}

    class Client:
        def login(self):
            state["logins"] += 1
            if state["logins"] < 3:
                raise RuntimeError("Baostock login failed")

        def logout(self):
            pass

        def query(self, code, day):
            state["queries"] += 1
            return [CAPTURED_ROW]

    provider = BaostockDailyProvider(client_factory=Client, symbols=[SYMBOL], retries=2,
                                     sleep_fn=lambda _seconds: None, checkpoint_path=tmp_path / "cp.json")
    result = provider.fetch(DAY)
    assert len(result["rows"]) == 1
    assert state == {"logins": 3, "queries": 1}
    assert result["baostock_timing"]["error_events"][0]["action"] == "login_retry"

    exhausted = {"logins": 0}

    class ExhaustingClient:
        def login(self):
            exhausted["logins"] += 1
            if exhausted["logins"] > 3:
                raise AssertionError("login retry limit was exceeded")
            raise RuntimeError("Baostock login failed")

    with pytest.raises(RuntimeError, match="Baostock login failed"):
        BaostockDailyProvider(client_factory=ExhaustingClient, symbols=[SYMBOL], retries=2,
                              sleep_fn=lambda _seconds: None,
                              checkpoint_path=tmp_path / "exhausted.json").fetch(DAY)
    assert exhausted["logins"] == 3


def test_logout_failure_is_recorded_as_cleanup_warning_without_discarding_rows(tmp_path):
    from types import SimpleNamespace

    from engine_core.daily_kline_capture import BaostockClient
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    module = SimpleNamespace(logout=lambda: SimpleNamespace(error_code="1", error_msg="logout failed!"))
    with pytest.raises(RuntimeError, match="logout failed"):
        BaostockClient(module).logout()

    class Client:
        def login(self):
            pass

        def query(self, code, day):
            return [CAPTURED_ROW]

        def logout(self):
            raise RuntimeError("logout failed!")

    result = BaostockDailyProvider(client_factory=Client, symbols=[SYMBOL],
                                   checkpoint_path=tmp_path / "cp.json").fetch(DAY)
    assert len(result["rows"]) == 1
    assert result["baostock_timing"]["result"] == "ok"
    assert result["baostock_timing"]["error_events"] == [{
        "stage": "logout", "error_type": "RuntimeError", "error_code": "RuntimeError",
        "message": "logout failed!", "retryable": False,
        "action": "cleanup_warning_data_retained",
    }]


def test_total_budget_saves_progress_and_resume_skips_every_completed_symbol(tmp_path):
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

    symbols = [SYMBOL, "600001"]
    checkpoint = tmp_path / "checkpoint.json"
    first_calls = []

    class SlowClient:
        def login(self):
            pass

        def logout(self):
            pass

        def query(self, code, day):
            symbol = code.rsplit(".", 1)[-1]
            first_calls.append(symbol)
            if symbol == SYMBOL:
                return [CAPTURED_ROW]
            if symbol == "600001":
                time.sleep(0.3)
            return []

    provider = BaostockDailyProvider(client_factory=SlowClient, symbols=symbols,
                                     timeout_seconds=0.5, total_timeout_seconds=0.12,
                                     retries=2, checkpoint_path=checkpoint, sample_scope=True)
    started = time.monotonic()
    interrupted = provider.fetch(DAY)
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert interrupted["baostock_timing"]["result"] == "budget_exhausted"
    assert interrupted["baostock_timing"]["error_events"][-1]["action"] == "total_budget_exhausted_checkpoint"
    assert interrupted["checkpoint_saved"] is True
    assert interrupted["completed_symbols"] == 1
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert {row["symbol"] for row in saved["rows"]} == {SYMBOL}
    assert saved["symbols_sha256"]
    assert first_calls == symbols

    resumed_calls = []

    class ResumeClient:
        def login(self):
            pass

        def logout(self):
            pass

        def query(self, code, day):
            symbol = code.rsplit(".", 1)[-1]
            resumed_calls.append(symbol)
            return []

    resumed = BaostockDailyProvider(client_factory=ResumeClient, symbols=symbols,
                                    timeout_seconds=0.5, retries=2, checkpoint_path=checkpoint,
                                    sample_scope=True).fetch(DAY)
    assert resumed["resumed_count"] == 1
    assert resumed["baostock_timing"]["query_count"] == len(symbols) - 1
    assert resumed_calls == symbols[1:]
    assert len(resumed["rows"]) == 1
    assert resumed["missing_symbols"] == ["600001"]
    changed_symbols = ["600001", SYMBOL]
    assert BaostockDailyProvider(client_factory=ResumeClient, symbols=changed_symbols,
                                 checkpoint_path=checkpoint, sample_scope=True)._load_checkpoint(
                                     DAY, changed_symbols) == {}
    legacy_checkpoint = tmp_path / "legacy-checkpoint.json"
    legacy_checkpoint.write_text(json.dumps({"trade_date": DAY, "rows": [{"symbol": SYMBOL}]}),
                                 encoding="utf-8")
    assert BaostockDailyProvider(client_factory=ResumeClient, symbols=symbols,
                                 checkpoint_path=legacy_checkpoint, sample_scope=True)._load_checkpoint(
                                     DAY, symbols) == {}


def test_run_data_jobs_returns_30_and_keeps_checkpoint_on_total_budget(tmp_path, monkeypatch, capsys):
    import hashlib
    import sys
    from types import ModuleType, SimpleNamespace

    from engine_core.data_pipeline.storage import ArtifactCatalog
    from examples import run_data_jobs

    monkeypatch.chdir(tmp_path)
    root = tmp_path / "artifacts"
    catalog = ArtifactCatalog(root)
    catalog.commit("symbol_universe", DAY, _real_universe(), {"as_of": "2026-10-22"},
                   source="baostock_query_all_stock")
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text(f"{SYMBOL}\n", encoding="utf-8")

    fake_baostock = ModuleType("baostock")
    fake_baostock.login = lambda: SimpleNamespace(error_code="0", error_msg="")
    fake_baostock.logout = lambda: SimpleNamespace(error_code="0", error_msg="")

    def slow_query(*_args, **_kwargs):
        time.sleep(0.25)
        raise AssertionError("caller deadline should return before this worker")

    fake_baostock.query_history_k_data_plus = slow_query
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock)
    started = time.monotonic()
    exit_code = run_data_jobs.main([
        "--job", "daily_kline", "--date", DAY, "--execute", "--providers", "baostock",
        "--symbols-file", str(sample_file), "--artifact-root", str(root),
        "--baostock-timeout-seconds", "0.5", "--baostock-total-timeout-seconds", "0.06",
        "--budget-seconds", "0.06", "--baostock-budget-fraction", "1",
    ])
    elapsed = time.monotonic() - started
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    contract = json.loads(lines[-1])
    checkpoint = (root / ".checkpoints" /
                  f"daily_kline-{DAY}-sample-{hashlib.sha256(sample_file.read_bytes()).hexdigest()[:12]}.json")

    assert exit_code == 30
    assert elapsed < 0.25
    assert contract["retryable"] is True
    assert contract["error_code"] == "DAILY_KLINE_ALL_PROVIDERS_FAILED"
    assert contract["detail"]["providers"] == ["baostock"]
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["rows"] == []


def test_run_data_jobs_reports_stall_as_retryable_exit_30(tmp_path, monkeypatch, capsys):
    import sys
    from types import ModuleType, SimpleNamespace

    from engine_core.data_pipeline.storage import ArtifactCatalog
    from examples import run_data_jobs

    monkeypatch.chdir(tmp_path)
    root = tmp_path / "artifacts"
    real_universe = _real_universe()
    symbol_row = next(row for row in real_universe
                      if str(row.get("symbol", row.get("code", ""))).rsplit(".", 1)[-1].zfill(6) == SYMBOL)
    ArtifactCatalog(root).commit("symbol_universe", DAY, [symbol_row], {"as_of": DAY},
                                 source="baostock_query_all_stock")
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text(f"{SYMBOL}\n", encoding="utf-8")
    fake_baostock = ModuleType("baostock")
    fake_baostock.login = lambda: SimpleNamespace(error_code="0", error_msg="")
    fake_baostock.logout = lambda: SimpleNamespace(error_code="0", error_msg="")

    def hung_query(*_args, **_kwargs):
        time.sleep(0.3)
        raise AssertionError("no-progress guard should return before this fake response")

    fake_baostock.query_history_k_data_plus = hung_query
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock)
    started = time.monotonic()
    exit_code = run_data_jobs.main([
        "--job", "daily_kline", "--date", DAY, "--execute", "--providers", "baostock",
        "--symbols-file", str(sample_file), "--artifact-root", str(root),
        "--baostock-timeout-seconds", "0.5", "--baostock-total-timeout-seconds", "1",
        "--baostock-no-progress-seconds", "0.04", "--budget-seconds", "2",
        "--baostock-budget-fraction", "1",
    ])
    elapsed = time.monotonic() - started
    contract = json.loads([line for line in capsys.readouterr().out.splitlines() if line.strip()][-1])

    assert exit_code == 30
    assert elapsed < 0.2
    assert contract["error_code"] == "STALL_NO_PROGRESS"
    assert contract["retryable"] is True
    assert contract["detail"]["provider_attempts"][0]["error_code"] == "STALL_NO_PROGRESS"


def test_core_data_daily_symbols_file_runs_probe_without_derived_jobs(tmp_path, monkeypatch, capsys):
    import json

    from examples import core_data_daily, run_data_jobs

    monkeypatch.chdir(tmp_path)
    root = tmp_path / "artifacts"
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text(f"{SYMBOL}\n", encoding="utf-8")
    payload = {
        "rows": [CAPTURED_ROW],
        "expected_count": 1, "sample_scope": "symbols_file", "sample_symbols": [SYMBOL],
        "source": "baostock", "tier": 0,
        "baostock_timing": {"result": "ok", "query_count": 1, "error_code": "0"},
    }
    monkeypatch.setattr(run_data_jobs, "_default_adapters", lambda *_args, **_kwargs: {
        "daily_kline": lambda _day: payload})
    monkeypatch.setattr(core_data_daily, "run_daily_phase",
                        lambda *_args, **_kwargs: pytest.fail("sample probe must not run derived jobs"))

    exit_code = core_data_daily.main([
        "--phase", "postmarket", "--date", DAY, "--symbols-file", str(sample_file),
        "--artifact-root", str(root),
    ])
    contract = json.loads([line for line in capsys.readouterr().out.splitlines() if line.strip()][-1])

    assert exit_code == 10
    assert contract["status"] == "degraded"
    assert contract["source"] == "baostock"
    assert contract["row_count"] == 1
    assert contract["detail"]["sample_scope"] == "symbols_file"
    assert contract["detail"]["business_status"] == "PROVISIONAL"
    with pytest.raises(SystemExit) as run_jobs_missing_root:
        run_data_jobs.main(["--job", "daily_kline", "--date", DAY,
                            "--symbols-file", str(sample_file)])
    assert run_jobs_missing_root.value.code == 2
    with pytest.raises(SystemExit) as daily_missing_root:
        core_data_daily.main(["--phase", "postmarket", "--date", DAY,
                              "--symbols-file", str(sample_file)])
    assert daily_missing_root.value.code == 2


def test_sample_symbols_file_is_validated_and_manifest_stays_provisional(tmp_path, monkeypatch):
    from engine_core.data_pipeline.providers.daily_kline_chain import DailyKlineProviderChain
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from examples.run_data_jobs import _symbols_file

    sample_path = tmp_path / "sample.txt"
    sample_path.write_text(f"{SYMBOL}\n", encoding="utf-8")
    universe = _real_universe()
    assert _symbols_file(sample_path, universe) == [SYMBOL]
    catalog = ArtifactCatalog(tmp_path / "artifacts")
    catalog.commit("symbol_universe", DAY, universe, {"as_of": "2026-10-23"},
                   source="baostock_query_all_stock")

    rows = [CAPTURED_ROW]
    chain = DailyKlineProviderChain(providers={"baostock": lambda _day: {
        "rows": rows, "expected_count": 1, "sample_scope": "symbols_file",
        "sample_symbols": [SYMBOL], "source": "baostock", "tier": 0,
        "baostock_timing": {"result": "ok", "query_count": 3, "error_code": "0"},
    }}, artifact_root=tmp_path / "artifacts")
    result = chain.fetch_chain(DAY, now="2026-10-26T18:00:00+08:00", persist=True)

    assert result is not None
    manifest = result.manifest
    assert manifest["source"] == "baostock"
    assert manifest["tier"] == 0
    assert manifest["status"] == "SAMPLE_ONLY"
    assert manifest["business_status"] == "PROVISIONAL"
    assert manifest["sample_scope"] == "symbols_file"
    assert manifest["quality"]["expected_count"] == 1
    assert manifest["quality"]["sample_symbol_count"] == 1
    assert manifest["quality"]["coverage"] == 1.0
    assert manifest["quality"]["status"] == "PARTIAL"
    assert manifest["row_count"] == 1
    assert ArtifactCatalog(tmp_path / "artifacts").read()["datasets"]["daily_kline"].get("latest") is None
    from engine_core.data_pipeline.access import get as read_dataset
    read_sample = read_dataset("daily_kline", trade_date=DAY, root=tmp_path / "artifacts")
    assert read_sample.meta["business_status"] == "PROVISIONAL"
    assert read_sample.meta["status"] == "DEGRADED"

    production_root = tmp_path / "production-core-data"
    monkeypatch.setenv("CORE_DATA_ROOT", str(production_root))
    from examples.run_data_jobs import _require_sample_artifact_root
    assert _require_sample_artifact_root(tmp_path / "outside-core-data") == str(
        (tmp_path / "outside-core-data").resolve())
    with pytest.raises(ValueError, match="cannot_be_core_data_root"):
        _require_sample_artifact_root(production_root)
    with pytest.raises(ValueError, match="cannot_be_core_data_root"):
        _require_sample_artifact_root(production_root / "sample")
    with pytest.raises(ValueError, match="cannot_be_core_data_root"):
        ArtifactCatalog(production_root).commit("daily_kline", DAY, rows, {
            "sample_scope": "symbols_file", "sample_symbols": [SYMBOL],
        }, source="baostock")
