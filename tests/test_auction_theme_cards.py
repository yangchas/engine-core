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
    return build_auction_theme_view(a['auction']+b['auction_0924'], a['index'], a['hot'], a['seal'], b['pool'], b['reason_rows'], names=a['names'], boards=a['boards'], listings=a['listings'], trading_dates=a['calendar'], trade_date='2026-11-17', previous_trade_date='2026-11-16', caps=caps, top_n_hot=top_n, min_active=0, sort_by='active_count')

def test_real_vocabulary_metrics_hierarchy_and_exclusions():
    view=build()
    cards={x['plate_code']:x for x in view['plates']}
    assert {'801676','801346','801045','801001','801085','801660'} <= cards.keys()
    grid=cards['801346']
    assert (grid['auction_count'],grid['strong_count'],grid['middle_count'],grid['weak_count']) == (489,3,479,7)
    assert round(grid['mean_change_bp']/100,2) == -0.61
    assert {'801909','801595','801856','801862','801530','801162'} <= {x['plate_code'] for x in view['excluded']}
    assert not ({x['plate_code'] for x in view['excluded']} & cards.keys())
    assert view['cards'] == sorted(view['cards'], key=lambda c: (-c['active_count'], c['amount_yuan'] is None, -(c['amount_yuan'] or 0), c['plate_code']))
    assert all(not c['children'] for c in view['cards'])
    assert 'overlap' in ' '.join(view['notes'])

def test_real_prior_reasons_and_capitalization_dangerous_control():
    a,b,caps=data(); view=build(caps)
    cards={x['plate_code']:x for x in view['plates']}
    counts=Counter(r['plate_code'] for r in b['pool'] if r.get('plate_code'))
    assert all(x['active_count'] >= 2 or x['rank'] is not None for x in build(top_n=0)['plates'])
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

def test_real_hot_order_flat_filter_and_active_dashboard():
    from engine_core.auction_theme_cards import render_theme_cards_html
    a, b, _ = data()
    view = build(top_n=40)
    assert {'地产链', '智能电网', '医药', '银行', '机场航空'} <= {c['name'] for c in view['cards']}
    assert all(not c['children'] for c in view['cards'])
    assert any(c['plate_code'] == '801909' and c['rank'] == 4 for c in view['excluded'])
    assert not {'中报增长', '三季报增长', '国有企业', '低价股', '股权转让'} & {c['name'] for c in view['cards']}
    grid = next(c for c in view['cards'] if c['name'] == '智能电网')
    assert (grid['auction_count'], grid['strong_count'], grid['middle_count'], grid['weak_count'], round(grid['mean_change_bp']/100, 2)) == (489, 3, 479, 7, -0.61)
    active = {r['symbol']: r for r in view['active_stocks']}
    assert {r['symbol'] for r in a['seal'] if r['side'] in ('limit_up', 'limit_down')} <= active.keys()
    assert all('primary_theme' in r and 'theme_source' in r and 'hot_plates' in r for r in active.values())
    html = render_theme_cards_html(view)
    assert '活跃个股' in html and '预测' not in html and '延续' not in html


def test_real_name_filter_and_active_attribution():
    from engine_core.auction_theme_cards import non_theme_reason
    a, b, _ = data()
    assert len(a['names']) > 400
    for name in ('可转债', '北交所', '破净股概念', '高股息精选', '中字头', '超跌', '融资融券', '沪股通', '深股通', 'MSCI', '中报增长', '三季报增长', '年报增长', '业绩增长', '云南省', '天津自贸区', '西部大开发', '京'):
        assert non_theme_reason(name), name
    for name in ('芯片', '人工智能', '通信', '算力', '医药', '智能电网', '机器人概念', '光模块'):
        assert non_theme_reason(name) is None, name
    view = build(top_n=243)
    cards = {c['plate_code']: c for c in view['cards']}
    active = {r['symbol']: r for r in view['active_stocks']}
    assert len([r for r in a['seal'] if r['side'] == 'limit_up']) == 3
    assert len([r for r in a['seal'] if r['side'] == 'limit_down']) == 17
    members = {code: set() for code in cards}
    for symbol, plates in a['index'].items():
        for plate in plates:
            code = str(plate.get('plate_code') if isinstance(plate, dict) else plate)
            if code in members:
                members[code].add(symbol)
    def order(code):
        return (-len(members[code] & active.keys()), len(members[code]), cards[code]['rank'] if cards[code]['rank'] is not None else float('inf'), code)
    pool_symbols = {r['symbol'] for r in b['pool']}
    for symbol, stock in active.items():
        candidates = {str(code) for r in b['pool'] + b['reason_rows'] if r.get('symbol') == symbol and symbol in pool_symbols and (r in b['pool'] or (r.get('event_date') or r.get('date')) == '2026-11-16') for code in [r.get('main_reason_plate_code'), r.get('plate_code')] if code and str(code) in cards and symbol in members[str(code)]}
        fallback = [c for c in cards if symbol in members[c] and len(members[c]) <= 400]
        expected = min(candidates or fallback, key=order) if candidates or fallback else None
        assert (stock['primary_theme'] or {}).get('plate_code') == expected
        assert stock['theme_source'] == ('limit_reason' if candidates else 'active_membership' if fallback else 'UNKNOWN')



