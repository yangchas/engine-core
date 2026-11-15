import json

import pytest

from test_guardian_presence_c1153 import Cold, DAY, RAW, Redis


def test_presence_is_read_only_and_distinguishes_missing_and_outage(monkeypatch):
    from engine_core.storage.two_layer import TwoLayerStore
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', lambda _: 2000000000)
    redis, cold = Redis(), Cold()
    store = TwoLayerStore(redis, cold)
    assert store.presence('limit_up_pool', DAY).status == 'RESTORED_FROM_TD'
    assert redis.writes == 0
    assert TwoLayerStore(Redis(), Cold(False)).presence('limit_up_pool', DAY).status == 'MISSING'
    assert TwoLayerStore(Redis(fail=True), cold).presence('limit_up_pool', DAY).status == 'UNKNOWN'


def test_get_restores_real_envelope_and_rejects_corruption(monkeypatch):
    from engine_core.storage.two_layer import TwoLayerStore
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', lambda _: 2000000000)
    redis, cold = Redis(), Cold()
    store = TwoLayerStore(redis, cold)
    result = store.get('limit_up_pool', DAY)
    assert result.status == 'RESTORED_FROM_TD'
    assert result.rows == json.loads(RAW)['rows']
    assert store.get('limit_up_pool', DAY).status == 'PRESENT_REDIS'
    bad = Cold()
    bad.rows[0]['payload'] += b'x'
    result = TwoLayerStore(Redis(), bad).get('limit_up_pool', DAY)
    assert result.status == 'CORRUPT' and result.reason == 'cold_archive_chunk_hash'


def test_publish_delegates_and_reports_queued_archive(monkeypatch):
    from engine_core.storage.two_layer import TwoLayerStore
    value = json.loads(RAW)
    from engine_core.data_pipeline import limit_pool_hot_view, cold_archive
    calls = []
    def publish(day, rows, manifest, **kwargs):
        kwargs['on_write'](RAW, value)
        return {'status': 'published', 'version': value['version']}
    monkeypatch.setattr(limit_pool_hot_view, 'publish', publish)
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: calls.append(args) or True)
    store = TwoLayerStore(Redis(), Cold())
    result = store.publish('limit_up_pool', DAY, value['rows'], value)
    assert result.version == value['version'] and result.archive_status == 'QUEUED'
    assert calls[0][0:2] == ('limit_up_pool', DAY)
    with pytest.raises(NotImplementedError):
        store.advance('daily_kline', DAY, value['version'])
    with pytest.raises(NotImplementedError):
        store.list_versions('limit_up_pool', (DAY, DAY))


def test_corrupt_real_envelope_reports_reason_and_plans_fetch(monkeypatch):
    from engine_core.storage.two_layer import TwoLayerStore
    from engine_core.guardian import startup
    redis = Redis()
    redis.values['core:view:limit_up_pool:' + DAY] = RAW[:-10]
    result = TwoLayerStore(redis, Cold()).presence('limit_up_pool', DAY)
    assert result.status == 'CORRUPT' and result.reason
    assert TwoLayerStore(redis, Cold()).get('limit_up_pool', DAY).rows == []
    monkeypatch.setattr(startup, '_calendar', lambda *_: (None, {'prev': DAY}))
    monkeypatch.setattr('engine_core.guardian.presence.resolve_presence', lambda *_: 'CORRUPT')
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        result = startup.boot_reconcile('2026-11-13', 'PRE_MARKET', {'items': []}, root,
                                        now='2026-11-13T08:30:00+08:00')
    assert any(row['id'] == 'limit_up_pool' and row['action'] == 'FETCH_NOW'
               and 'corrupt' in row.get('reason', '') for row in result['detail']['plan'])
