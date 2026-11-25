import hashlib
import json
from pathlib import Path

from engine_core.auction_active_buckets import build_active_bucket_view

REAL = Path(__file__).parent / 'fixtures/real'


def test_real_bucket_v2_and_v1_control():
    raw = json.loads((REAL / 'c1398/excerpt.json').read_text())
    data = json.loads((REAL / 'c1387/excerpt.json').read_text())
    facts = json.loads((REAL / 'c1397/excerpt.json').read_text())
    quotes = raw['auction']
    mapping = {s: row['plates'] for s, row in raw['index'].items()}
    pool = {r['symbol'] for r in facts['pool']['rows']}
    args = (quotes, mapping, pool, facts['reason_rows'])
    kw = dict(names=data['names'], plate_names=facts['plate_names'], caps=data['caps'])
    old = build_active_bucket_view(*args, **kw, rule='v1', top_amount=100, top_abs_change=100)
    assert hashlib.sha256(json.dumps(old, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == '72316cbcdbb2c047724f2f0cd91b00a1e5d1c65acbee10e9322ee973fa837e36'
    default = build_active_bucket_view(*args, **kw)
    assert default['bucket_version'] == 'bucket_v2'
    bare = build_active_bucket_view(quotes, mapping, [], [], **kw, top_amount=0)
    stocks = {r['symbol']: r for r in bare['stocks']}
    qualified = {r['symbol'] for r in quotes if r.get('price_milli') and r.get('change_bp') is not None and abs(r['change_bp']) >= 500 and r.get('amount_yuan') is not None and r['amount_yuan'] >= 5000000}
    assert stocks.keys() == qualified
    assert any(abs(r['change_bp']) >= 500 and r['amount_yuan'] < 5000000 for r in quotes if r.get('change_bp') is not None and r.get('amount_yuan') is not None)
    assert any(abs(r['change_bp']) < 500 and r['amount_yuan'] >= 5000000 for r in quotes if r.get('change_bp') is not None and r.get('amount_yuan') is not None)
    assert '300553' not in stocks
    captured = json.loads((REAL / 'c1414/300553.json').read_text())
    assert captured['auction'] == next(r for r in quotes if r['symbol'] == '300553')
    fact = captured['seal']
    assert fact['side'] == 'limit_down' and fact['seal_volume'] > 0
    sealed_symbols = {fact['symbol']} if fact['side'] in ('limit_up', 'limit_down') and fact['seal_volume'] > 0 else set()
    sealed = build_active_bucket_view(quotes, mapping, [], [], **kw, top_amount=0, sealed_symbols=sealed_symbols)
    assert next(r for r in sealed['stocks'] if r['symbol'] == '300553')['sources'] == ['s']


def test_real_digest_relay_and_render():
    from test_auction_stock_themes import inputs
    from engine_core.auction_stock_themes import assign_stock_themes
    from engine_core.auction_bucket_amount import add_bucket_amount_context
    from engine_core.auction_bucket_digest import build_anomaly_digest, render_digest_md
    base, mapping, names, counts, history = inputs()
    view = assign_stock_themes(base['stocks'], mapping, names, counts, history, limit_available=True)
    amounts = json.loads((REAL / 'c1407/auction.json').read_text())
    view = add_bucket_amount_context(view, amounts['2026-11-17'], {d: amounts[d] for d in ('2026-11-12', '2026-11-13', '2026-11-16')}, mapping)
    digest = build_anomaly_digest(view)
    assert all(b['active']['count'] >= 3 for b in digest['strong'] + digest['weak'])
    relay = digest['relay_market']
    assert relay['high'] + relay['flat'] + relay['low'] == relay['priced']
    assert relay['count'] == sum('y' in s['sources'] for s in view['stocks'])
    md = render_digest_md(digest)
    assert len(md.splitlines()) <= 45 and '市场' in md and '接力' in md
    assert not any(word in md for word in ('预测', '买入', '卖出'))
