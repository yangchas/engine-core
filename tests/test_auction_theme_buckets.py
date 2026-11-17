import json
from pathlib import Path

from engine_core.auction_theme_buckets import build_theme_buckets
from engine_core.data_pipeline.plate_stock_index import build_index


def test_real_excerpt_theme_counts_and_missing_price():
    data = json.loads((Path(__file__).parent / 'fixtures/real/c1332/excerpt.json').read_text())
    mapping = build_index(data['plate_members'])
    result = build_theme_buckets(data['auction_0925'] + data['auction_0924'], mapping)
    theme = next(row for row in result['themes'] if row['plate_code'] == '801003')
    assert theme['member_count'] == 45
    assert theme['auction_count'] == 45
    assert theme['amount_yuan'] == 1621416239
    assert theme['mean_change_bp'] == -178.6
    assert theme['median_change_bp'] == -218
    assert theme['amount_delta_yuan'] == 893901896
    assert result['market']['amount_yuan'] == 8029343768
    symbol = next(r['symbol'] for r in data['auction_0925'] if r['symbol'] in mapping and len(mapping[r['symbol']]) > 1)
    reduced = [r for r in data['auction_0925'] if r['symbol'] != symbol]
    missing = build_theme_buckets(reduced, mapping)
    codes = {p['plate_code'] for p in mapping[symbol] if p['plate_class'] == 'THEME'}
    assert len(codes) > 1
    assert all(r['missing_price_count'] >= 1 for r in missing['themes'] if r['plate_code'] in codes)


def test_real_excerpt_unknown_listing_and_unavailable_mapping():
    data = json.loads((Path(__file__).parent / 'fixtures/real/c1332/excerpt.json').read_text())
    mapping = build_index(data['plate_members'])
    result = build_theme_buckets(data['auction_0925'], mapping)
    theme = next(row for row in result['themes'] if row['plate_code'] == '801003')
    assert theme['unknown_limit_count'] == theme['auction_count'] == 45
    assert theme['distribution'] is None
    assert theme['limit_up_count'] == 0  # known portion; all 45 remain unknown
    assert theme['limit_down_count'] == 0
    assert build_theme_buckets(data['auction_0925'], None)['status'] == 'unavailable'


def test_real_limits_names_and_classes():
    import gzip
    from engine_core.data_pipeline.auction_seal_facts import classify_limit
    real = Path(__file__).parent / 'fixtures/real'
    data = json.loads((real / 'c1332/excerpt.json').read_text())
    excerpt = json.loads((real / 'c1337/excerpt.json').read_text())
    calendar = json.loads((real.parent / 'trade_calendar/baostock-2026-01-01_2026-12-31.json').read_text())
    dates = [r['calendar_date'] for r in calendar['rows'] if r['is_trading_day'] == '1']
    listings = {r['symbol']: r['listing_date'] for r in excerpt['stock_static']}
    names = {r['plate_code']: r['name'] for r in excerpt['hot_plates'] if r['name']}
    mapping = build_index(data['plate_members'])
    quote = next(r for r in data['auction_0925'] if r['symbol'] == '002242')
    assert classify_limit('2026-11-16', '002242', quote, listings['002242'], dates) == 'limit_up'
    complete = build_theme_buckets(data['auction_0925'], mapping, names=names, listings=listings, trading_dates=dates)
    full = next(r for r in complete['themes'] if r['plate_code'] == '801053')
    assert full['limit_up_count'] == 1
    missing_symbol = next(s for s, plates in mapping.items() if s != '002242' and any(p['plate_code'] == '801053' for p in plates) and s in listings)
    listings.pop(missing_symbol)
    result = build_theme_buckets(data['auction_0925'] + data['auction_0924'], mapping,
                                 names=names, listings=listings, trading_dates=dates)
    theme = next(r for r in result['themes'] if r['plate_code'] == '801053')
    assert theme['unknown_limit_count'] >= 1 and theme['limit_up_count'] == full['limit_up_count']
    assert any(r['name'] for r in result['themes']) and any(r['name'] is None for r in result['themes'])
    assert 'names_source' in str(result['notes']) and 'coverage' in str(result['notes'])
    assert set(result['by_class']) == {'NICHE', 'BROAD', 'GENERIC_FACTOR', 'EVENT'}
    assert all(r['class'] == 'THEME' for r in result['themes'])
    with gzip.open(real / 'c1256/auction.json.gz', 'rt') as f:
        auction = json.load(f)
    raw = json.loads(next(real.glob('td_stock_static_2026-11-16_688827_*.json')).read_text())
    listing = json.loads(raw['rows'][0][raw['columns'].index('payload')])['listing_date']
    assert classify_limit('2026-11-16', '688827', auction['688827'], listing, dates) == 'unlimited'


