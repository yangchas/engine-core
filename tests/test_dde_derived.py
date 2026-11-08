from engine_core.data_pipeline.dde_derived import ratio


def test_ratio_branches():
    assert ratio(1, 2) == (0.5, 'FORMAL', None)
    assert ratio(1, 0) == (None, 'PARTIAL', 'DENOM_ZERO')
    assert ratio(1, None) == (None, 'PARTIAL', 'DENOM_NULL')
    assert ratio(None, 1) == (None, 'PARTIAL', 'DDJE_NULL')
    assert ratio(float('nan'), 1) == (None, 'PARTIAL', 'NON_FINITE')
    assert ratio(1, float('inf')) == (None, 'PARTIAL', 'NON_FINITE')

import json
import re
import statistics
from pathlib import Path
import pytest
from engine_core.data_pipeline.dde_derived import DdeDerivedStore, run

FIX = Path(__file__).parent / 'fixtures'


def real_data():
    raw = json.loads((FIX / 'dde_c841/000001.json').read_text())
    dde = dict(zip(raw['Date'], raw['DDJE'], strict=True))
    bars = [json.loads(line) for line in (FIX / 'price_volume_c852/daily_kline.jsonl').read_text().splitlines()]
    pairs = [(dde[b['ts'][:10].replace('-', '')], b) for b in bars
             if b['symbol'] == '000001' and b['ts'][:10].replace('-', '') in dde]
    return pairs, bars


def test_real_pairs():
    pairs, _ = real_data()
    assert len(pairs) >= 100
    values = []
    for ddje, bar in pairs:
        result, quality, reason = ratio(ddje, bar['amount_yuan'])
        assert result == pytest.approx(ddje / bar['amount_yuan'], rel=1e-12, abs=0)
        assert (quality, reason) == ('FORMAL', None)
        values.append(abs(result))
    assert 0.01 <= statistics.median(values) <= 0.2


class Stub:
    def __init__(self, dde, bars):
        self.dde, self.bars = dde, bars
        self.results, self.markers, self.calls = {}, {}, []
    def ensure_schema(self): pass
    def read_dde(self, day): self.calls.append(('dde', day)); return self.dde
    def read_day_rows(self, day): self.calls.append(('bars', day)); return self.bars
    def write_results(self, day, rows): self.results[day] = rows
    def write_day_marker(self, day, version, quality, count, digest, available): self.markers[day] = (quality, count)
    def trading_dates(self, day): return ['2026-10-29', '2026-10-30']


def test_missing_suspended_batch_and_idempotence():
    pairs, bars = real_data()
    ddje, bar = pairs[0]
    others = [b for b in bars if b['symbol'] != bar['symbol']]
    b1, b2 = next(b for b in others if b['symbol'] == '600519'), next(b for b in others if b['symbol'] == '300750')
    # Remove whole source records to exercise missingness; market values remain captured data.
    second = json.loads((FIX / 'dde_c841/000002.json').read_text())
    store = Stub({'000001': {'ddje': ddje}, '000002': {'ddje': second['DDJE'][0]}},
                 {'000001': bar, '600519': b1, '300750': dict(b2, suspended=True)})
    request = {'trade_date': '2026-10-30'}
    code, reply = run(request, '.', store=store)
    assert code == 0
    assert reply['detail']['partial_reasons'] == {'NO_KLINE_ROW': 1, 'NO_DDE_ROW': 1}
    assert store.results['2026-10-30']['000002']['ddje_ratio'] is None
    assert store.results['2026-10-30']['600519']['quality'] == 'PARTIAL'
    assert '300750' not in store.results['2026-10-30']
    assert store.markers['2026-10-30'] == ('PARTIAL', 3)
    old = {s: (r['ddje_ratio'], r['quality']) for s, r in store.results['2026-10-30'].items()}
    run(request, '.', store=store)
    assert old == {s: (r['ddje_ratio'], r['quality']) for s, r in store.results['2026-10-30'].items()}
    assert store.markers['2026-10-30'] == ('PARTIAL', 3)
    assert store.calls == [('dde', '2026-10-30'), ('bars', '2026-10-30')] * 2


def test_empty_and_rebuild():
    store = Stub({}, {})
    code, reply = run({'trade_date': '2026-10-30'}, '.', store=store)
    assert (code, reply['reason'], store.markers) == (10, 'NO_INPUT_ROWS', {})
    ddje, bar = real_data()[0][0]
    store = Stub({'000001': {'ddje': ddje}}, {'000001': bar})
    code, _ = run({'trade_date': '2026-10-30', 'args': {'from_date': '2026-10-29'}}, '.', store=store)
    assert code == 0 and sorted(store.markers) == ['2026-10-29', '2026-10-30']


def test_ddl_widths():
    class Recorder:
        database = 'core_data'
        def __init__(self): self.sql = []
        def _run(self, sql): self.sql.append(sql)
    recorder = Recorder()
    DdeDerivedStore(recorder).ensure_schema()
    for sql in recorder.sql:
        widths = [int(n) * (4 if typ.upper() == 'NCHAR' else 1)
                  for typ, n in re.findall(r'\b(VARCHAR|NCHAR)\((\d+)\)', sql, re.I)]
        assert max(widths) <= 60000 and sum(widths) <= 60100
