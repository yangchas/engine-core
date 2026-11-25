import json
from pathlib import Path

from engine_core.auction_active_buckets import build_active_bucket_view, render_active_bucket_html

ROOT = Path(__file__).parent / 'fixtures/real'
DATA = json.loads((ROOT / 'c1387/excerpt.json').read_text())
FACTS = json.loads((ROOT / 'c1397/excerpt.json').read_text())


def test_real_universe_and_unfiltered_membership():
    pool = FACTS['pool']['rows']
    yesterday = {r['symbol'] for r in pool}
    quotes = DATA['quotes']['0925']
    # The missing quote is a real pool stock omitted from this input window.
    absent = next(r['symbol'] for r in pool if r['symbol'] in {q['symbol'] for q in quotes})
    quotes = [q for q in quotes if q['symbol'] != absent]
    view = build_active_bucket_view(quotes, FACTS['index'], yesterday, FACTS['reason_rows'], names={}, plate_names=FACTS['plate_names'], top_amount=100, top_abs_change=100, rule='v1', sort_by='active_count')
    stocks = {r['symbol']: r for r in view['stocks']}
    assert yesterday <= stocks.keys()
    assert stocks[absent]['price_missing'] and stocks[absent]['sources'] == ['y']
    a = {r['symbol'] for r in sorted(quotes, key=lambda r: (-r['amount_yuan'], r['symbol']))[:100]}
    c = {r['symbol'] for r in sorted(quotes, key=lambda r: (-abs(r['change_bp']), r['symbol']))[:100]}
    assert all(('a' in stocks[s]['sources']) == (s in a) and ('c' in stocks[s]['sources']) == (s in c) for s in stocks)
    assert any(len(stocks[s]['sources']) > 1 for s in stocks)
    sample = next(s for s in stocks if len(FACTS['index'].get(s, [])) > 1)
    assert set(stocks[sample]['plate_codes']) == {str(x['plate_code']) for x in FACTS['index'][sample]}
    buckets = view['buckets']
    assert buckets == sorted(buckets, key=lambda r: (-r['active_count'], -(r['amount_yuan'] or 0), r['plate_code']))
    assert all(r['leaders_status'] == 'caps_unavailable' for r in buckets)
    styles = [r for r in buckets if r['name'] in ('低价股', '国有企业')]
    assert {r['name'] for r in styles} == {'低价股', '国有企业'}
    assert all(r['non_theme_reason'] for r in styles)
    assert '预测' not in render_active_bucket_html(view)


def test_real_price_limit_and_missing_contrast():
    quotes = DATA['quotes']['0925']
    view = build_active_bucket_view(quotes, FACTS['index'], [], [], names={}, plate_names=FACTS['plate_names'], top_amount=100, top_abs_change=100, trade_date='2026-11-17', listings=DATA['listings'], trading_dates=DATA['calendar'])
    assert any(r['limit_up_count'] or r['limit_down_count'] for r in view['buckets'])
    bare = build_active_bucket_view(quotes, FACTS['index'], [], [], names={}, plate_names=FACTS['plate_names'], top_amount=100, top_abs_change=100, rule='v1')
    assert all(r['limit_up_count'] is None and r['limit_down_count'] is None for r in bare['buckets'])


def test_bucket_v1_real_full_mapping():
    full = json.loads((ROOT / 'c1398/excerpt.json').read_text())
    args = (full['auction'], {s: row['plates'] for s, row in full['index'].items()}, {r['symbol'] for r in FACTS['pool']['rows']}, FACTS['reason_rows'])
    kwargs = dict(names=DATA['names'], plate_names=FACTS['plate_names'], caps=DATA['caps'])
    view = build_active_bucket_view(*args, **kwargs, rule='v1')
    ranks = {row['name']: i for i, row in enumerate(view['buckets'])}
    assert all(ranks['文化传媒'] < ranks[name] for name in ('中报增长', '三季报增长', '人工智能', '芯片'))
    assert view['bucket_version'] == 'bucket_v1'
    old = build_active_bucket_view(*args, **kwargs, sort_by='active_count', rule='v1')['buckets']
    assert old == sorted(old, key=lambda r: (-r['active_count'], -(r['amount_yuan'] if r['amount_yuan'] is not None else -1), r['plate_code']))
    bucket = next(r for r in view['buckets'] if len(r['leaders']) == 3)
    assert bucket['leaders_status'] == 'caps_ok'
    assert all(set(r) == {'symbol', 'name', 'change_bp', 'amount_yuan', 'float_cap_yuan'} for r in bucket['leaders'])
    assert [r['float_cap_yuan'] for r in bucket['leaders']] == sorted((r['float_cap_yuan'] for r in bucket['leaders']), reverse=True)
    html = render_active_bucket_html(view).split('<h2>个股</h2>')[0]
    assert '<th>score</th>' in html and '<th>plate_code</th>' not in html
    assert 'plate_codes' not in html and '<th>龙头</th>' in html
