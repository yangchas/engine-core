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


def test_store_snapshot_single_insert_roundtrip():
    from engine_core.data_pipeline.adjust_factor import AdjustFactorStore
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    record = next(r for r in records if r['code'] == 'sh.600519')
    rows = [dict(zip(record['fields'], r)) for r in record['rows']]
    class TD:
        database = 'core_data'
        def __init__(self): self.sql = []
        def _run(self, sql, **kwargs):
            self.sql.append(sql)
            return []
    td = TD()
    store = AdjustFactorStore(td)
    store.replace_snapshot('600519', rows, '20261108000000-bbbbbbbb')
    writes = [s for s in td.sql if s.startswith(('INSERT', 'DELETE'))]
    assert len(writes) == 1
    assert 'events_json' in writes[0]
    payload = writes[0].rsplit("'", 2)[1]
    data = json.loads(payload)
    assert [item['date'] for item in data] == sorted(row['dividOperateDate'] for row in rows)
    td._run = lambda sql, **kwargs: [('20261108000000-bbbbbbbb', len(rows), __import__('engine_core.data_pipeline.adjust_factor', fromlist=['_snapshot_digest'])._snapshot_digest(rows), payload)]
    actual, quality = store.read_events('600519')
    assert quality == 'FORMAL'
    assert [(x['dividOperateDate'], x['foreAdjustFactor']) for x in actual] == [(x['dividOperateDate'], x['foreAdjustFactor']) for x in sorted(rows, key=lambda x: x['dividOperateDate'])]

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
    from decimal import Decimal
    close = Decimal(str(previous['close']))
    assert detect_suspected({'600519': {'pre_close': close * Decimal('1.000001')}}, {'600519': previous}) == set()
    assert detect_suspected({'600519': {'pre_close': close * Decimal('1.0000011')}}, {'600519': previous}) == {'600519'}


def test_incremental_keeps_completed_on_upstream_failure(monkeypatch):
    from engine_core.data_pipeline import adjust_factor as module
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    rows_by_symbol = {r['code'][-6:]: [dict(zip(r['fields'], row)) for row in r['rows']] for r in records}
    monkeypatch.setattr(module, 'detect_suspected', lambda *_: {'000001', '600519'})
    class Store:
        def __init__(self): self.writes = []
        def ensure_schema(self): pass
        def read_day_rows(self, day): return {'600519': {}}
        def listing_symbols(self, day, include_delisted=True): return {'600519'}, 'stock_listing'
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
        if symbol == '000001': raise RuntimeError('baostock_range_failed')
        return rows_by_symbol[symbol]
    code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'incremental'}}, '.',
                             store=store, fetcher=fetch, sleep=lambda _: None, lease_factory=lambda *_: Lease())
    assert code == 10
    assert reply['detail']['aborted'] is False
    assert reply['detail']['failed_total'] == 1
    assert reply['detail']['failed_samples'] == [{'symbol': '000001', 'error_code': 'BAOSTOCK_RANGE_FAILED'}]
    assert store.writes == ['600519']


def test_incremental_spacing_and_unchanged(monkeypatch):
    from engine_core.data_pipeline import adjust_factor as module
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    rows_by_symbol = {r['code'][-6:]: [dict(zip(r['fields'], row)) for row in r['rows']] for r in records}
    monkeypatch.setattr(module, 'detect_suspected', lambda *_: {'000001', '600519'})
    class Store:
        def __init__(self): self.writes = []
        def ensure_schema(self): pass
        def read_day_rows(self, day): return {'600519': {}}
        def listing_symbols(self, day, include_delisted=True): return {'600519'}, 'stock_listing'
        def previous_day(self, day): return '2026-11-05'
        def read_events(self, symbol): return rows_by_symbol[symbol], 'FORMAL'
        def replace_snapshot(self, symbol, events, version): self.writes.append(symbol)
        def write_status(self, *args): pass
        def invalidate_day_marker(self, day): self.writes.append('invalidate')
        def write_day_marker(self, *args): self.writes.append('marker'); self.marker_count = args[3]
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
    assert store.writes == ['invalidate', 'marker']
    assert reply['detail']['unchanged'] == 2
    assert store.marker_count == 2
    assert all(sample['old_sha256'] == sample['new_sha256'] for sample in reply['detail']['samples'])


