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
    monkeypatch.setattr(plate_stock_index, 'read_reference_view', lambda *args: None)
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
    def read_reference(client, dataset, day, asof):
        ref = Ref()
        snapshot = ref.read_as_of(dataset, asof)
        if snapshot is None and dataset == 'plate_registry':
            candidate = ref.read_latest(dataset)
            if candidate['available_at'] <= asof:
                return candidate
        return snapshot
    monkeypatch.setattr(plate_stock_index, 'read_reference_view', read_reference)
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


def test_hot_buckets_real_members_and_category_filter():
    from engine_core.auction_theme_buckets import build_hot_theme_buckets
    data = json.loads((Path(__file__).parent / 'fixtures/real/c1351/excerpt.json').read_text())
    hot = next(h for h in data['hot'] if h['event_id'] == 'kaipan_hot_092520')
    result = build_hot_theme_buckets(data['auction_0925'] + data['auction_0924'], data['mapping'], hot['rows'], data['registry']['rows'], listings={r['symbol']: r['listing_date'] for r in data['stock_static']}, trading_dates=data['calendar'], top_n_hot=243)
    bank = next(r for r in result['themes'] if r['plate_code'] == '801027')
    baseline = next(r for r in data['baseline'] if r['code'] == '801027')
    assert (bank['auction_count'], bank['strong_count'], bank['middle_count'], bank['weak_count']) == (42, 0, 42, 0)
    assert bank['mean_change_bp'] / 100 == baseline['mean']
    assert bank['amount_yuan'] == baseline['amount']
    assert not {'801676', '801346', '801909'} & {r['plate_code'] for r in result['themes']}
    assert 'excluded' in str(result['notes'])


def test_hot_reader_reports_missing_data_and_cutoff(monkeypatch):
    from engine_core.auction_theme_buckets import read_and_build_hot
    data = json.loads((Path(__file__).parent / 'fixtures/real/c1351/excerpt.json').read_text())
    class Client:
        def scan_iter(self, **kwargs): return range(len(data['hot']))
        def get(self, key): return json.dumps(data['hot'][key])
    from engine_core.data_pipeline import auction_redis_view
    monkeypatch.setattr(auction_redis_view, 'read_auction_view', lambda *args: (_ for _ in ()).throw(RuntimeError('read failure')))
    result = read_and_build_hot('2026-11-17', store=object(), redis_client=Client())
    assert result['status'] == 'unavailable'
    assert 'read failure' in result['reason']
    assert 'kaipan_hot_092520' in str(result['notes'])
    assert 'kaipan_hot_094520' not in str(result['notes'])


def test_hot_seal_assignments_use_real_evidence_and_unknown():
    from engine_core.auction_theme_buckets import build_hot_theme_buckets
    data = json.loads((Path(__file__).parent / 'fixtures/real/c1351/excerpt.json').read_text())
    hot = next(h for h in data['hot'] if h['event_id'] == 'kaipan_hot_092520')
    registry = {r['plate_code']: r for r in data['registry']['rows']}
    candidate = lambda code: {'id': code, 'name': registry[code]['name']}
    pool = {r['symbol']: [candidate(r['plate_code'])] for r in data['pool']['rows'] if r.get('plate_code') in registry}
    reasons = [{'symbol': r['symbol'], 'Date': r['event_date'], 'ZSCode': [candidate(c) for c in r['plate_codes'] if c in registry]} for r in data['reason_rows']]
    result = build_hot_theme_buckets(data['auction_0925'], data['mapping'], hot['rows'], data['registry']['rows'],
        trade_date='2026-11-17', previous_trade_date='2026-11-16', trading_dates=data['calendar'],
        seal_rows=data['seals']['rows'], pool_labels=pool, reason_rows=reasons)
    assignments = {r['symbol']: r['assignment'] for r in result['seals']}
    assert assignments['600715']['source'] == 'POOL_LABEL'
    assert assignments['002685']['source'] == 'POOL_LABEL'
    assert assignments['002685']['primary_theme']['id'] == '801808'
    assert assignments['603396']['source'] == 'REASON_LATEST'
    assert assignments['300779']['source'] == 'UNKNOWN'


