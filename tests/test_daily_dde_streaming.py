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