def test_bootstrap_resumes_empty_and_completed():
    from engine_core.data_pipeline import adjust_factor as module
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    record = next(r for r in records if r['code'] == 'sh.600519')
    events = [dict(zip(record['fields'], row)) for row in record['rows']]
    class Store:
        def __init__(self): self.states = {'000001': []}; self.markers = []
        def ensure_schema(self): pass
        def listing_symbols(self, day): return {'000001', '600519'}, 'stock_listing'
        def read_events(self, symbol): return (self.states[symbol], 'FORMAL' if self.states[symbol] else 'NO_EVENTS') if symbol in self.states else ([], 'UNPROCESSED')
        def replace_snapshot(self, symbol, rows, version): self.states[symbol] = rows
        def invalidate_day_marker(self, day): pass
        def write_day_marker(self, *args): self.markers.append(args)
    class Lease:
        def acquire(self): return True
        def renew(self): return True
        def release(self): pass
    store, calls = Store(), []
    def fetch(symbol): calls.append(symbol); return events
    for _ in range(2):
        code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'bootstrap'}}, '.',
                                 store=store, fetcher=fetch, lease_factory=lambda *_: Lease())
        assert code == 0
    assert calls == ['600519']
    assert store.states['600519'] == events
    assert factor_for(store.states['000001'], '2026-11-06') == (None, 'NO_EVENTS')
    assert len(store.markers) == 2


def test_many_batches_and_resource_sharding():
    from engine_core.data_pipeline.adjust_factor import AdjustFactorStore, _snapshot_digest
    from engine_core.data_pipeline.td_sink import TaosStore, TdResourceExhausted
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    record = next(r for r in records if r['code'] == 'sh.600519')
    rows = [dict(zip(record['fields'], r)) for r in record['rows']]
    entries = [{'date': r['dividOperateDate'], 'fore': r['foreAdjustFactor'], 'back': r['backAdjustFactor'], 'adjust': r['adjustFactor']} for r in rows]
    payload = json.dumps(entries)
    class TD:
        database = 'core_data'
        _day_scan = TaosStore._day_scan
        def __init__(self): self.calls = []; self.shard_events = []; self.failed = False
        def _run(self, sql, **kwargs):
            self.calls.append(sql)
            if not self.failed:
                self.failed = True
                raise TdResourceExhausted('0x073a')
            if "symbol >= '5'" in sql and "symbol < '7'" in sql:
                return [('600519', '20261108000000-bbbbbbbb', len(rows), _snapshot_digest(rows), payload)]
            return []
    td = TD()
    result = AdjustFactorStore(td).read_events_many([f'{n:06d}' for n in range(201)] + ['600519'])
    assert result['600519'][1] == 'FORMAL'
    assert td.shard_events == [{'segments': 4}]
    assert len(td.calls) == 6


def test_snapshot_rejects_oversize():
    import pytest
    from engine_core.data_pipeline.adjust_factor import AdjustFactorStore
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    record = next(r for r in records if r['code'] == 'sh.600519')
    rows = [dict(zip(record['fields'], r)) for r in record['rows']]
    rows[0]['foreAdjustFactor'] += '0' * 60000
    class TD:
        database = 'core_data'
        def _run(self, sql, **kwargs): raise AssertionError('unexpected TD access')
    with pytest.raises(ValueError, match='exceeds_60000_bytes'):
        AdjustFactorStore(TD()).replace_snapshot('600519', rows, '20261108000000-bbbbbbbb')


