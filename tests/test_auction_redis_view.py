"""Replay the C977 TD-derived candidate views through the product chain."""
import hashlib
import json
from pathlib import Path

import pytest
import redis

FIXTURES = Path(__file__).parent / 'fixtures' / 'redis_views'
TAGS = ('0920', '0924', '0925')


@pytest.mark.parametrize('scenario', ['normal', 'missing_key', 'incomplete', 'bad_json', 'digest_mismatch',
                                     'invalid_structure', 'invalid_field', 'empty_view', 'redis_error',
                                     'fallback_failure', 'missing_price', 'missing_final', 'after_cutoff', 'early_empty'])
def test_auction_redis_consumer(monkeypatch, tmp_path, scenario):
    from examples import fetch_auction_item as fetch
    from engine_core import auction_stock_facts as facts_module
    from engine_core.data_pipeline import td_sink
    from engine_core.data_pipeline.auction_redis_view import read_auction_view
    from engine_core.data_pipeline import storage

    pairs = {}
    for tag in TAGS:
        stem = FIXTURES / f'c977_2026-11-11_{tag}'
        pairs[tag] = [stem.with_suffix('.json').read_bytes(), Path(str(stem) + '.meta.json').read_bytes()]
    original = {tag: list(pair) for tag, pair in pairs.items()}
    if scenario in ('missing_key', 'fallback_failure', 'after_cutoff'):
        pairs['0924'][0] = None
    elif scenario == 'missing_final':
        pairs['0925'][0] = None
    elif scenario == 'incomplete':
        pairs['0924'][1] = b'{"status":"INCOMPLETE"}'
    elif scenario in ('bad_json', 'invalid_structure', 'invalid_field', 'empty_view', 'early_empty', 'missing_price'):
        target = '0925' if scenario in ('missing_price', 'empty_view') else '0924'
        data = json.loads(pairs[target][0])
        if scenario == 'bad_json':
            raw = b'{'
        elif scenario == 'invalid_structure':
            raw = b'[]'
        elif scenario in ('empty_view', 'early_empty'):
            raw = b'{}'
        else:
            row = next(iter(data.values()))
            if scenario == 'invalid_field':
                del row['amount_yuan']
            else:
                row['src'], row['price_milli'], row['change_bp'] = 'missing', None, None
            raw = json.dumps(data, separators=(',', ':')).encode()
        meta = json.loads(pairs[target][1]); meta['sha256'] = hashlib.sha256(raw).hexdigest()
        if scenario in ('empty_view', 'early_empty'):
            meta['rows'] = 0
        pairs[target] = [raw, json.dumps(meta).encode()]
    elif scenario == 'digest_mismatch':
        meta = json.loads(pairs['0924'][1]); meta['sha256'] = '0' * 64
        pairs['0924'][1] = json.dumps(meta).encode()

    class ReplayRedis:
        def __init__(self, data):
            self.data = data
        def mget(self, keys):
            if scenario == 'redis_error' and keys[0].endswith(':0924') and self.data is pairs:
                raise OSError('injected connection error')
            return self.data[keys[0].split(':')[-1]]

    monkeypatch.setattr(redis.Redis, 'from_url', lambda *a, **k: ReplayRedis(pairs))
    calls = []
    def fallback(day):
        calls.append(day)
        if scenario == 'fallback_failure':
            raise RuntimeError('TD unavailable')
        tag = '0925' if scenario in ('missing_final', 'empty_view') else '0924'
        rows = read_auction_view(ReplayRedis(original), day, tag)
        if scenario == 'after_cutoff':
            rows.append(dict(rows[0], ts=f'{day} 09:26:00'))
        return rows, []
    monkeypatch.setattr(fetch, '_td_source_rows', fallback)
    monkeypatch.setattr(td_sink, 'connect_td', lambda *a, **k: pytest.fail('unexpected TD connection'))
    if scenario == 'fallback_failure':
        with pytest.raises(RuntimeError, match='TD unavailable'):
            fetch._source_rows('2026-11-11')
        return
    snapshots, ticks = fetch._source_rows('2026-11-11')
    quality = snapshots.source_quality
    assert [quality[tag]['rows'] for tag in TAGS] == [4885, 0 if scenario == 'early_empty' else 5076, 5215]
    assert quality['0920']['source'] == 'redis_auction_full'
    assert quality['0925']['source'] == ('td_auction_snapshot_v2' if scenario in ('missing_final', 'empty_view') else 'redis_auction_full')
    if scenario not in ('missing_final', 'empty_view'):
        assert quality['0925']['src_counts']['indicative_bid_ask_mid'] == 216
    assert not ticks
    degraded = scenario not in ('normal', 'missing_price', 'early_empty')
    assert len(calls) == int(degraded)
    assert quality['0924']['source'] == ('td_auction_snapshot_v2' if degraded and scenario not in ('missing_final', 'empty_view') else 'redis_auction_full')
    if degraded:
        failed_tag = '0925' if scenario in ('missing_final', 'empty_view') else '0924'
        assert quality[failed_tag]['reason'] == {'bad_json': 'invalid_json', 'invalid_field': 'invalid_structure',
                                               'after_cutoff': 'missing_key', 'missing_final': 'missing_key',
                                               'redis_error': 'redis_error'}.get(scenario, scenario)

    captured = {}
    class FactsCatalog:
        def __init__(self, root): pass
        def commit(self, *args, **kwargs):
            captured['facts_manifest'] = args[3]
            return {'version': 'test'}
    monkeypatch.setattr(facts_module, 'ArtifactCatalog', FactsCatalog)
    original_store = facts_module.store_auction_stock_facts
    class HotStore:
        def set(self, key, value, ex):
            captured['hot'] = json.loads(value)
    def store_facts(facts, **kwargs):
        captured['facts'] = facts
        return original_store(facts, redis=HotStore(), **kwargs)
    monkeypatch.setattr(facts_module, 'store_auction_stock_facts', store_facts)
    result = fetch._products('2026-11-11', 'auction_stock_facts', tmp_path, snapshots, ticks)
    assert result['status'] == ('degraded' if degraded else 'ok')
    assert result['source_quality'] == quality
    assert captured['facts_manifest']['quality']['source_quality'] == quality
    assert captured['facts_manifest']['quality']['input_status'] == ('degraded' if degraded else 'ok')
    assert captured['hot']['source_quality'] == quality
    assert captured['hot']['input_status'] == ('degraded' if degraded else 'ok')
    assert result['rows'] == 5215
    final = json.loads(original['0925'][0])
    midpoint = next(symbol for symbol, row in final.items() if row['src'] == 'indicative_bid_ask_mid')
    for symbol in (list(final)[1], midpoint):
        raw = final[symbol]
        row = next(row for row in captured['facts']['rows'] if row['symbol'] == symbol)
        assert row['src'] == raw['src']
        assert row['price'] == raw['price_milli'] / 1000
        assert row['pct_change'] == raw['change_bp'] / 100
        assert row['auction_amount_yuan'] == raw['amount_yuan']
        assert row['bid_amount_yuan'] == raw['bid_amount_yuan']
        assert row['source_rank_amount'] == raw['rank_amount']
        assert row['seal']['status'] == 'UNKNOWN'
        assert row['snapshot_cutoff'].endswith('09:25:06+08:00')
    if scenario == 'missing_price':
        row = next(row for row in captured['facts']['rows'] if row['src'] == 'missing')
        assert row['price'] is None and row['pct_change'] is None
    if scenario in ('normal', 'missing_key'):
        class Catalog:
            def __init__(self, root): pass
            def read(self): return {}
            def commit(self, *args, **kwargs):
                captured['points'] = args
                return {'version': 'test'}
        monkeypatch.setattr(storage, 'ArtifactCatalog', Catalog)
        points = fetch._products('2026-11-11', 'auction_points', tmp_path, snapshots, ticks)
        assert points['status'] == ('degraded' if degraded else 'ok') and points['source_quality'] == quality
        assert captured['points'][3]['quality']['source_quality'] == quality
        assert captured['points'][3]['quality']['input_status'] == ('degraded' if degraded else 'ok')
        from engine_core import auction_panorama
        monkeypatch.setattr(auction_panorama, 'build_auction_panorama', lambda *a, **kw: {})
        def store_panorama(product, **kwargs):
            captured['panorama'] = product
            return {'version': 'test'}
        monkeypatch.setattr(auction_panorama, 'store_auction_panorama', store_panorama)
        panorama = fetch._products('2026-11-11', 'auction_panorama', tmp_path, snapshots, ticks)
        assert panorama['status'] == 'degraded'
        assert panorama['source_quality'] == quality
        assert captured['panorama']['input_quality']['auction_sources'] == quality
        assert captured['panorama']['input_quality']['auction_status'] == ('degraded' if degraded else 'ok')
