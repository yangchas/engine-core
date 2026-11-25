import gzip
import json
from pathlib import Path

from engine_core.data_pipeline.auction_seal_facts import build_facts
from engine_core.data_pipeline.plate_stock_index import build_index

REAL = Path(__file__).parent / 'fixtures' / 'real'
DAY = '2026-11-16'


def real_inputs():
    with gzip.open(REAL / 'c1256/auction.json.gz', 'rt') as stream:
        auction = json.load(stream)
    with gzip.open(REAL / 'core_artifacts/c594/stock_static/2026-10-29/c573_guardian/rows.jsonl.gz', 'rt') as stream:
        listings = {r['symbol']: r['listing_date'] for line in stream if (r := json.loads(line))}
    for symbol in ('001381', '688827'):
        raw = json.loads(next(REAL.glob(f'td_stock_static_{DAY}_{symbol}_*.json')).read_text())
        listings[symbol] = json.loads(raw['rows'][0][raw['columns'].index('payload')])['listing_date']
    ticks = {}
    for path in REAL.glob(f'td_stock_tick_{DAY}_*_092430_092600_*.json'):
        raw = json.loads(path.read_text())
        ticks[path.name.split('_')[4]] = [dict(zip(raw['columns'], row)) for row in raw['rows']]
    raw = json.loads(next(REAL.glob(f'td_stock_tick_{DAY}_002129_092457_092503_*.json')).read_text())
    ticks['002129'] = [dict(zip(raw['columns'], row)) for row in raw['rows']]
    pre = ticks['002129'][0]
    auction['002129'] = {'price_milli': pre['px_milli'], 'pre_close_milli': pre['pc_milli']}
    calendar = json.loads((REAL.parent / 'trade_calendar/baostock-2026-01-01_2026-12-31.json').read_text())
    dates = [r['calendar_date'] for r in calendar['rows'] if r['is_trading_day'] == '1']
    facts = build_facts(DAY, auction, listings, dates, lambda symbol: ticks[symbol])
    with gzip.open(REAL / 'core_artifacts/c594/plate_members/2026-10-28/current/rows.jsonl.gz', 'rt') as stream:
        plates = build_index([json.loads(line) for line in stream])
    return facts, plates


def test_real_facts_summary_and_missing_amount():
    from engine_core.auction_seal_report import build_seal_section
    facts, plates = real_inputs()
    section = build_seal_section(facts, plates)
    for side in ('limit_up', 'limit_down'):
        valid = [r for r in facts['rows'] if r['side'] == side and r['seal_amount_yuan'] is not None and r['price_source'] == 'auction_match']
        assert section[side]['confirmed']['count'] == len(valid)
        assert section[side]['confirmed']['seal_amount_yuan'] == sum(r['seal_amount_yuan'] for r in valid)
        assert section[side]['confirmed']['top_stocks'] == sorted(valid, key=lambda r: (-r['seal_amount_yuan'], r['symbol']))[:10]
        assert all(r['symbol'] != '002129' for r in section[side]['confirmed']['top_stocks'])
        expected = {}
        for row in valid:
            for plate in plates.get(row['symbol'], []):
                code = plate['plate_code']
                count, amount = expected.get(code, (0, 0))
                expected[code] = (count + 1, amount + row['seal_amount_yuan'])
        actual = section['themes'][side]
        assert [(r['plate_code'], r['count'], r['seal_amount_yuan']) for r in actual] == [
            (code, count, amount) for code, (count, amount) in sorted(expected.items(), key=lambda p: (-p[1][1], p[0]))[:10]]
    assert '002129' in section['missing']['seal_amount_yuan']['symbols']
    for key in ('unknown_listing', 'unknown_prices', 'unlimited'):
        assert section['missing'][key] == {'count': len(facts[key]), 'symbols': sorted(facts[key])[:20]}
    assert section['themes']['status'] == 'available' and section['notes']
    assert build_seal_section(facts, None)['themes']['status'] == 'unavailable'


