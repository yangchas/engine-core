import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from engine_core.data_pipeline.cold_archive import write_envelope
from engine_core.guardian.registry import load_registry
from engine_core.guardian.reconcile_plan import reconcile_plan

RAW = (Path(__file__).parent / 'fixtures/real/c1147/limit_up_pool_1112_formal.json').read_bytes()
DAY = '2026-11-12'


def test_hot_rank_missing_td_table_is_missing(monkeypatch):
    from engine_core.guardian.presence import resolve_presence

    error = json.loads((Path(__file__).parent / 'fixtures/real/td/errors/table_does_not_exist.json').read_text())

    class Redis:
        def get(self, key):
            return None

    class Cold:
        def latest_version(self, dataset, day):
            raise RuntimeError(error['message'])

    assert resolve_presence('hot_rank', DAY, redis_client=Redis(), cold_store=Cold()) == 'MISSING'


def test_previous_pool_without_catalog_is_planned_from_storage():
    assert json.loads(RAW)['trade_date'] == DAY
    item = next(item for item in load_registry() if item['id'] == 'limit_up_pool')
    now = datetime(2026, 11, 13, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
    rows = reconcile_plan(now, {'datasets': {}}, trade_date='2026-11-13',
                          prev_trade_date=DAY, registry=[item], pool_presence='MISSING')
    assert [(row['id'], row['partition'], row['action']) for row in rows] == [
        ('limit_up_pool', DAY, 'FETCH_NOW')]


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **kwargs):
        if kwargs.get('nx') and key in self.values:
            return False
        self.values[key] = value
        return True


class MemoryCold:
    def __init__(self, populated=True):
        self.rows = []
        if populated:
            write_envelope(self, 'limit_up_pool', DAY, json.loads(RAW)['version'], RAW, {})

    def latest_version(self, dataset, day):
        return json.loads(RAW)['version'] if self.rows else None

    def read(self, *args):
        return self.rows.copy()

    def write(self, dataset, day, version, rows, *, meta):
        self.rows.extend(rows)


def test_registry_presence_restores_real_pool_and_ignores_stale_catalog(monkeypatch, tmp_path):
    from engine_core.guardian.presence import resolve_presence
    items = load_registry()
    ids = {'hot_plates', 'hot_plates_yesterday', 'hot_plates_gaps',
           'limit_up_pool_today', 'limit_up_pool', 'intraday_tick_stream'}
    selected = [item for item in items if item['id'] in ids]
    assert len(selected) == len(ids)
    redis, cold = MemoryRedis(), MemoryCold()
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', lambda _: 2000000000)
    monkeypatch.setattr('engine_core.guardian.presence.resolve_presence',
                        lambda dataset, date, **kwargs: resolve_presence(dataset, date, redis_client=redis, cold_store=cold))
    now = datetime(2026, 11, 13, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
    stale = {'datasets': {name: {'dates': {DAY: [{'version': 'stale'}]}}
                          for name in ('hot_plates', 'hot_plates_gaps', 'limit_up_pool_today', 'limit_up_pool', 'intraday_checkpoint')}}
    def plan(catalog):
        return reconcile_plan(now, catalog, trade_date='2026-11-13', prev_trade_date=DAY, registry=selected)
    empty, old = plan({'datasets': {}}), plan(stale)
    assert empty == old == plan(str(tmp_path / 'missing-catalog.json'))
    from engine_core.storage.two_layer import TwoLayerStore
    TwoLayerStore(redis, cold).restore('limit_up_pool', DAY)
    assert redis.values['core:view:limit_up_pool:' + DAY] == RAW
    assert not any(row['action'] == 'FETCH_NOW' for row in empty)
    assert all(item.get('hot_store') and item.get('cold_store') and item.get('kind') for item in selected)


def test_missing_and_unavailable_storage_do_not_follow_stale_catalog(monkeypatch, tmp_path):
    from engine_core.guardian.presence import resolve_presence
    items = [item for item in load_registry() if item['id'] in {
        'hot_plates', 'hot_plates_yesterday', 'hot_plates_gaps',
        'limit_up_pool_today', 'limit_up_pool', 'intraday_tick_stream'}]
    redis, cold = MemoryRedis(), MemoryCold(populated=False)
    monkeypatch.setattr('engine_core.guardian.presence.resolve_presence',
                        lambda dataset, date, **kwargs: resolve_presence(dataset, date, redis_client=redis, cold_store=cold))
    now = datetime(2026, 11, 13, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
    stale = {'datasets': {'limit_up_pool': {'dates': {DAY: [{'version': 'stale'}]}}}}
    def plan(catalog):
        return reconcile_plan(now, catalog, trade_date='2026-11-13', prev_trade_date=DAY, registry=items)
    rows = plan(stale)
    assert rows == plan({'datasets': {}}) == plan(str(tmp_path / 'missing.json'))
    assert [(row['id'], row['action']) for row in rows] == [('limit_up_pool', 'FETCH_NOW')]
    class BrokenRedis(MemoryRedis):
        def get(self, key):
            raise ConnectionError('offline')
    redis = BrokenRedis()
    assert plan(stale) == []
