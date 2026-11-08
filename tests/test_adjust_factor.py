import json
from pathlib import Path

from engine_core.data_pipeline.adjust_factor import factor_for


def test_factor_for_event_boundaries():
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    record = next(record for record in records if record['code'] == 'sh.600519')
    events = [dict(zip(record['fields'], row)) for row in record['rows']]
    assert factor_for(events, '2025-06-26') == (0.960527, 'FORMAL')
    assert factor_for(events, '2025-06-25') == (0.942063, 'FORMAL')
    assert factor_for(events, '2000-01-01') == (None, 'BEFORE_FIRST_EVENT')


def test_store_replaces_before_deleting_stale_dates():
    from engine_core.data_pipeline.adjust_factor import AdjustFactorStore
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    record = next(r for r in records if r['code'] == 'sh.600519')
    rows = [dict(zip(record['fields'], r)) for r in record['rows']]

    class TD:
        database = 'core_data'
        def __init__(self):
            self.sql = []
        def _run(self, sql, **kwargs):
            self.sql.append(sql)
            if sql.startswith('SELECT'):
                row = rows[0]
                return [(row['dividOperateDate'], row['foreAdjustFactor'], row['backAdjustFactor'], row['adjustFactor'], '20260101000000-aaaaaaaa')]
            return []
    td = TD()
    store = AdjustFactorStore(td)
    store.replace_snapshot('600519', rows[1:3], '20261108000000-bbbbbbbb')
    writes = [s for s in td.sql if s.startswith(('INSERT', 'DELETE'))]
    assert writes[0].startswith('INSERT')
    assert writes[-1].startswith('DELETE')
    assert rows[0]['dividOperateDate'] not in [r['dividOperateDate'] for r in rows[1:3]]


def test_detect_suspected_real_adjacent_days():
    import gzip
    from engine_core.data_pipeline.adjust_factor import detect_suspected
    root = Path(__file__).parent / 'fixtures/real'
    path = root / 'core_artifacts/c522/daily_kline/2026-10-23/formal/rows.jsonl.gz'
    with gzip.open(path, 'rt') as stream:
        previous = next(json.loads(line) for line in stream if '"600519"' in line)
    raw = json.loads((root / 'wencai_daily_kline_2026-10-26_2109.json').read_text())['rows']
    current = next(row for row in raw if row['code'] == '600519')
    assert detect_suspected({'600519': {'pre_close': current['前收盘价:不复权[20261026]']}},
                            {'600519': previous}) == set()


def test_incremental_keeps_completed_on_upstream_failure(monkeypatch):
    from engine_core.data_pipeline import adjust_factor as module
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    rows_by_symbol = {r['code'][-6:]: [dict(zip(r['fields'], row)) for row in r['rows']] for r in records}
    monkeypatch.setattr(module, 'detect_suspected', lambda *_: {'000001', '600519'})
    class Store:
        def __init__(self): self.writes = []
        def ensure_schema(self): pass
        def read_day_rows(self, day): return {'600519': {}}
        def previous_day(self, day): return '2026-11-05'
        def read_events(self, symbol): return [], 'NO_EVENTS'
        def replace_snapshot(self, symbol, events, version): self.writes.append(symbol)
        def write_status(self, *args): pass
        def invalidate_day_marker(self, day): pass
        def write_day_marker(self, *args): self.writes.append('marker')
    store = Store()
    class Lease:
        def acquire(self): return True
        def renew(self): return True
        def release(self): pass
    def fetch(symbol):
        if symbol == '600519': raise RuntimeError('baostock_range_failed')
        return rows_by_symbol[symbol]
    code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'incremental'}}, '.',
                             store=store, fetcher=fetch, sleep=lambda _: None, lease_factory=lambda *_: Lease())
    assert code == 75
    assert reply['detail']['aborted'] is True
    assert store.writes == ['000001']


def test_incremental_spacing_and_unchanged(monkeypatch):
    from engine_core.data_pipeline import adjust_factor as module
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    rows_by_symbol = {r['code'][-6:]: [dict(zip(r['fields'], row)) for row in r['rows']] for r in records}
    monkeypatch.setattr(module, 'detect_suspected', lambda *_: {'000001', '600519'})
    class Store:
        def __init__(self): self.writes = []
        def ensure_schema(self): pass
        def read_day_rows(self, day): return {'600519': {}}
        def previous_day(self, day): return '2026-11-05'
        def read_events(self, symbol): return rows_by_symbol[symbol], 'FORMAL'
        def replace_snapshot(self, symbol, events, version): self.writes.append(symbol)
        def write_status(self, *args): pass
        def invalidate_day_marker(self, day): self.writes.append('invalidate')
        def write_day_marker(self, *args): self.writes.append('marker')
    class Lease:
        def acquire(self): return True
        def renew(self): return True
        def release(self): pass
    store, times, now = Store(), [], [0.0]
    def fetch(symbol):
        times.append(now[0])
        return rows_by_symbol[symbol]
    def sleep(seconds): now[0] += seconds
    code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'incremental'}}, '.',
                             store=store, fetcher=fetch, sleep=sleep, clock=lambda: now[0], lease_factory=lambda *_: Lease())
    assert code == 0
    assert times[1] - times[0] >= 1.0
    assert store.writes == ['invalidate', '000001', '600519', 'marker']
    assert reply['detail']['unchanged'] == 2
    assert all(sample['old_sha256'] == sample['new_sha256'] for sample in reply['detail']['samples'])
