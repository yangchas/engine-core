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
    assert [r['limit_up_count'] for r in view['limit_up']] == sorted((r['limit_up_count'] for r in view['limit_up']), reverse=True)


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
    assert [row['name'] for row in down] == ['短剧', '游戏', '影视院线']
    assert [row['limit_down_count'] for row in down] == [4, 3, 2]
    assert next(row for row in build(min_limit_lift=0)['limit_down'] if row['name'] == '中报增长')
    assert view['limit_down_filtered_count'] == 2
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
