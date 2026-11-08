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


def test_missing_suspension_table_degrades_and_guardian_maps_exit(monkeypatch):
    from engine_core.data_pipeline.daily_dde import DdeError
    from engine_core.guardian import daily_dde_sync

    raw = json.loads((Path(__file__).parent / 'fixtures/real/kaipan_stock_dde_2026-11-02_000831_0731.json').read_text())
    class MissingTable(RecordingDdeStore):
        def read_suspended(self, symbols, target_date):
            raise DdeError('td_statement_failed:table missing')

    result = run_daily_dde_sync(['000831'], '2026-11-03', store=MissingTable(),
                                fetcher=lambda *_: raw, attempts=1, skip_redis=True)
    assert result['status'] == 'degraded'
    assert result['unknown_missing_total'] == 1
    assert result['unknown_missing'][0]['symbol'] == '000831'
    monkeypatch.setattr('engine_core.data_pipeline.daily_dde.run_daily_dde_sync', lambda *a, **kw: result)
    code, reply = daily_dde_sync.run({'trade_date': '2026-11-03'}, Path('.'))
    assert (code, reply['status'], reply['retryable']) == (10, 'degraded', False)


def test_consecutive_fetch_failures_abort():
    import pytest
    from engine_core.data_pipeline.daily_dde import DdeSyncRetryableError
    calls = []
    def fetch(symbol, target):
        calls.append(symbol)
        raise RuntimeError('upstream unavailable')
    with pytest.raises(DdeSyncRetryableError) as caught:
        run_daily_dde_sync(['000001', '000002', '000003'], '2026-11-06',
                           store=RecordingDdeStore(), fetcher=fetch, attempts=1,
                           consecutive_failure_limit=2, skip_redis=True, sleep=lambda _: None)
    assert calls == ['000001', '000002']
    assert caught.value.summary['aborted'] == 'consecutive_failures'
    assert caught.value.summary['error_code'] == 'DDE_SYNC_INCOMPLETE_RETRYABLE'
