import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

import pytest
from engine_core.auction_plate_view import build_auction_plate_view, render_auction_plate_html

DATA = json.loads((Path(__file__).parent / 'fixtures/real/c1387/excerpt.json').read_text())
MAPPING = defaultdict(list)
for row in DATA['members']:
    MAPPING[row['symbol']].append(row)


def build(tag='0925', **kwargs):
    return build_auction_plate_view(DATA['quotes'][tag], MAPPING, tag=tag,
        names=DATA['names'], listings=DATA['listings'], trading_dates=DATA['calendar'],
        trade_date='2026-11-17', top_n=50, **kwargs)


def test_reference_values_and_dangerous_limit_contrast():
    view = build()
    ref = DATA['reference']['0925']
    actual = {r['plate_code']: r for r in view['plates']}
    assert {'801027', '801045', '801095', '801206', '801346', '801572', '801676', '801786', '801803', '801695'} <= set(actual)
    correction = ref['market_bp'] - view['market_bp']
    for row in ref['rows']:
        assert actual[row['code']]['r_bp'] == pytest.approx(row['r_bp'] + correction, abs=1e-6)
    mid = actual['801572']
    assert abs(mid['r_bp']) < 20
    assert view['rising'][0]['plate_code'] != '801572'
    assert view['falling'][0]['plate_code'] != '801572'
    assert actual['801786']['limit_down_count'] == 2
    assert '801786' in [r['plate_code'] for r in view['limit_down']]
    assert '801786' not in [r['plate_code'] for r in view['limit_up']]
    assert [r['lift_limit_up'] for r in view['limit_up']] == sorted((r['lift_limit_up'] for r in view['limit_up']), reverse=True)


def test_optional_data_and_anchors():
    bare = build()
    enriched = build(hot_rows=DATA['hot'], caps=DATA['caps'], previous_rows=DATA['quotes']['0924'])
    a = {r['plate_code']: r for r in bare['plates']}
    b = {r['plate_code']: r for r in enriched['plates']}
    for code in a:
        assert a[code]['r_bp'] == b[code]['r_bp']
        assert a[code]['kaipan_rank'] is None
        assert a[code]['anchors_status'] == 'caps_unavailable'
    for hot in DATA['hot']:
        row = b[hot['plate_code']]
        assert (row['kaipan_rank'], row['kaipan_strength'], row['kaipan_change_pct']) == (hot['rank'], hot['strength'], hot['change_pct'])
    row = b['801206']; symbols = {x['symbol'] for x in DATA['members'] if x['plate_code'] == '801206'}
    quoted = {x['symbol']: x for x in DATA['quotes']['0925'] if x['price_milli'] is not None and x['change_bp'] is not None}
    expected = sorted((s for s in symbols if s in quoted and DATA['caps'].get(s, {}).get('float_cap_yuan') is not None), key=lambda s: (-DATA['caps'][s]['float_cap_yuan'], s))[:3]
    assert [x['symbol'] for x in row['anchors']] == expected
    assert row['anchor_delta_bp'] == pytest.approx(mean(quoted[s]['change_bp'] for s in expected) - row['mean_bp'])
    assert row['amount_delta_yuan'] is not None
    assert all(r['amount_delta_yuan'] is None for r in build(previous_rows=[])['plates'])


def test_preview_filter_and_html():
    view = build('0924')
    assert '指示价' in view['price_note']
    hidden = build(hide_non_theme=True)
    assert all(not r['non_theme_reason'] for key in ('rising','falling','limit_up','limit_down') for r in hidden[key])
    assert any(r['plate_code'] == '801572' and r['non_theme_reason'] for r in hidden['plates'])
    html = render_auction_plate_html(view)
    for word in ('涨幅榜','涨停榜','跌幅榜','跌停榜','板块相对大盘平均涨幅','涨停只数'):
        assert word in html
    assert '预测' not in html and '看好' not in html