def test_registry_parent_name_from_recorded_registry():
    from engine_core.plate_registry import build_registry
    recorded = json.loads((Path(__file__).parent / 'fixtures/real/c1342/plate_registry.json').read_text())['rows']
    names = {r['code']: r['name'] for r in recorded}
    parents = {r['code']: r['parent'] for r in recorded if r.get('parent')}
    child = next(r for r in recorded if r.get('parent') and names.get(r['parent']))
    registry = {r['code']: r for r in build_registry({}, names, parents)['plates']}
    assert registry[child['code']]['parent_name'] == names[child['parent']]


def test_hot_index_miss_does_not_open_cold_store(monkeypatch):
    from engine_core.data_pipeline import cold_archive, plate_stock_index, redis_day_view_read
    data = json.loads((Path(__file__).parent / 'fixtures/real/c1332/excerpt.json').read_text())
    symbol = data['auction_0925'][0]['symbol']
    monkeypatch.setattr(redis_day_view_read, 'read_day_view', lambda *a, **k: {
        'source': 'td_fallback', 'rows': {}, 'meta': {}})
    monkeypatch.setattr(cold_archive, 'TdStore', lambda: (_ for _ in ()).throw(AssertionError('TD_ACCESS')))
    class Store:
        redis = object()
        cold = None
    result = plate_stock_index.read_stocks(Store(), '2026-11-16', [symbol], restore=False, hot_only=True)
    assert result[symbol]['status'] == 'UNKNOWN'
    assert result[symbol]['reason'] == 'hot_index_unavailable'


def test_preload_canonical_reference_and_direct_read(tmp_path, monkeypatch):
    import csv
    from engine_core.data_pipeline.plate_stock_index import preload_canonical_reference, read_reference_view
    from engine_core.data_pipeline import redis_day_view

    monkeypatch.setattr(redis_day_view, "expire_at", lambda *a, **k: 1800000000)

    reg_file = tmp_path / "plate_registry.csv"
    mem_file = tmp_path / "stock_plate_membership.csv"
    static_file = tmp_path / "stock_static.csv"
    with open(reg_file, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["plate_code", "name", "parent_code", "parent_name", "class", "size"])
        w.writeheader()
        w.writerow({"plate_code": "801003", "name": "5G", "parent_code": "801660", "parent_name": "通信", "class": "THEME", "size": "10"})
    with open(mem_file, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "plate_code"])
        w.writeheader()
        w.writerow({"symbol": "000001", "plate_code": "801003"})
    with open(static_file, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "name", "listing_date", "exchange", "market_board", "is_st"])
        w.writeheader()
        w.writerow({"symbol": "000001", "name": "平安银行", "listing_date": "1991-04-03", "exchange": "SZ", "market_board": "MAIN", "is_st": "false"})

    class MockPipeline:
        def __init__(self, storage): self.storage = storage
        def hset(self, key, mapping): self.storage[key] = mapping; return self
        def expireat(self, *a): return self
        def set(self, key, val, *a, **k): self.storage[key] = val; return self
        def execute(self): return [True]

    class MockRedis:
        def __init__(self): self.storage = {}
        def set(self, key, val, *a, **k): self.storage[key] = val
        def get(self, key): return self.storage.get(key)
        def delete(self, key): self.storage.pop(key, None)
        def pipeline(self): return MockPipeline(self.storage)
        def hlen(self, key): return len(self.storage.get(key, {}))

    r = MockRedis()
    res = preload_canonical_reference("2026-11-20", data_dir=tmp_path, redis_client=r)
    assert res["status"] == "READY"
    assert res["plate_registry_rows"] == 1
    assert res["plate_stock_symbols"] == 1
    assert res["stock_static_rows"] == 1

    snap = read_reference_view(r, "plate_registry", "2026-11-20", "2026-11-20T09:05:00+08:00")
    assert snap is not None
    assert snap["rows"][0]["name"] == "5G"
    assert snap["rows"][0]["parent_name"] == "通信"

    snap_static = read_reference_view(r, "stock_static", "2026-11-20", "2026-11-20T09:05:00+08:00")
    assert snap_static is not None
    assert snap_static["rows"][0]["symbol"] == "000001"
    assert snap_static["rows"][0]["listing_date"] == "1991-04-03"


