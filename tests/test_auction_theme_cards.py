import json
from pathlib import Path
from collections import Counter

REAL = Path(__file__).parent / 'fixtures' / 'real'

def data():
    a = json.loads((REAL / 'c1356/inputs.json').read_text())
    b = json.loads((REAL / 'c1367/inputs.json').read_text())
    caps = json.loads((REAL / 'c1365/wencai_excerpts.json').read_text())['rows']
    return a, b, {r['code']: {'float_cap_yuan': float(r['a股市值(不含限售股)[20261117]'])} for r in caps}

def build(caps=None, top_n=243):
    from engine_core.auction_theme_cards import build_auction_theme_view
    a,b,_ = data()
    return build_auction_theme_view(a['auction']+b['auction_0924'], a['index'], a['hot'], a['seal'], b['pool'], b['reason_rows'], names=a['names'], boards=a['boards'], listings=a['listings'], trading_dates=a['calendar'], trade_date='2026-11-17', previous_trade_date='2026-11-16', caps=caps, top_n_hot=top_n)

def test_real_vocabulary_metrics_hierarchy_and_exclusions():
    view=build()
    cards={x['plate_code']:x for x in view['plates']}
    assert {'801676','801346','801045','801001','801085','801660'} <= cards.keys()
    grid=cards['801346']
    assert (grid['auction_count'],grid['strong_count'],grid['middle_count'],grid['weak_count']) == (489,3,479,7)
    assert round(grid['mean_change_bp']/100,2) == -0.61
    assert {'801909','801595','801856','801862','801530','801162'} <= {x['plate_code'] for x in view['excluded']}
    assert not ({x['plate_code'] for x in view['excluded']} & cards.keys())
    assert {'801001','801085','801660'} <= {x['plate_code'] for x in view['cards']}
    chip=next(x for x in view['cards'] if x['plate_code']=='801001')
    children = {x['plate_code'] for x in chip['children']}
    assert children
    for code in ('801722', '801658'):
        if code in view['taxonomy']['plates'] and view['taxonomy']['plates'][code]['primary_parent'] == '801001':
            assert code in children
    assert 'overlap' in ' '.join(view['notes'])

def test_real_prior_reasons_and_capitalization_dangerous_control():
    a,b,caps=data(); view=build(caps)
    cards={x['plate_code']:x for x in view['plates']}
    counts=Counter(r['plate_code'] for r in b['pool'] if r.get('plate_code'))
    reason_only = {x['plate_code'] for x in build(top_n=0)['plates']}
    assert any(code in reason_only for code, _ in counts.most_common(20))
    for code,count in counts.most_common(20):
        if code in cards: assert cards[code]['previous_limit_reason_count'] >= count
    chip=cards['801001']
    assert [x['symbol'] for x in chip['anchors']][:3] == ['300308','300502','300394']
    assert chip['anchor_delta_bp'] is not None
    assert next(x for x in build()['plates'] if x['plate_code']=='801001')['anchors_status'] == 'caps_unavailable'

def test_reader_unavailable_and_html(monkeypatch):
    from engine_core.auction_theme_cards import read_and_build_theme_view, render_theme_cards_html
    class Missing:
        def scan_iter(self, **kwargs): return iter(())
    assert read_and_build_theme_view('2026-11-17', Missing())['status'] == 'unavailable'
    import engine_core.data_pipeline.auction_redis_view as auction_view
    import engine_core.data_pipeline.redis_day_view_read as day_view
    a, _, _ = data()
    hot = json.loads((REAL / 'c1356/inputs.json').read_text())['hot']
    class MissingMapping:
        def scan_iter(self, **kwargs): return iter(['hot'])
        def get(self, key):
            return json.dumps({'available_at': hot[0]['available_at'], 'trade_date':'2026-11-17', 'dataset':'hot_plates', 'row_count':len(hot), 'rows':hot})
    monkeypatch.setattr(auction_view, 'read_auction_view', lambda client, day, tag: a['auction'])
    monkeypatch.setattr(day_view, 'read_day_view', lambda *args, **kwargs: {'source':'td_fallback', 'rows':{}})
    result = read_and_build_theme_view('2026-11-17', MissingMapping())
    assert result['status'] == 'unavailable' and result['reason'].startswith('mapping:')
    html=render_theme_cards_html(build())
    assert '<details' in html and '芯片' in html and '非题材' in html
    assert '预测' not in html and '延续' not in html