def test_real_geography_filter_does_not_use_substring_matching():
    from engine_core.auction_theme_cards import non_theme_reason
    names = json.loads((REAL / 'c1374/plate_names.json').read_text())
    assert len(names) >= 700
    assert non_theme_reason('智慧城市') is None
    assert non_theme_reason('节能环保') is None
    for name in ('上海市', '北京市', '云南省', '西藏自治区', '天津自贸区', '西部大开发', '新疆', '浙江'):
        assert non_theme_reason(name) == 'geography_name', name
    filtered = [name for name in names if non_theme_reason(name) == 'geography_name']
    assert '智慧城市' not in filtered
    assert '节能环保' not in filtered
    assert set(('上海市', '北京市', '云南省', '西藏自治区', '天津自贸区', '西部大开发', '新疆', '浙江')) <= set(filtered)


def test_real_c1377_v3_vocabulary_and_display():
    from engine_core.auction_theme_cards import build_auction_theme_view, render_theme_cards_html
    a, b, _ = data()
    expected = json.loads((REAL / 'c1378/expected.json').read_text())
    def run(**kwargs):
        return build_auction_theme_view(a['auction'] + b['auction_0924'], a['index'], a['hot'], a['seal'], b['pool'], b['reason_rows'], names=a['names'], listings=a['listings'], trading_dates=a['calendar'], trade_date='2026-11-17', previous_trade_date='2026-11-16', min_active=0, sort_by='active_count', **kwargs)
    view = run()
    stocks = {s['symbol']: s for s in view['active_stocks']}
    assert len(stocks) == 54
    assert all(s['primary_theme'] for s in stocks.values())
    assert len(view['cards']) == 163
    assert len(view['display_cards']) == 50 and len(view['more_cards']) == 113
    assert view['cards'] == sorted(view['cards'], key=lambda c: (-c['active_count'], c['amount_yuan'] is None, -(c['amount_yuan'] or 0), c['plate_code']))
    for symbol, oracle in expected['V3 k=2']['stocks'].items():
        assert stocks[symbol]['primary_theme']['name'] == oracle['theme']
        assert stocks[symbol]['theme_source'] == oracle['source']
    assert not {'中报增长', '三季报增长', '国有企业', '低价股', '股权转让', '数字经济'} & {c['name'] for c in view['cards']}
    for name, metrics in expected['C1354 metrics'].items():
        card = next(c for c in view['cards'] if c['name'] == name)
        assert {key: card[key] for key in metrics} == metrics
    k3 = run(k_active=3)
    assert sum(bool(s['hot_plates']) for s in k3['active_stocks']) == expected['V3 k=3']['covered'] == 53
    assert sum(s['primary_theme'] is not None for s in k3['active_stocks']) == 52
    html = render_theme_cards_html(view)
    assert '更多' in html and '预测' not in html and '延续' not in html


def test_real_enrichment_and_cap_attribution():
    import pytest
    from engine_core.auction_theme_cards import build_auction_theme_view, render_theme_cards_html, _hypergeom_tail
    a, b, _ = data()
    caps = json.loads((REAL / 'c1380/caps.json').read_text())
    def run(**kwargs):
        return build_auction_theme_view(a['auction'] + b['auction_0924'], a['index'], a['hot'], a['seal'], b['pool'], b['reason_rows'], names=a['names'], listings=a['listings'], trading_dates=a['calendar'], trade_date='2026-11-17', previous_trade_date='2026-11-16', caps=caps, **kwargs)
    view = run()
    cards = {c['name']: c for c in view['cards']}
    for name, count, total in [('机器人概念', 13, 1269), ('游戏', 4, 95), ('文化传媒', 7, 242)]:
        card = cards[name]
        assert (card['active_count'], card['auction_count']) == (count, total)
        assert card['expected_active'] == pytest.approx(total * 54 / 5214)
        assert card['lift'] == pytest.approx(count / card['expected_active'])
    order = [c['name'] for c in view['cards']]
    assert order.index('文化传媒') < order.index('字节概念') < order.index('游戏') < order.index('机器人概念')
    lift_cards = run(sort_by='lift')['cards']
    assert lift_cards == sorted(lift_cards, key=lambda c: (c['lift'] is None, -(c['lift'] or 0), -c['active_count'], c['amount_yuan'] is None, -(c['amount_yuan'] or 0), c['plate_code']))
    assert all(c['active_count'] >= 2 for c in view['cards'])
    assert _hypergeom_tail(10, 3, 4, 2) == pytest.approx(1/3)
    legacy = run(sort_by='active_count', min_active=0)['cards']
    assert any(c['active_count'] < 2 for c in legacy)
    assert legacy == sorted(legacy, key=lambda c: (-c['active_count'], c['amount_yuan'] is None, -(c['amount_yuan'] or 0), c['plate_code']))
    stocks = {s['symbol']: s for s in view['active_stocks']}
    for symbol in ('688256', '300308', '300502'):
        assert stocks[symbol]['float_cap_yuan'] == caps[symbol]['float_cap_yuan']
        assert 'anchor_cap_rank' in stocks[symbol]
    assert stocks['688256']['primary_theme']['name'] == '阿里巴巴概念'
    assert stocks['300308']['primary_theme']['name'] == 'CPO/MPO'
    assert stocks['300502']['primary_theme']['name'] == 'CPO/MPO'
    assert [x['name'] for x in stocks['688256']['anchor_themes']] == ['操作系统', '数字芯片', '东数西算']
    assert 'GPU' not in [x['name'] for x in stocks['688256']['anchor_themes']]
    assert '光芯片' in [x['name'] for x in stocks['300308']['anchor_themes']]
    assert '光模块' in [x['name'] for x in stocks['300502']['anchor_themes']]
    for card in view['cards']:
        assert 0 <= card['p_value'] <= 1

    html = render_theme_cards_html(view)
    assert all(word in html for word in ('期望数', '倍数', '龙头地位板块', '涨跌幅(bp)', '流通市值(亿元)', 'anchor_delta_bp'))
    assert '预测' not in html and '延续' not in html