def test_read_and_build_reports_snapshot_and_mapping_failures(monkeypatch):
    from engine_core.auction_theme_buckets import read_and_build
    from engine_core.data_pipeline import auction_redis_view, plate_stock_index, reference_snapshot_td
    real = Path(__file__).parent / 'fixtures/real/c1332/excerpt.json'
    data = json.loads(real.read_text())
    monkeypatch.setattr(auction_redis_view, 'read_auction_view', lambda client, day, tag: data['auction_' + tag])
    monkeypatch.setattr(plate_stock_index, 'get_stock_plates', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('mapping_failure')))
    result = read_and_build('2026-11-16', store=object(), redis_client=object())
    assert result['status'] == 'unavailable' and 'mapping' in result['reason']
    monkeypatch.setattr(plate_stock_index, 'get_stock_plates', lambda symbols, *a, **k: {s: {'status': 'PRESENT_REDIS', 'plates': []} for s in symbols})
    class NoSnapshot:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read_as_of(self, *args): return None
    monkeypatch.setattr(reference_snapshot_td, 'ReferenceSnapshotStore', NoSnapshot)
    result = read_and_build('2026-11-16', store=object(), redis_client=object())
    assert result['status'] == 'unavailable' and 'stock_static' in result['reason']


def test_registry_names_and_point_in_time_fallback_from_real_excerpts(monkeypatch):
    from engine_core.auction_theme_buckets import read_and_build
    from engine_core.data_pipeline import auction_redis_view, plate_stock_index, reference_snapshot_td, trade_calendar
    real = Path(__file__).parent / 'fixtures/real'
    data = json.loads((real / 'c1332/excerpt.json').read_text())
    excerpt = json.loads((real / 'c1337/excerpt.json').read_text())
    registry = json.loads((real / 'c1342/plate_registry.json').read_text())
    calendar = json.loads((real.parent / 'trade_calendar/baostock-2026-01-01_2026-12-31.json').read_text())
    dates = [r['calendar_date'] for r in calendar['rows'] if r['is_trading_day'] == '1']
    mapping = build_index(data['plate_members'])
    monkeypatch.setattr(auction_redis_view, 'read_auction_view', lambda client, day, tag: data['auction_' + tag])
    monkeypatch.setattr(plate_stock_index, 'get_stock_plates', lambda symbols, *a, **k: {
        s: {'status': 'PRESENT_REDIS', 'plates': mapping.get(s, [])} for s in symbols})
    monkeypatch.setattr(trade_calendar.TradeCalendarStore, 'all_trading_dates', lambda self: dates)
    class Client:
        def scan_iter(self, **kwargs): return [b'hot']
        def get(self, key):
            return json.dumps({'dataset': 'hot_plates', 'trade_date': '2026-11-16',
                               'captured_at': '2026-11-16T09:00:00+08:00',
                               'rows': excerpt['hot_plates'], 'row_count': len(excerpt['hot_plates'])})
    class Ref:
        mode = 'visible'
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read_as_of(self, dataset, asof):
            if dataset == 'stock_static':
                return {'trade_date': '2026-11-16', 'rows': excerpt['stock_static']}
            assert asof == '2026-11-16T09:05:00+08:00'
            if self.mode == 'error': raise RuntimeError('read failure')
            return registry if self.mode == 'visible' else None
        def read_latest(self, dataset): return registry
    monkeypatch.setattr(reference_snapshot_td, 'ReferenceSnapshotStore', Ref)
    hot = {r['plate_code']: r['name'] for r in excerpt['hot_plates'] if r.get('name')}
    rows = registry['rows']
    named = next(r for r in rows if r.get('name') and r['code'] not in hot)
    missing = next(r for r in rows if not r.get('name') and r['code'] not in hot)
    hot_code = next(c for c in hot if any(r['code'] == c and r.get('name') for r in rows))
    # Project real rows to exercise each identity field, and omit one real registry row to test hot supplementation.
    registry['rows'] = [{k: v for k, v in r.items() if k != ('plate_code' if r['code'] == named['code'] else 'code')}
                        for r in rows if r['code'] != hot_code]
    result = read_and_build('2026-11-16', store=object(), redis_client=Client())
    themes = {r['plate_code']: r['name'] for r in result['themes']}
    assert themes[named['code']] == named['name']
    assert themes[hot_code] == hot[hot_code]
    assert themes[missing['code']] is None
    plate_only = next(r for r in rows if r.get('name') and r['code'] != named['code'] and r['code'] != hot_code)
    assert themes[plate_only['code']] == plate_only['name']
    assert 'names_source=plate_registry+hot_plates' in result['notes'][0]
    assert 'registry_coverage=' in result['notes'][0] and 'hot_plates_supplement=' in result['notes'][0] and 'missing=' in result['notes'][0]
    for mode in ('invisible', 'error'):
        Ref.mode = mode
        result = read_and_build('2026-11-16', store=object(), redis_client=Client())
        themes = {r['plate_code']: r['name'] for r in result['themes']}
        assert themes[named['code']] is None and themes[hot_code] == hot[hot_code]
        assert 'registry_unavailable_at_asof' in str(result['notes'])
