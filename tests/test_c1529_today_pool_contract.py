"""Cold fallback contract using the recorded C1165 Redis view."""
import hashlib
import json
from pathlib import Path

from engine_core.data_pipeline import cold_archive, reader, redis_day_view
from engine_core.guardian.presence import resolve_presence

FIXTURE = Path(__file__).parent / 'fixtures/real/c1165'
RAW = (FIXTURE / 'view.json').read_bytes()
VALUE = json.loads(RAW)
DAY = VALUE['trade_date']


class ReadOnlyRedis:
    def __init__(self):
        self.writes = 0

    def get(self, key):
        return None

    def set(self, *args, **kwargs):
        self.writes += 1
        raise RuntimeError('write_blocked')


class RecordedCold:
    def latest_version(self, dataset, day):
        assert (dataset, day) == ('limit_up_pool_today', DAY)
        return VALUE['version']


def test_cold_read_is_degraded_and_never_writes(monkeypatch):
    provenance = json.loads((FIXTURE / 'PROVENANCE.json').read_text())
    assert hashlib.sha256(RAW).hexdigest() == provenance['sha256']['view.json']
    monkeypatch.setattr(cold_archive, 'read_envelope', lambda *args, **kwargs: RAW)
    monkeypatch.setattr(redis_day_view, 'hot_view_expire_at', lambda day: 2000000000)
    redis = ReadOnlyRedis()
    result = reader._read_today_pool(DAY, client=redis, cold_store=RecordedCold())
    assert redis.writes == 0
    assert (result.status, result.discovered_by, result.rows) == ('DEGRADED', 'td', VALUE['rows'])


def test_presence_cold_fallback_is_degraded_without_writes(monkeypatch):
    monkeypatch.setattr(cold_archive, 'read_envelope', lambda *args, **kwargs: RAW)
    monkeypatch.setattr(redis_day_view, 'hot_view_expire_at', lambda day: 2000000000)
    redis = ReadOnlyRedis()
    result = resolve_presence('limit_up_pool_today', DAY, redis_client=redis, cold_store=RecordedCold())
    assert redis.writes == 0
    assert result == 'DEGRADED'


def test_restore_requires_explicit_call_and_attempts_conditional_write(monkeypatch):
    import pytest
    monkeypatch.setattr(redis_day_view, 'hot_view_expire_at', lambda day: 2000000000)
    redis = ReadOnlyRedis()
    with pytest.raises(RuntimeError, match='write_blocked'):
        reader._restore_today_pool(DAY, redis, RAW)
    assert redis.writes == 1
