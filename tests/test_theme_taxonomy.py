import hashlib
import json
from collections import defaultdict
from pathlib import Path


def inputs():
    data = json.loads((Path(__file__).parent / 'fixtures/real/c1356/inputs.json').read_text())
    members = defaultdict(set)
    for symbol, codes in data['index'].items():
        for code in codes:
            members[code].add(symbol)
    return data, members


def test_real_hierarchy_and_dangerous_cross_theme():
    from engine_core.theme_taxonomy import build_taxonomy
    data, members = inputs()
    tax = build_taxonomy(members, data['names'], data['boards'])
    pairs = {(p['child'], p['parent']) for p in tax['relations']}
    for parent, children in {'801001': ['801722', '801658', '801068', '801222'],
                             '801660': ['801003', '801704', '801780'],
                             '801159': ['801893', '801894', '801512']}.items():
        assert all((child, parent) in pairs for child in children)
    assert ('801960', '801005') in pairs
    child = tax['plates']['801960']
    assert child['primary_parent'] != '801005'
    assert '801005' in child['secondary_parents']
    candidates = [p for p in tax['relations'] if p['child'] == '801960']
    assert child['primary_parent'] == min(candidates, key=lambda p: (p['parent_members'], -p['containment'], p['parent']))['parent']
    for code in ('801740', '801741', '801595'):
        assert tax['plates'][code]['non_theme_reason'] and not tax['plates'][code]['is_broad']
    assert all(tax['plates'][c]['primary_parent'] is None for c in tax['orphans'])
    assert tax['orphans']


def test_real_determinism_and_thresholds():
    from engine_core.theme_taxonomy import build_taxonomy, STYLE_WORDS
    data, members = inputs()
    low = build_taxonomy(members, data['names'], data['boards'], containment=0.7)
    high = build_taxonomy(members, data['names'], data['boards'], containment=0.9)
    assert len(low['relations']) >= len(high['relations'])
    assert {'可转债', '破净', '高股息', '中字头', '超跌', '融资融券', '沪股通', '深股通', 'MSCI'} <= set(STYLE_WORDS)
    again = build_taxonomy(dict(reversed(list(members.items()))), data['names'], data['boards'], containment=0.9)
    digest = lambda x: hashlib.sha256(json.dumps(x, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert digest(high) == digest(again)


def test_real_cards_have_own_metrics_and_folded_groups():
    from engine_core.theme_taxonomy import build_taxonomy, build_theme_cards
    data, members = inputs()
    tax = build_taxonomy(members, data['names'], data['boards'])
    cards = build_theme_cards(tax, data['auction'], data['hot'], data['seal'], top_n=5,
                              listings=data['listings'], trading_dates=data['calendar'], trade_date='2026-11-17')
    chip = next(c for c in cards['broad_cards'] if c['plate_code'] == '801001')
    assert chip['member_count'] == len(members['801001'])
    assert chip['mean_change_bp'] is not None and chip['weighted_mean_change_bp'] is not None
    assert chip['market_amount_share'] > 0 and chip['taxonomy_parameters'] == tax['parameters']
    assert all(tax['plates'][c['plate_code']]['primary_parent'] == '801001' for c in chip['top_amount'] + chip['top_strength'])
    assert any(c['plate_code'] == '801595' for c in cards['non_theme'])
    assert {c['plate_code'] for c in cards['orphans']} == set(tax['orphans'])