def test_incremental_rejects_missing_or_incomplete_day_rows(monkeypatch):
    from engine_core.data_pipeline import adjust_factor as module
    records = json.loads((Path(__file__).parent / 'fixtures/adjust_factor_c846/events.json').read_text())
    symbols = [r['code'][-6:] for r in records]
    assert len(symbols) >= 3
    class Store:
        def __init__(self, rows): self.rows = rows
        def ensure_schema(self): pass
        def previous_day(self, day): return '2026-11-05'
        def read_day_rows(self, day): return self.rows
        def listing_symbols(self, day, include_delisted=True): return set(symbols), 'stock_listing'
        def invalidate_day_marker(self, day): raise AssertionError('marker invalidated')
    class Lease:
        def acquire(self): return True
        def release(self): pass
    monkeypatch.setattr(module, 'detect_suspected', lambda *_: (_ for _ in ()).throw(AssertionError('discovery called')))
    for rows, reason in (({}, 'NO_DAY_ROWS'), ({symbols[0]: {}}, 'DAY_ROWS_INCOMPLETE')):
        code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'incremental'}}, '.',
                                 store=Store(rows), lease_factory=lambda *_: Lease())
        assert code == 10
        assert reply['detail']['reason'] == reason


def test_bootstrap_retries_isolated_range_failure():
    from engine_core.data_pipeline import adjust_factor as module
    class Store:
        def ensure_schema(self): pass
        def listing_symbols(self, day): return {'600519'}, 'stock_listing'
        def read_events(self, symbol): return [], 'UNPROCESSED'
        def replace_snapshot(self, symbol, rows, version): pass
        def invalidate_day_marker(self, day): pass
        def write_day_marker(self, *args): pass
    class Lease:
        def acquire(self): return True
        def renew(self): return True
        def release(self): pass
    calls, now, sleeps = [], [0.0], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    def fetch(symbol):
        calls.append((symbol, now[0]))
        if len(calls) == 1:
            raise RuntimeError('baostock_range_failed')
        return []
    code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'bootstrap'}}, '.',
                             store=Store(), fetcher=fetch, sleep=sleep, clock=lambda: now[0],
                             lease_factory=lambda *_: Lease())
    assert code == 0
    assert reply['detail']['failed_total'] == 0
    assert reply['detail']['aborted'] is False
    assert len(calls) == 2
    assert sleeps == [5] and calls[1][1] - calls[0][1] >= 1


def test_bootstrap_aborts_after_five_consecutive_failures():
    from engine_core.data_pipeline import adjust_factor as module
    class Store:
        def __init__(self): self.writes = []
        def ensure_schema(self): pass
        def listing_symbols(self, day): return {f'{i:06d}' for i in range(7)}, 'stock_listing'
        def read_events(self, symbol): return [], 'UNPROCESSED'
        def replace_snapshot(self, symbol, rows, version): self.writes.append(symbol)
        def invalidate_day_marker(self, day): pass
        def write_day_marker(self, *args): raise AssertionError('unexpected marker')
    class Lease:
        def acquire(self): return True
        def renew(self): return True
        def release(self): pass
    calls = []
    def fetch(symbol):
        calls.append(symbol)
        if symbol != '000000': raise RuntimeError('baostock_range_failed')
        return []
    store = Store()
    code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'bootstrap'}}, '.',
                             store=store, fetcher=fetch, sleep=lambda _: None,
                             lease_factory=lambda *_: Lease())
    assert code == 75 and reply['error_code'] == 'BAOSTOCK_RANGE_FAILED'
    assert reply['detail']['aborted'] is True and reply['detail']['failed_total'] == 5
    assert store.writes == ['000000'] and len(calls) == 16


def test_bootstrap_login_failure_aborts_immediately():
    from engine_core.data_pipeline import adjust_factor as module
    class Store:
        def ensure_schema(self): pass
        def listing_symbols(self, day): return {'600519'}, 'stock_listing'
        def read_events(self, symbol): return [], 'UNPROCESSED'
        def invalidate_day_marker(self, day): pass
    class Lease:
        def acquire(self): return True
        def release(self): pass
    calls = []
    def fetch(symbol):
        calls.append(symbol)
        raise RuntimeError('baostock_login_failed')
    code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'bootstrap'}}, '.',
                             store=Store(), fetcher=fetch, sleep=lambda _: None,
                             lease_factory=lambda *_: Lease())
    assert code == 75 and reply['error_code'] == 'BAOSTOCK_LOGIN_FAILED'
    assert reply['detail']['aborted'] is True and len(calls) == 1