def test_read_and_build_hot_reads_redis_reference_views(monkeypatch):
    from engine_core.auction_theme_buckets import read_and_build_hot
    from engine_core.data_pipeline import auction_redis_view, plate_stock_index, trade_calendar

    day = "2026-11-20"

    class MockRedis:
        def scan_iter(self, match=None, count=None):
            return ["core:cache:data:hot_plates:2026-11-20:v1"]
        def get(self, key):
            if "hot_plates" in key:
                return json.dumps({
                    "dataset": "hot_plates", "trade_date": day, "version": "v1",
                    "available_at": f"{day}T09:25:00+08:00",
                    "rows": [{"rank": 1, "plate_code": "801003", "name": "5G"}],
                    "row_count": 1
                })
            if "auction_seal_facts" in key:
                return json.dumps({
                    "dataset": "auction_seal_facts", "trade_date": day,
                    "available_at": f"{day}T09:25:30+08:00",
                    "rows": [{"symbol": "000001", "side": "limit_up", "seal_amount_yuan": 100000}],
                    "row_count": 1
                })
            if "limit_up_pool" in key:
                return json.dumps({
                    "dataset": "limit_up_pool", "trade_date": "2026-11-19",
                    "available_at": f"{day}T00:40:00+08:00",
                    "rows": [{"symbol": "000001", "plate_code": "801003", "reason_category": "通信"}],
                    "row_count": 1
                })
            return None

    monkeypatch.setattr(auction_redis_view, "read_auction_view", lambda c, d, tag: [
        {"symbol": "000001", "amount_yuan": 50000, "price_milli": 100000, "pre_close_milli": 95000, "change_bp": 526, "auction_tag": tag}
    ])
    monkeypatch.setattr(plate_stock_index, "get_stock_plates", lambda syms, d, **k: {
        "000001": {"status": "PRESENT_REDIS", "plates": [{"plate_code": "801003", "plate_class": "THEME"}]}
    })
    monkeypatch.setattr(plate_stock_index, "read_reference_view", lambda c, ds, d, asof: {
        "plate_registry": {"trade_date": day, "rows": [{"plate_code": "801003", "name": "5G", "parent_name": "通信", "class": "THEME"}]},
        "stock_static": {"trade_date": day, "rows": [{"symbol": "000001", "listing_date": "2020-01-01"}]}
    }.get(ds))
    monkeypatch.setattr(trade_calendar.TradeCalendarStore, "all_trading_dates", lambda self: [
        "2026-11-14", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"
    ])

    res = read_and_build_hot(day, redis_client=MockRedis(), top_n_hot=10)
    assert res["status"] == "available"
    assert len(res["themes"]) == 1
    assert res["themes"][0]["plate_code"] == "801003"
    assert len(res["seals"]) == 1
    assert res["seals"][0]["symbol"] == "000001"


def test_read_auction_view_with_decoded_string_client():
    import hashlib
    from engine_core.data_pipeline.auction_redis_view import read_auction_view

    raw_dict = {"000001": {"amount_yuan": 1000, "price_milli": 10000, "pre_close_milli": 9500,
                           "change_bp": 526, "bid_amount_yuan": 500, "rank_amount": 1, "src": "auction_match"}}
    raw_str = json.dumps(raw_dict, separators=(",", ":"))
    meta_str = json.dumps({"status": "COMPLETE", "rows": 1, "sha256": hashlib.sha256(raw_str.encode("utf-8")).hexdigest()},
                          separators=(",", ":"))

    class DecodedRedis:
        def mget(self, keys):
            # Returns str, simulating decode_responses=True
            return [raw_str, meta_str]

    rows = read_auction_view(DecodedRedis(), "2026-11-20", "0925")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "000001"
    assert rows[0]["price_milli"] == 10000
    assert rows[0]["auction_tag"] == "0925"



