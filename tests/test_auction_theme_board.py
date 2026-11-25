import copy
import json
from pathlib import Path

from engine_core.data_pipeline.auction_theme_board import assemble

REAL = Path(__file__).parent / 'fixtures/real'
DAY = '2026-11-17'
ASOF = DAY + 'T09:26:20+08:00'


def inputs():
    raw = json.loads((REAL / 'c1398/excerpt.json').read_text())
    data = json.loads((REAL / 'c1387/excerpt.json').read_text())
    facts = json.loads((REAL / 'c1397/excerpt.json').read_text())
    amounts = json.loads((REAL / 'c1407/auction.json').read_text())
    return dict(auction_0925={'rows': raw['auction'], 'meta': {'status': 'COMPLETE', 'sha256': 'f80d2f8457d967e0666ea8aefb0752e91713c167c9f69292d1b93809b2fac48e'}},
        auction_0924_meta={'rows': 5053}, prior_0925_by_date={d: amounts[d] for d in ('2026-11-12', '2026-11-13', '2026-11-16')},
        plate_map={'rows': {s: row['plates'] for s, row in raw['index'].items()}, 'available_at': DAY+'T09:05:00+08:00'},
        caps={'rows': data['caps'], 'available_at': DAY+'T17:49:59+08:00', 'trade_date': DAY, 'replay': True},
        yesterday_pool=facts['pool']['rows'], yesterday_reasons=facts['reason_rows'], reason_history={},
        seal=None, names=data['names'], listings=data['listings'], plate_names=facts['plate_names'], trading_dates=data['calendar'])


def test_gates_and_dangerous_controls():
    base = inputs()
    cases = [('asof_too_early', base, DAY+'T09:25:19+08:00'),
             ('auction_view_incomplete', {**base, 'auction_0924_meta': {**base['auction_0924_meta'], 'rows': 6000}}, ASOF),
             ('auction_view_invalid', {**base, 'auction_0925': {**base['auction_0925'], 'meta': {'status': 'FAILED', 'sha256': ''}}}, ASOF),
             ('plate_map_not_visible', {**base, 'plate_map': {**base['plate_map'], 'available_at': DAY+'T09:27:00+08:00'}}, ASOF),
             ('plate_map_not_visible', {**base, 'plate_map': {**base['plate_map'], 'available_at': None}}, ASOF),
             ('yesterday_pool_missing', {**base, 'yesterday_pool': None}, ASOF)]
    for reason, data, asof in cases:
        result = assemble(DAY, asof, data)
        assert result['status'] == 'failed' and result['reason'] == reason and 'fields' not in result
    assert assemble(DAY, ASOF, base)['status'] == 'ok'


def test_degradation_and_determinism():
    base = inputs()
    base.update(prior_0925_by_date={}, caps=None, seal=None)
    one = assemble(DAY, ASOF, base)
    two = assemble(DAY, ASOF, copy.deepcopy(base))
    assert one == two and one['status'] == 'ok'
    assert one['summary']['caps_unavailable'] and one['summary']['sealed_missing']
    assert one['summary']['arm_counts']['s'] == 0
    assert one['summary']['market_ratio'] is None
    assert '前几日不足' in one['fields']['digest_md'].splitlines()[0]


def test_visibility_and_replay_controls():
    base = inputs()
    replay = assemble(DAY, ASOF, base)
    assert replay['summary']['replay_non_pit'] and not replay['summary']['caps_unavailable']
    base['caps']['replay'] = False
    base['seal'] = {'sealed_symbols': ['300553'], 'available_at': DAY+'T09:27:00+08:00'}
    result = assemble(DAY, ASOF, base)
    assert result['summary']['caps_unavailable'] and result['summary']['sealed_missing']
    assert result['summary']['arm_counts']['s'] == 0
