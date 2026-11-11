"""Regression against the captured Kaipan pool; injected stores replay that data."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine_core.data_pipeline import limit_pool_hot_view as hot
from engine_core.data_pipeline.limit_pool_td import MemoryPoolStore
from engine_core.guardian.limit_pool_d1 import PoolDeps, run_pool_tier


class RedisSpy:
    def __init__(self):
        self.writes = {}
        self.deadlines = {}
        self.fail = False

    def get(self, key):
        if self.fail:
            raise ConnectionError('redis unavailable')
        return self.writes.get(key)

    def pipeline(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def watch(self, key):
        pass

    def unwatch(self):
        pass

    def multi(self):
        pass

    def ttl(self, key):
        deadline = self.deadlines.get(key)
        return deadline - int(datetime(2026, 10, 27, 1, tzinfo=timezone.utc).timestamp()) if deadline else -1

    def set(self, key, value, *, exat):
        if self.fail:
            raise ConnectionError('redis unavailable')
        self.writes[key] = value
        self.deadlines[key] = exat
        return True

    def execute(self):
        return [True]


@pytest.mark.parametrize('source,wanted,scenario', [
    ('wencai', 'wencai', 'hit'), ('wencai', 'td', 'hit'),
    ('td', 'td', 'hit'), ('wencai', 'wencai', 'partial'),
    ('wencai', 'kaipan', 'upgrade'), ('kaipan', 'kaipan', 'dry_ttl'),
    ('wencai', 'kaipan', 'dry_upgrade'), ('wencai', 'wencai', 'dry_missing'),
])
def test_pool_degraded_hit_and_dry_run(source, wanted, scenario, tmp_path, monkeypatch):
    from engine_core.data_pipeline import limit_pool as core

    day = '2026-10-26'
    clock = lambda: datetime(2026, 10, 29, 1, tzinfo=timezone.utc)
    fixture_root = Path(__file__).parent / 'fixtures'
    if source == 'wencai':
        doc = json.loads((fixture_root / 'real/wencai_limit_up_pool_2026-10-26_0539.json').read_text())
        part = core.build_wencai(day, doc['rows'], reported_total=doc['reported_total'])
    elif source == 'td':
        doc = json.loads((fixture_root / 'limit_pool/td_bars_20261026.json').read_text())
        bars = {symbol: [tuple(bar) for bar in series] for symbol, series in doc['bars'].items()}
        part = core.build_td_derived(day, bars, names=doc['names'], sessions=doc['sessions'], st_source='fixture')
    else:
        pages = json.loads((fixture_root / 'limit_pool/kaipan_pool_20261026.json').read_text())['pages']
        part = core.build_kaipan(day, {int(k): v for k, v in pages.items()})
    manifest = {**part.manifest, 'version': 'recorded-source', 'available_at': clock().isoformat()}
    if scenario == 'partial':
        manifest['quality'] = {**manifest['quality'], 'status': 'PARTIAL', 'partial': True}
        manifest['business_status'] = 'PARTIAL'
    redis = RedisSpy()
    redis.ttl = lambda key: redis.deadlines.get(key, 0) - int(clock().timestamp())
    key = f'core:view:limit_up_pool:{day}'
    value = hot.envelope(day, part.rows, manifest)
    if scenario != 'dry_missing':
        redis.writes[key] = json.dumps(value)
        redis.deadlines[key] = hot.deadline_for(day, Path.home() / 'core-data')
    if scenario == 'dry_ttl':
        redis.deadlines[key] -= 60
    before = dict(redis.writes), dict(redis.deadlines)
    calls = []
    def no_call(name):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError('unexpected ' + name)
        return fail
    deps = PoolDeps(kaipan=no_call('kaipan'), wencai=no_call('wencai'), td_bars=no_call('td_bars'),
                    td_store=no_call('td_store'), redis_client=redis,
                    calendar_root=Path.home() / 'core-data', now_utc=clock)
    monkeypatch.setattr(redis, 'set', no_call('set'))
    if scenario == 'upgrade':
        pages = json.loads((fixture_root / 'limit_pool/kaipan_pool_20261026.json').read_text())['pages']
        def fetch(*args):
            calls.append('kaipan')
            return {'pages': {int(k): v for k, v in pages.items()}, 'request_count': 6}
        deps.kaipan = fetch
        monkeypatch.undo()
    code, body = run_pool_tier({'data_date': day, 'args': {'source': wanted}}, tmp_path,
                               dry_run=scenario.startswith('dry_'), deps=deps)
    if scenario == 'upgrade':
        assert code == 0 and calls == ['kaipan'] and json.loads(redis.writes[key])['provider'] == core.KAIPAN
    elif scenario.startswith('dry_'):
        assert code in (0, 20) and not calls and (redis.writes, redis.deadlines) == before
    else:
        assert code == 20 and body['error_code'] == 'ALREADY_SATISFIED' and not calls
        assert (redis.writes, redis.deadlines) == before
        rows, meta = hot.read(day, clock().isoformat(), redis_client=redis)
        assert rows == part.rows and meta['status'] == ('PARTIAL' if scenario == 'partial' else 'DEGRADED')
        assert meta['business_status'] == manifest['business_status'] and meta['quality'] == manifest['quality']


@pytest.mark.parametrize('day', ['2026-10-26'])
def test_pool_producer_publishes_hot_view(day, tmp_path, monkeypatch):
    from engine_core.guardian.registry import _validate_storage, load_registry
    view = {'kind': 'redis_view', 'role': 'view', 'name': 'core:view:limit_up_pool:{date}'}
    retention = {'kind': 'trading_days', 'count': 5, 'anchor': 'data_date',
                 'expire_time': '00:00:00', 'timezone': 'Asia/Shanghai'}
    _validate_storage('limit_up_pool', [{**view, 'retention': retention}])
    _validate_storage('old_view', [{**view, 'ttl_seconds': 60}])
    for lifecycle in ({}, {'ttl_seconds': 0}, {'ttl_seconds': True},
                      {'retention': {**retention, 'count': 0}},
                      {'retention': {**retention, 'count': True}},
                      {'retention': {k: v for k, v in retention.items() if k != 'count'}},
                      {'retention': {**retention, 'kind': 'calendar_days'}},
                      {'retention': {**retention, 'timezone': 'UTC'}},
                      {'retention': {**retention, 'expire_time': '25:00:00'}},
                      {'retention': {**retention, 'anchor': 'now'}},
                      {'retention': {**retention, 'extra': 1}},
                      {'retention': retention, 'ttl_seconds': 60}):
        with pytest.raises(ValueError):
            _validate_storage('invalid_view', [{**view, **lifecycle}])
    registered = next(item for item in load_registry() if item['id'] == 'limit_up_pool')
    hot_location = next(item for item in registered['storage'] if item['kind'] == 'redis_view')
    assert hot_location['name'] == 'core:view:limit_up_pool:{data_date}'
    assert hot_location['retention'] == retention and 'redis_ttl_seconds' not in registered
    fixture = Path(__file__).parent / 'fixtures/limit_pool/kaipan_pool_20261026.json'
    pages = {int(k): v for k, v in json.loads(fixture.read_text())['pages'].items()}
    redis = RedisSpy()
    store = MemoryPoolStore()
    clock = lambda: datetime(2026, 10, 27, 1, tzinfo=timezone.utc)
    deps = PoolDeps(kaipan=lambda *_: {'pages': pages, 'request_count': 6}, td_store=lambda: store,
                    redis_client=redis, calendar_root=Path.home() / 'core-data', now_utc=clock)
    request = {'data_date': day, 'args': {'source': 'kaipan'}}
    code, body = run_pool_tier(request, tmp_path, deps=deps)
    assert code == 0, body
    key = f'core:view:limit_up_pool:{day}'
    assert key in redis.writes
    value = json.loads(redis.writes[key])
    assert value['row_count'] == len(value['rows']) > 0
    assert value['source_sha256'] != value['rows_sha256']
    assert redis.deadlines[key] == int(datetime.fromisoformat('2026-11-02T00:00:00+08:00').timestamp())
    assert hot.nth_trade_date('2026-11-10', 5, root=Path.home() / 'core-data').isoformat() == '2026-11-17'
    # A healthy same-rank hot result must finish before any cold or upstream access.
    from engine_core.guardian import limit_pool_d1 as producer
    with monkeypatch.context() as patch:
        patch.setattr(producer, 'existing_rank', lambda *_: pytest.fail('hot hit accessed catalog'))
        patch.setattr(redis, 'set', lambda *a, **kw: pytest.fail('hot hit rewrote Redis'))
        patch.setattr(deps, 'kaipan', lambda *_: pytest.fail('hot hit refetched source'))
        code, body = run_pool_tier(request, tmp_path, deps=deps)
        assert code == 20 and body['error_code'] == 'ALREADY_SATISFIED'
        same = hot.publish(day, value['rows'], {**value, 'available_at': '2026-10-27T01:01:00+00:00'},
                           redis_client=redis, calendar_root=Path.home() / 'core-data', clock=clock)
        assert same['status'] == 'unchanged'
    from engine_core.data_pipeline.limit_pool_td import LimitPoolTdSink
    # Seed a historical partition from the same captured source for fallback checks.
    assert LimitPoolTdSink(store, tmp_path).write_partition(
        day, value['rows'], {**value, 'sha256': value['source_sha256']}).status == 'synced'
    rows, meta = hot.read(day, '2026-10-27T09:25:00+08:00', redis_client=redis,
                          td_store=store)
    assert rows == value['rows'] and meta['status'] == 'READY' and meta['storage_source'] == 'redis'
    assert {'kp_02', 'kp_20', 'reason_plates_text', 'price'} <= set(rows[0])

    # Existing verified cold partition heals a missing view, without a refetch or new source timestamp.
    redis.writes.clear()
    deps.kaipan = lambda *_: pytest.fail('already satisfied must not refetch')
    code, body = run_pool_tier(request, tmp_path, deps=deps)
    assert code == 20 and body['detail']['hot_write']['status'] == 'published', body
    assert json.loads(redis.writes[key])['available_at'] == value['available_at']
    restored_rows, restored_meta = hot.read(day, '2026-10-27T09:25:00+08:00', redis_client=redis, td_store=store)
    assert restored_rows == value['rows'] and restored_meta['status'] == 'DEGRADED'
    redis.writes[key] = json.dumps(value)
    inferior = {**value, 'version': 'lower-quality-newer', 'business_status': 'PROVISIONAL',
                'degraded': True, 'available_at': '2026-10-27T02:00:00+00:00'}
    preserved = hot.publish(day, rows, inferior, redis_client=redis,
                            calendar_root=Path.home() / 'core-data', clock=clock)
    assert preserved['status'] == 'preserved_better'
    assert json.loads(redis.writes[key])['version'] == value['version']

    # Slice and prep use the exact hot bytes, without accessing catalog or TD.
    from engine_core.slices import SliceProviders, SliceReader
    from engine_core.prep_trade_day import _step
    class NoCold:
        def read(self):
            pytest.fail('pool hot path read catalog')
        def locate(self, *args):
            pytest.fail('pool hot path located catalog')
    monkeypatch.setattr(hot, '_client', lambda: redis)
    monkeypatch.setattr(hot, '_td_store', lambda: pytest.fail('pool hot path read TD'))
    cutoff = '2026-10-27T09:25:00+08:00'
    result = SliceReader(SliceProviders(catalog=NoCold(), redis=redis).providers()).get(
        'yest_limit_pool', trade_date='2026-10-27', asof=cutoff, prior_trade_date=day)
    assert result.rows == rows and result.meta['status'] == 'READY' and result.meta['provider'] == value['provider']
    step = _step('yesterday_limit_pool', 'limit_up_pool', day, NoCold(), asof=cutoff)
    assert step['status'] == 'READY' and step['storage_source'] == 'redis' and step['version'] == value['version']
    from engine_core.guardian import preopen_check
    monkeypatch.setattr('engine_core.prep_trade_day.previous_trade_date', lambda *_: day)
    check_code, check_body = preopen_check.run(
        {'trade_date': '2026-10-27', 'now': cutoff,
         'args': {'checks': {'limit_up_pool': {'statuses': ['FORMAL'], 'min_rows': 1}}}},
        tmp_path, catalog=NoCold())
    assert check_code == 0 and check_body['detail']['results'][0]['detail']['version'] == value['version']

    # Missing, malformed and disconnected Redis all explicitly fall back to real-derived TD rows.
    for raw in (None, '{', json.dumps({**value, 'rows_sha256': 'bad'})):
        redis.writes[key] = raw
        fallback_rows, fallback = hot.read(day, cutoff, redis_client=redis, td_store=store)
        assert fallback_rows and fallback['status'] == 'DEGRADED' and fallback['storage_source'] == 'td'
        assert fallback['fallback_reason'] and fallback['version'] == value['version']
    redis.fail = True
    fallback_rows, fallback = hot.read(day, cutoff, redis_client=redis, td_store=store)
    assert fallback_rows and fallback['fallback_reason'] == 'redis_ConnectionError'
    redis.fail = False
    redis.writes[key] = None
    physical = store.days[day]
    symbol = next(iter(physical))
    old_version = physical[symbol]['version']
    physical[symbol]['version'] = 'different'
    assert hot.read(day, cutoff, redis_client=redis, td_store=store)[1]['status'] == 'MISSING'
    physical[symbol]['version'] = old_version
    for physical_row in physical.values():
        physical_row['version'] = 'metadata-for-other-version'
    assert hot.read(day, cutoff, redis_client=redis, td_store=store)[1]['reason'] == 'td_version_metadata_mismatch'
    for physical_row in physical.values():
        physical_row['version'] = old_version
    early = '2026-10-26T15:00:00+08:00'
    assert hot.read(day, early, redis_client=redis, td_store=store)[1]['reason'] == 'late_visible'
    late_rows, late_meta = hot.read(day, early, redis_client=redis, td_store=store, late_rebuild=True,
                                     late_asof=cutoff)
    assert late_rows and late_meta['late_visible'] and late_meta['status'] == 'PARTIAL'
    redis.writes[key] = json.dumps({**value, 'business_status': 'PROVISIONAL', 'degraded': True})
    assert hot.read(day, cutoff, redis_client=redis, td_store=store)[1]['status'] == 'DEGRADED'
    deps.kaipan = lambda *_: {'pages': pages, 'request_count': 6}
    redis.fail = True
    code, body = run_pool_tier(request, tmp_path, deps=deps)
    assert code == 30 and body['error_code'] == 'HOT_PUBLISH_FAILED'
