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

    def expiretime(self, key):
        return self.deadlines.get(key, -1)

    def set(self, key, value, *, exat):
        if self.fail:
            raise ConnectionError('redis unavailable')
        self.writes[key] = value
        self.deadlines[key] = exat
        return True

    def execute(self):
        return [True]


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
    from engine_core.data_pipeline.limit_pool_td import LimitPoolTdSink
    from engine_core.data_pipeline.storage import ArtifactCatalog
    supplied_catalog = ArtifactCatalog(tmp_path).read()
    with monkeypatch.context() as patch:
        patch.setattr(LimitPoolTdSink, '_catalog', lambda self: pytest.fail('ignored supplied catalog'))
        assert LimitPoolTdSink(store, tmp_path).sync_date(day, supplied_catalog).status == 'unchanged'
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
    redis.fail = True
    code, body = run_pool_tier(request, tmp_path, deps=deps)
    assert code == 30 and body['error_code'] == 'HOT_PUBLISH_FAILED'
    assert body['detail']['td_write']['status'] == 'ok'
