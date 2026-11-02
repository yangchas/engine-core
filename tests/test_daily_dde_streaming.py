import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engine_core.data_pipeline.daily_dde import run_daily_dde_sync


ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests" / "fixtures" / "real"


class RecordingDdeStore:
    database = "core_dev_c615_test"

    def __init__(self):
        self.writes = []
        self.watermarks = {}
        self.first_write = threading.Event()

    def ensure_schema(self):
        return "recording-store"

    def latest_watermarks(self):
        return dict(self.watermarks)

    def read_target_rows(self, symbols, target_date):
        return {symbol: {"symbol": symbol, "date": target_date, "ddje": 0.0}
                for symbol in symbols if self.watermarks.get(symbol) == target_date}

    def write_and_verify(self, symbol, rows, version):
        self.writes.append(symbol)
        self.watermarks[symbol] = max(row["date"] for row in rows)
        self.first_write.set()
        return len(rows)

    def compare_legacy(self, symbols, rows):
        return {"pairs": 0, "equal": 0, "different": 0, "unmatched": 0,
                "columns": {}, "reasons": {}}


def test_dde_writes_first_completed_symbol_before_other_fetch_finishes():
    target_date = "2026-10-29"
    raw_by_symbol = {
        "600519": json.loads((REAL / "kaipan_stock_dde_2026-10-29_600519_1539.json").read_text()),
        "688001": json.loads((REAL / "kaipan_stock_dde_2026-10-29_688001_1540.json").read_text()),
    }
    second_fetch_started = threading.Event()
    release_second_fetch = threading.Event()
    store = RecordingDdeStore()

    def fetch(symbol, target):
        assert target == target_date
        if symbol == "600519":
            second_fetch_started.set()
            assert release_second_fetch.wait(10), "test did not release delayed real response"
        return raw_by_symbol[symbol]

    def run():
        return run_daily_dde_sync(
            ["600519", "688001"], target_date, store=store, fetcher=fetch,
            max_workers=2, attempts=1, write_redis=False, skip_redis=True,
            sleep=lambda _seconds: None)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run)
        assert second_fetch_started.wait(5), "delayed response did not start"
        wrote_before_release = store.first_write.wait(3)
        release_second_fetch.set()
        result = future.result(timeout=15)

    assert wrote_before_release, "DDE waited for every source response before starting TD writes"
    assert result["status"] == "ok"
    assert set(store.writes) == {"600519", "688001"}


class SuspendedAwareStore(RecordingDdeStore):
    """Same recording store; ``suspended`` is the set TD returned for 2026-11-02 (evidence/C643)."""

    def __init__(self, suspended):
        super().__init__()
        self.suspended = set(suspended)

    def read_suspended(self, symbols, target_date):
        return self.suspended & set(symbols)


def test_dde_suspended_stock_without_same_day_dde_is_known_missing_not_failure():
    store = SuspendedAwareStore({"000831"})
    raw = json.loads((REAL / "kaipan_stock_dde_2026-11-02_000831_0731.json").read_text())
    result = run_daily_dde_sync(
        ["000831"], "2026-11-02", database=store.database, root=ROOT, max_workers=1, attempts=2,
        history_trading_days=250, write_redis=False, skip_redis=True, store=store,
        fetcher=lambda s, d: raw, sleep=lambda _s: None)
    assert result["status"] == "ok"
    assert result["known_missing"] == [{"symbol": "000831", "reason": "suspended_no_same_day_dde"}]
    assert store.writes == []


def test_dde_stale_response_for_a_stock_that_is_not_suspended_still_fails():
    import pytest
    from engine_core.data_pipeline.daily_dde import DdeSyncRetryableError
    store = SuspendedAwareStore(set())
    raw = json.loads((REAL / "kaipan_stock_dde_2026-11-02_000831_0731.json").read_text())
    with pytest.raises(DdeSyncRetryableError) as caught:
        run_daily_dde_sync(
            ["000831"], "2026-11-02", database=store.database, root=ROOT, max_workers=1, attempts=2,
            history_trading_days=250, write_redis=False, skip_redis=True, store=store,
            fetcher=lambda s, d: raw, sleep=lambda _s: None)
    assert [item["symbol"] for item in caught.value.summary["failed"]] == ["000831"]