def test_limit_lift_filter_on_real_excerpt():
    view = build()
    down = view['limit_down']
    assert '中报增长' not in [row['name'] for row in down]
    assert [row['name'] for row in down] == ['影视院线', '短剧', '游戏']
    assert [row['limit_down_count'] for row in down] == [2, 4, 3]
    assert next(row for row in build(min_limit_lift=0, hide_non_theme=False)['limit_down'] if row['name'] == '中报增长')
    assert view['limit_down_filtered_count'] == 1
    assert view['market_limit_down_ratio'] == pytest.approx(8 / 3386)
    for side in ('up', 'down'):
        for row in view['limit_' + side]:
            assert row['lift_limit_' + side] >= 2
            assert row['limit_' + side + '_ratio'] / view['market_limit_' + side + '_ratio'] == pytest.approx(row['lift_limit_' + side])
    assert build(min_limit_lift=10000)['limit_up'] == []
    without_limits = build_auction_plate_view(DATA['quotes']['0925'], MAPPING, tag='0925')
    assert without_limits['market_limit_up_ratio'] is None
    assert without_limits['limit_up'] == []
    assert all(row['lift_limit_up'] is None for row in without_limits['plates'])
    html = render_auction_plate_html(view)
    assert '占比' in html and '倍数' in html


def test_default_hides_marked_real_plates_and_opt_out_restores_them():
    view = build()
    marked = [r for r in view['plates'] if r['name'] in {'低价股', '股权转让', '国有企业', '中报增长'}]
    assert marked and all(r['non_theme_reason'] for r in marked)
    for key in ('rising', 'falling', 'limit_up', 'limit_down'):
        assert all(not r['non_theme_reason'] for r in view[key])
    unhidden = build(hide_non_theme=False, min_limit_lift=0)
    assert any(r['name'] == '中报增长' for r in unhidden['rising'])
    assert any(r['name'] == '中报增长' for r in unhidden['limit_down'])


def test_limit_count_lift_order_and_insufficient_market_note_real_excerpt():
    view = build(hide_non_theme=False, min_limit_lift=0)
    for side in ('up', 'down'):
        rows = view['limit_' + side]
        assert all(r['limit_' + side + '_count'] >= 2 for r in rows)
        assert rows == sorted(rows, key=lambda r: (-r['lift_limit_' + side], -r['limit_' + side + '_count'], -r['r_bp'] if side == 'up' else r['r_bp'], r['plate_code']))
    assert next(r for r in view['plates'] if r['name'] == '智能电网')['limit_up_count'] == 1
    assert all(r['name'] != '智能电网' for r in view['limit_up'])
    assert view['limit_down'][0]['name'] == '影视院线'
    from engine_core.data_pipeline.auction_seal_facts import classify_limit
    quotes = DATA['quotes']['0925']
    limit_symbols = [r['symbol'] for r in quotes if r.get('price_milli') is not None and classify_limit('2026-11-17', r['symbol'], r, DATA['listings'].get(r['symbol']), DATA['calendar']) == 'limit_up']
    assert len(limit_symbols) > 1
    one_limit = [r for r in quotes if r['symbol'] not in limit_symbols[1:]]
    single = build_auction_plate_view(one_limit, MAPPING, tag='0925', names=DATA['names'], listings=DATA['listings'], trading_dates=DATA['calendar'], trade_date='2026-11-17')
    assert single['limit_up'] == [] and '样本不足' in single['limit_up_note']
    insufficient = build(min_limit_count=len(DATA['quotes']['0925']) + 1)
    assert insufficient['limit_up'] == [] and '样本不足' in insufficient['limit_up_note']
    assert insufficient['limit_down'] == [] and '样本不足' in insufficient['limit_down_note']
    assert insufficient['limit_up_note'] in render_auction_plate_html(insufficient)
    assert insufficient['limit_down_note'] in render_auction_plate_html(insufficient)
