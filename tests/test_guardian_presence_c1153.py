import json
from pathlib import Path

import pytest

RAW = (Path(__file__).parent / 'fixtures/real/c1147/limit_up_pool_1112_formal.json').read_bytes()
DAY = '2026-11-12'


class Redis:
    def __init__(self, fail=False):
        self.values = {}
        self.fail = fail
        self.writes = 0

    def get(self, key):
        if self.fail:
            raise ConnectionError('offline')
        return self.values.get(key)

    def set(self, key, value, **kwargs):
        assert kwargs.get('nx') is True and kwargs['exat'] > 0
        if key in self.values:
            return False
        self.writes += 1
        self.values[key] = value
        return True


class Cold:
    def __init__(self, populated=True):
        self.rows = []
        if populated:
            from engine_core.data_pipeline.cold_archive import write_envelope
            write_envelope(self, 'limit_up_pool', DAY, json.loads(RAW)['version'], RAW, {})

    def latest_version(self, dataset, day):
        return json.loads(RAW)['version'] if self.rows else None

    def read(self, *args):
        return self.rows.copy()

    def write(self, dataset, day, version, rows, *, meta):
        self.rows.extend(rows)


@pytest.mark.parametrize('case', ['restore', 'missing', 'redis_down', 'corrupt'])
def test_presence_real_envelope_and_dangerous_controls(case, monkeypatch):
    from engine_core.guardian.presence import resolve_presence
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', lambda _: 2000000000)
    redis = Redis(fail=case == 'redis_down')
    cold = Cold(populated=case != 'missing')
    if case == 'corrupt':
        cold.rows[0]['payload'] += b'x'
    expected = {'restore': 'RESTORED_FROM_TD', 'missing': 'MISSING',
                'redis_down': 'UNKNOWN', 'corrupt': 'CORRUPT'}[case]
    assert resolve_presence('limit_up_pool', DAY, redis_client=redis, cold_store=cold) == expected
    if case == 'restore':
        assert redis.values['core:view:limit_up_pool:' + DAY] == RAW
        assert resolve_presence('limit_up_pool', DAY, redis_client=redis, cold_store=cold) == 'PRESENT_REDIS'
        assert redis.writes == 1
    else:
        assert redis.writes == 0


@pytest.mark.parametrize('presence,fetch', [('RESTORED_FROM_TD', False), ('MISSING', True), ('UNKNOWN', False)])
def test_missing_catalog_and_pool_planning(tmp_path, monkeypatch, caplog, presence, fetch):
    from engine_core.guardian import startup
    monkeypatch.setattr(startup, '_calendar', lambda *_: (None, {'prev': DAY}))
    monkeypatch.setattr('engine_core.guardian.presence.resolve_presence', lambda *_: presence)
    result = startup.boot_reconcile('2026-11-13', 'PRE_MARKET', {'items': []}, tmp_path,
                                    now='2026-11-13T08:30:00+08:00')
    plan = result['detail']['plan']
    assert not any(row['action'] == 'PLAN_ERROR' for row in plan)
    assert any('catalog' in record.message.lower() for record in caplog.records)
    assert any(row['id'] == 'limit_up_pool' and row['action'] == 'FETCH_NOW' for row in plan) == fetch
    if presence == 'UNKNOWN':
        assert any(row['id'] == 'limit_up_pool' and row['status'] == 'UNKNOWN' for row in result['detail']['gaps'])


def test_unreadable_catalog_does_not_break_plan(tmp_path, monkeypatch, caplog):
    from engine_core.guardian import startup
    monkeypatch.setattr(startup, "_calendar", lambda *_: (None, {"prev": None}))
    result = startup.boot_reconcile("2026-11-13", "PRE_MARKET", {"items": []}, tmp_path,
                                    now="2026-11-13T08:30:00+08:00")
    assert not any(row["action"] == "PLAN_ERROR" for row in result["detail"]["plan"])
    assert any("catalog" in record.message.lower() for record in caplog.records)


@pytest.mark.parametrize('version,expected', [
    ('sha256-382ebc1cd7502951:ba07029b9a6576e8', 'RESTORED_FROM_TD'),
    ('sha256-382ebc1cd7502951:ba07029b9a6576e9', 'CORRUPT'),
])
def test_presence_production_archive_version(version, expected, monkeypatch):
    import hashlib
    from engine_core.guardian.presence import resolve_presence

    assert hashlib.sha256(RAW).hexdigest() == 'ba07029b9a6576e814d8e4846fe50875924fe98b5e473904bdd84ab764f2decb'
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', lambda _: 2000000000)
    redis, cold = Redis(), Cold()
    cold.latest_version = lambda *_: version
    assert resolve_presence('limit_up_pool', DAY, redis_client=redis, cold_store=cold) == expected
    if expected == 'RESTORED_FROM_TD':
        assert redis.values['core:view:limit_up_pool:' + DAY] == RAW
    else:
        assert redis.writes == 0
