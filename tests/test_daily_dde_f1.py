import json
from pathlib import Path

from engine_core.data_pipeline.daily_dde import run_daily_dde_sync
from tests.test_daily_dde_streaming import RecordingDdeStore


def test_requests_are_serial_and_spaced():
    fixtures = Path(__file__).parent / 'fixtures/dde_c841'
    raw = {symbol: json.loads((fixtures / (symbol + '.json')).read_text())
           for symbol in ('000001', '000002')}
    now = [0.0]
    calls = []
    store = RecordingDdeStore()

    def fetch(symbol, target):
        calls.append((symbol, now[0], list(store.writes)))
        return raw[symbol]

    def sleep(seconds):
        now[0] += seconds

    run_daily_dde_sync(['000001', '000002'], '2026-11-06', store=store, fetcher=fetch,
                       max_workers=2, attempts=1, skip_redis=True, sleep=sleep,
                       clock=lambda: now[0])
    assert calls[1][1] - calls[0][1] >= 1.0
    assert calls[1][2] == ['000001']
