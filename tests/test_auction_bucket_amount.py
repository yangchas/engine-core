import json
from pathlib import Path
import pytest
from engine_core.auction_bucket_amount import add_bucket_amount_context, render_bucket_amount_html, amount_status

DATA = json.loads((Path(__file__).parent / 'fixtures/real/c1407/auction.json').read_text())

def test_fixed_members_and_market():
    today = DATA['2026-11-17']
    prior = {d: DATA[d] for d in ('2026-11-12', '2026-11-13', '2026-11-16')}
    view = {'stocks': [{'symbol': r['symbol'], 'primary': {'plate_code': 'p'}} for r in today], 'buckets': [{'plate_code': 'p', 'name': 'test', 'mean_change_bp': 50}]}
    result = add_bucket_amount_context(view, today, prior, {r['symbol']: ['p'] for r in today})
    bucket = result['buckets'][0]['active']
    assert bucket['count'] == 2
    assert bucket['today_yuan'] == 6912700 + 3184400
    assert bucket['prior_yuan']['2026-11-13'] == 4891000 + 1202000
    assert bucket['ratio'] == pytest.approx((6912700 + 3184400) / ((8153800+1549700+4891000+1202000+10737000+2640080)/3))
    assert result['market']['ratio'] == pytest.approx(bucket['ratio'])
    assert bucket['relative_ratio'] == pytest.approx(1)
    short = add_bucket_amount_context(view, today, {'2026-11-16': prior['2026-11-16']}, {})
    assert short['buckets'][0]['active']['ratio'] is None
    assert short['buckets'][0]['active']['status'] == 'days_insufficient'
    markup = render_bucket_amount_html(result)
    assert all(x in markup for x in ('今日竞价额', '前几日均值', '放量倍数', '相对放量', 'r_bp', '状态'))
    assert all(x not in markup for x in ('预测', '买入', '卖出'))

def test_status_boundaries():
    assert amount_status(1.2, 50) == '放量偏强'
    assert amount_status(0.8, -50) == '缩量偏弱'
    assert amount_status(1.2, -50) == '放量偏弱'
    assert amount_status(0.8, 50) == '缩量偏强'


def test_real_board_and_small_bucket_ranking():
    from test_auction_stock_themes import inputs
    from engine_core.auction_stock_themes import assign_stock_themes
    from engine_core.auction_bucket_amount import rank_relative_amount, render_bucket_board_md
    base, mapping, names, counts, history = inputs()
    themes = assign_stock_themes(base['stocks'], mapping, names, counts, history)
    prior = {d: DATA[d] for d in ('2026-11-12', '2026-11-13', '2026-11-16')}
    view = add_bucket_amount_context(themes, DATA['2026-11-17'], prior, mapping)
    assert all(b['active']['count'] >= 3 for b in rank_relative_amount(view))
    assert not rank_relative_amount(view)
    board = render_bucket_board_md(view)
    assert all(s in board for s in ('市场', '主题材', '活跃只数', '昨涨停', '今日竞价额', '前几日均值', '放量倍数', '相对放量', '状态', '平均涨幅', '去掉最高1只后', '涨停/跌停', '偏移', '龙头', '%'))
