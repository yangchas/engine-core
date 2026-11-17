import json
from collections import Counter, defaultdict
from pathlib import Path

from engine_core.auction_active_buckets import build_active_bucket_view
from engine_core.auction_stock_themes import assign_stock_themes, render_stock_themes_html
from engine_core.auction_theme_cards import non_theme_reason

REAL = Path(__file__).parent / 'fixtures' / 'real'


def inputs():
    raw = json.loads((REAL / 'c1398/excerpt.json').read_text())
    ref = json.loads((REAL / 'c1351/excerpt.json').read_text())
    mapping = {s: x['plates'] for s, x in raw['index'].items()}
    names = {r['symbol']: r.get('name') for r in ref['stock_static']}
    plate_names = {str(r.get('plate_code') or r.get('code')): r.get('name') for r in ref['registry']['rows']}
    pool = {r['symbol'] for r in ref['pool']['rows']}
    base = build_active_bucket_view(raw['auction'], mapping, pool, ref['reason_rows'], names=names, plate_names=plate_names)
    counts = Counter(str(p['plate_code']) for plates in mapping.values() for p in plates)
    history = defaultdict(list)
    for r in ref['reason_rows']:
        history[r['symbol']].append({'date': r['date'], 'plate_codes': r['plate_codes']})
    return base, mapping, plate_names, counts, history


def test_real_assignments_partition_and_render():
    base, mapping, names, counts, history = inputs()
    view = assign_stock_themes(base['stocks'], mapping, names, counts, history)
    stocks = {x['symbol']: x for x in view['stocks']}
    x = stocks['300308']
    assert x['primary']['source'] in ('support', 'specific')
    assert not non_theme_reason(x['primary']['name'])
    assert counts[x['primary']['plate_code']] <= 800
    assert len(x['secondary']) <= 2
    assert sum(b['active_count'] for b in view['buckets']) == sum(bool(mapping.get(x['symbol'])) for x in base['stocks'])
    html = render_stock_themes_html(view)
    assert '桶表' in html and '个股表' in html and '预测' not in html


def test_real_reason_control_and_no_plate():
    base, mapping, names, counts, history = inputs()
    symbol = next(s for s in history if s in mapping and any(set(r['plate_codes']) & {p['plate_code'] for p in mapping[s]} for r in history[s]))
    stock = next((s for s in base['stocks'] if s['symbol'] == symbol), None)
    if stock is None:
        stock = {'symbol': symbol, 'name': symbol, 'change_bp': None, 'amount_yuan': None, 'sources': []}
    control = dict(mapping)
    control[symbol] = []
    view = assign_stock_themes([stock], control, names, counts, history)
    assert view['stocks'][0]['primary'] is None
    assert view['stocks'][0]['status'] == 'no_plate'
    assert view['ignored_reason_count'] > 0
    positive = assign_stock_themes([stock], mapping, names, counts, history)
    assert positive['stocks'][0]['primary']['source'] == 'reason'


def test_real_limit_counts_keep_unknowns_distinct():
    base, mapping, names, counts, history = inputs()
    ref = json.loads((REAL / 'c1351/excerpt.json').read_text())
    raw = json.loads((REAL / 'c1398/excerpt.json').read_text())
    listings = {r['symbol']: r.get('listing_date') for r in ref['stock_static']}
    active = build_active_bucket_view(raw['auction'], mapping,
        {r['symbol'] for r in ref['pool']['rows']}, ref['reason_rows'],
        names={r['symbol']: r.get('name') for r in ref['stock_static']},
        plate_names=names, trade_date='2026-11-17', listings=listings,
        trading_dates=ref['calendar'])
    view = assign_stock_themes(active['stocks'], mapping, names, counts, history, limit_available=True)
    for bucket in view['buckets']:
        rows = [s for s in view['stocks'] if s['primary'] and s['primary']['plate_code'] == bucket['plate_code']]
        unknown = any(s['limit_status'] in ('unknown_listing', 'unknown_price') or s['price_missing'] for s in rows)
        assert bucket['limit_up_count'] == (None if unknown else sum(s['limit_status'] == 'limit_up' for s in rows))
        assert bucket['limit_down_count'] == (None if unknown else sum(s['limit_status'] == 'limit_down' for s in rows))
    assert any(b['limit_up_count'] is None for b in view['buckets'])
    assert any(b['limit_up_count'] is not None for b in view['buckets'])