def test_bootstrap_failure_ratio_threshold():
    from engine_core.data_pipeline import adjust_factor as module
    class Store:
        def ensure_schema(self): pass
        def listing_symbols(self, day): return {f'{i:06d}' for i in range(21)}, 'stock_listing'
        def read_events(self, symbol): return [], 'UNPROCESSED'
        def replace_snapshot(self, symbol, rows, version): pass
        def invalidate_day_marker(self, day): pass
        def write_day_marker(self, *args): raise AssertionError('unexpected marker')
    class Lease:
        def acquire(self): return True
        def renew(self): return True
        def release(self): pass
    def fetch(symbol):
        if int(symbol) in {0, 4, 8, 12, 19}:
            raise ConnectionError('receive failure')
        return []
    code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'bootstrap'}}, '.',
                             store=Store(), fetcher=fetch, sleep=lambda _: None,
                             lease_factory=lambda *_: Lease())
    assert code == 75 and reply['error_code'] == 'BAOSTOCK_CONNECTION_ERROR'
    assert reply['detail']['requested'] == 20 and reply['detail']['failed_total'] == 5
    assert reply['detail']['aborted'] is True


def _listing_fixture(monkeypatch):
    from engine_core.data_pipeline import reference_snapshot_td
    fixture = json.loads((Path(__file__).parent / 'fixtures/stock_listing_c819/basic_subset.json').read_text())
    rows = [dict(zip(fixture['fields'], row)) for row in fixture['rows']]
    rows = [{'symbol': row['code'], 'ipo_date': row['ipoDate'], 'out_date': row['outDate'] or None} for row in rows]
    class Reference:
        def __init__(self, **kwargs): pass
        def read_latest(self, dataset): return {'rows': rows}
        def close(self): pass
    monkeypatch.setattr(reference_snapshot_td, 'ReferenceSnapshotStore', Reference)
    return rows


def test_listing_includes_recent_delisting(monkeypatch):
    from engine_core.data_pipeline.adjust_factor import AdjustFactorStore
    _listing_fixture(monkeypatch)
    class TD: database = 'core_data'
    store = AdjustFactorStore(TD())
    symbols, source = store.listing_symbols('2026-11-06')
    assert '301139' in symbols
    assert '000972' in symbols
    assert source == 'stock_listing'
    assert '301139' not in store.listing_symbols('2026-11-06', include_delisted=False)[0]
    # The sole delisted fixture row is 301139, dated 2026-10-28.
    assert '301139' not in store.listing_symbols('2028-01-01')[0]


def test_listing_delisted_lookback_boundary(monkeypatch):
    from datetime import date, timedelta
    from engine_core.data_pipeline.adjust_factor import AdjustFactorStore
    _listing_fixture(monkeypatch)
    class TD: database = 'core_data'
    store = AdjustFactorStore(TD())
    out = date.fromisoformat('2026-10-28')
    assert '301139' in store.listing_symbols((out + timedelta(days=366)).isoformat())[0]
    assert '301139' not in store.listing_symbols((out + timedelta(days=367)).isoformat())[0]


def test_bootstrap_counts_delisted_and_skips_existing(monkeypatch):
    from engine_core.data_pipeline import adjust_factor as module
    _listing_fixture(monkeypatch)
    class TD: database = 'core_data'
    class Store(module.AdjustFactorStore):
        def __init__(self): super().__init__(TD()); self.completed = set()
        def ensure_schema(self): pass
        def read_events(self, symbol): return [], 'UNPROCESSED' if symbol == '301139' and symbol not in self.completed else 'NO_EVENTS'
        def replace_snapshot(self, symbol, rows, version): self.completed.add(symbol)
        def invalidate_day_marker(self, day): pass
        def write_day_marker(self, *args): pass
    class Lease:
        def acquire(self): return True
        def renew(self): return True
        def release(self): pass
    calls = []
    store = Store()
    for _ in range(2):
        code, reply = module.run({'trade_date': '2026-11-06', 'args': {'mode': 'bootstrap'}}, '.',
                                 store=store, fetcher=lambda symbol: calls.append(symbol) or [],
                                 lease_factory=lambda *_: Lease())
        assert code == 0
        assert reply['detail']['delisted_included'] == 1
    assert calls == ['301139']
