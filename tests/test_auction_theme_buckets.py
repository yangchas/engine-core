import json
from pathlib import Path

from engine_core.auction_theme_buckets import build_theme_buckets
from engine_core.data_pipeline.plate_stock_index import build_index


def test_real_excerpt_theme_counts_and_missing_price():
    data = json.loads((Path(__file__).parent / 'fixtures/real/c1332/excerpt.json').read_text())
    mapping = build_index(data['plate_members'])
    result = build_theme_buckets(data['auction_0925'] + data['auction_0924'], mapping)
    theme = next(row for row in result['themes'] if row['plate_code'] == '801003')
    assert theme['member_count'] == 45
    assert theme['auction_count'] == 45
    assert theme['amount_yuan'] == 1621416239
    assert theme['mean_change_bp'] == -178.6
    assert theme['median_change_bp'] == -218
    assert theme['amount_delta_yuan'] == 893901896
    assert result['market']['amount_yuan'] == 8029343768
    symbol = next(r['symbol'] for r in data['auction_0925'] if r['symbol'] in mapping and len(mapping[r['symbol']]) > 1)
    reduced = [r for r in data['auction_0925'] if r['symbol'] != symbol]
    missing = build_theme_buckets(reduced, mapping)
    codes = {p['plate_code'] for p in mapping[symbol] if p['plate_class'] == 'THEME'}
    assert len(codes) > 1
    assert all(r['missing_price_count'] >= 1 for r in missing['themes'] if r['plate_code'] in codes)


def test_real_excerpt_unknown_listing_and_unavailable_mapping():
    data = json.loads((Path(__file__).parent / 'fixtures/real/c1332/excerpt.json').read_text())
    mapping = build_index(data['plate_members'])
    result = build_theme_buckets(data['auction_0925'], mapping)
    theme = next(row for row in result['themes'] if row['plate_code'] == '801003')
    assert theme['unknown_limit_count'] == theme['auction_count'] == 45
    assert theme['distribution'] is None
    assert theme['limit_up_count'] is None
    assert theme['limit_down_count'] is None
    assert build_theme_buckets(data['auction_0925'], None)['status'] == 'unavailable'
