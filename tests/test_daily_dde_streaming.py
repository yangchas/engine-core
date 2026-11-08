# DATA: real:kaipan_stock_dde_2026-10-29_600519_1539.json, kaipan_stock_dde_2026-10-29_688001_1540.json, kaipan_stock_dde_2026-11-02_000831_0731.json, td/dde_suspended.json.gz, td/dde_not_flagged_1103.json.gz
import json
import threading
from pathlib import Path

import pytest

from engine_core.data_pipeline.daily_dde import DdeSyncRetryableError, TaosDdeStore, run_daily_dde_sync


from tests.replay import ReplayTd

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


def test_dde_writes_each_symbol_before_starting_next_fetch():
    target_date = "2026-10-29"
    raw_by_symbol = {
        "600519": json.loads((REAL / "kaipan_stock_dde_2026-10-29_600519_1539.json").read_text()),
        "688001": json.loads((REAL / "kaipan_stock_dde_2026-10-29_688001_1540.json").read_text()),
    }
    store = RecordingDdeStore()
    writes_at_fetch = []

    def fetch(symbol, target):
        assert target == target_date
        writes_at_fetch.append(list(store.writes))
        return raw_by_symbol[symbol]

    result = run_daily_dde_sync(
        ["600519", "688001"], target_date, store=store, fetcher=fetch,
        max_workers=2, attempts=1, write_redis=False, skip_redis=True,
        sleep=lambda _seconds: None)
    assert writes_at_fetch == [[], ["600519"]]
    assert result["status"] == "ok"
    assert store.writes == ["600519", "688001"]


class ReplayedSuspensionStore(RecordingDdeStore):
    """Recording store whose suspension lookup is the real TaosDdeStore.read_suspended over a recorded TD answer."""

    def __init__(self, recording):
        super().__init__()
        self._real = TaosDdeStore("core_data", connect=lambda: ReplayTd(recording))

    def read_suspended(self, symbols, target_date):
        return self._real.read_suspended(symbols, target_date)


def test_dde_suspended_stock_without_same_day_dde_is_known_missing_not_failure():
    store = ReplayedSuspensionStore("dde_suspended")
    raw = json.loads((REAL / "kaipan_stock_dde_2026-11-02_000831_0731.json").read_text())
    result = run_daily_dde_sync(
        ["000831"], "2026-11-02", database=store.database, root=ROOT, max_workers=1, attempts=2,
        history_trading_days=250, write_redis=False, skip_redis=True, store=store,
        fetcher=lambda s, d: raw, sleep=lambda _s: None)
    assert result["status"] == "ok"
    assert result["known_missing"] == [{"symbol": "000831", "reason": "suspended_no_same_day_dde"}]
    assert store.writes == []


def test_dde_stale_response_for_a_stock_that_is_not_suspended_still_fails():
    # Recorded TD answer for 2026-11-03: 000831 has no daily_kline row that day, so nothing marks it suspended.
    store = ReplayedSuspensionStore("dde_not_flagged_1103")
    raw = json.loads((REAL / "kaipan_stock_dde_2026-11-02_000831_0731.json").read_text())
    with pytest.raises(DdeSyncRetryableError) as caught:
        run_daily_dde_sync(
            ["000831"], "2026-11-03", database=store.database, root=ROOT, max_workers=1, attempts=2,
            history_trading_days=250, write_redis=False, skip_redis=True, store=store,
            fetcher=lambda s, d: raw, sleep=lambda _s: None)
    assert [item["symbol"] for item in caught.value.summary["failed"]] == ["000831"]