def test_read_replays_real_facts_and_unavailable(monkeypatch):
    from engine_core import auction_seal_report as report
    facts, plates = real_inputs()
    payload = dict(facts, dataset='auction_seal_facts', trade_date=DAY, row_count=len(facts['rows']), version='auction_seal_facts_v1', available_at=DAY + 'T09:26:00+08:00')
    class Redis:
        def get(self, key):
            return json.dumps(payload).encode()
    class Store:
        redis = Redis()
        cold = None
    def get_plates(symbols, trade_date, **kwargs):
        assert trade_date == DAY
        return {s: {'status': 'PRESENT_REDIS', 'version': 'captured', 'plates': plates.get(s, [])} for s in symbols}
    monkeypatch.setattr(report.plate_stock_index, 'get_stock_plates', get_plates)
    result = report.read_and_build(DAY, store=Store())
    assert result['status'] == 'available' and result['themes']['status'] == 'available'
    monkeypatch.setattr(report.plate_stock_index, 'get_stock_plates', lambda *a, **kw: (_ for _ in ()).throw(ConnectionError()))
    result = report.read_and_build(DAY, store=Store())
    assert result['status'] == 'available' and result['themes']['status'] == 'unavailable'
    assert report.read_and_build('invalid', store=Store())['status'] == 'unavailable'


def test_cold_fallback_replays_real_facts_without_redis_writes(monkeypatch):
    import hashlib
    from engine_core import auction_seal_report as report
    facts, plates = real_inputs()
    payload = dict(facts, dataset='auction_seal_facts', trade_date=DAY, row_count=len(facts['rows']),
                   version='auction_seal_facts_v1', available_at=DAY + 'T09:26:00+08:00')
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    version = payload['version'] + ':' + hashlib.sha256(raw).hexdigest()[:16]
    class Redis:
        def get(self, key):
            raise ConnectionError('redis unavailable')
    class Cold:
        def latest_version(self, dataset, day):
            assert (dataset, day) == ('auction_seal_facts', DAY)
            return version
    class Store:
        redis = Redis()
        cold = Cold()
    monkeypatch.setattr(report.cold_archive, 'read_envelope', lambda dataset, day, ver, *, store: raw)
    monkeypatch.setattr(report.plate_stock_index, 'get_stock_plates',
                        lambda symbols, day, **kw: {s: {'status': 'RESTORED_FROM_TD', 'version': 'captured', 'plates': plates.get(s, [])} for s in symbols})
    result = report.read_and_build(DAY, store=Store())
    assert result == report.build_seal_section(facts, plates)
    monkeypatch.setattr(report.cold_archive, 'read_envelope', lambda *args, **kw: raw + b' ')
    assert report.read_and_build(DAY, store=Store())['status'] == 'unavailable'


def test_real_price_sources_are_separated():
    from engine_core.auction_seal_report import build_seal_section
    facts, plates = real_inputs()
    excerpt = {}
    for line in (REAL / 'c1317/seals.out').read_text().splitlines():
        prefix, payload = line.split('[', 1)
        side = prefix.split()[1]
        excerpt[side] = json.loads('[' + payload)
    facts['rows'] = [r for r in facts['rows'] if r['symbol'] != '002129']
    section = build_seal_section(facts, plates)
    for side, symbol in (('limit_up', '002905'), ('limit_down', '002973')):
        confirmed = [r for r in excerpt[side] if r['source'] == 'auction_match']
        estimated = [r for r in excerpt[side] if r['source'] != 'auction_match']
        assert section[side]['confirmed']['count'] == len(confirmed)
        assert section[side]['confirmed']['seal_amount_yuan'] == sum(r['amount_yuan'] for r in confirmed)
        assert section[side]['estimated']['count'] == len(estimated)
        assert section[side]['estimated']['seal_amount_yuan'] == sum(r['amount_yuan'] for r in estimated)
        assert [r['symbol'] for r in section[side]['estimated']['rows']] == [symbol]
        assert symbol not in [r['symbol'] for r in section[side]['confirmed']['top_stocks']]
        assert 'seal_amount_yuan' not in section[side]


def test_real_nov17_price_source_counts():
    from engine_core.auction_seal_report import build_seal_section
    lines = (REAL / 'c1326/report.out').read_text().splitlines()
    captured = json.loads(next(line.removeprefix('read_and_build ') for line in lines if line.startswith('read_and_build ')))
    rows = captured['limit_up']['estimated']['rows'] + captured['limit_down']['estimated']['rows']
    # The two indicative symbols and side counts are recorded in the source output.
    indicative = {line.split()[2] for line in lines if line.startswith('SAMPLE ') and 'actual_src indicative_bid_ask_mid' in line}
    assert len(rows) == 20 and indicative == {'002724', '300553'}
    facts = {'rows': [dict(row, price_source='indicative_bid_ask_mid' if row['symbol'] in indicative else 'auction_match') for row in rows],
             'unknown_listing': [], 'unknown_prices': [], 'unlimited': []}
    section = build_seal_section(facts, None)
    assert (section['limit_up']['confirmed']['count'], section['limit_up']['estimated']['count']) == (3, 0)
    assert (section['limit_down']['confirmed']['count'], section['limit_down']['estimated']['count']) == (15, 2)
