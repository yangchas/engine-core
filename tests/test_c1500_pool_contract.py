"""Contract checks using the recorded Redis envelope and in-memory faults."""
import builtins
import hashlib
import io
import json
import os
import socket
import sys
import types
from pathlib import Path

import pytest

sys.dont_write_bytecode = True


@pytest.fixture(autouse=True)
def _isolated_slice_root(monkeypatch, request):
    """Offline, read-only replacement for the repository's tmp_path fixture."""
    for name in ('TD_CONFIG', 'TD_HOST', 'TD_PORT', 'TD_USER', 'TD_PASSWORD',
                 'TD_DATABASE', 'CORE_TD_DATABASE', 'REDIS_URL', 'CORE_REDIS_URL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('CORE_STATE_CACHE', 'off')
    monkeypatch.setenv('CORE_PROVIDER_STATE', 'memory')
    if hasattr(request.config, "cache"):
        request.config.cache.set = lambda *args, **kwargs: None

    def blocked(*args, **kwargs):
        raise RuntimeError('offline_read_only_test')

    class OfflineKaipan(types.ModuleType):
        def __getattr__(self, name):
            return blocked

    monkeypatch.setitem(sys.modules, 'pykaipan.pykaipan', OfflineKaipan('pykaipan.pykaipan'))
    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr('pytest.TempPathFactory.getbasetemp', blocked)
    original_open, original_io_open, original_os_open = builtins.open, io.open, os.open

    def checked_open(original, file, mode='r', *args, **kwargs):
        if any(flag in mode for flag in 'wax+'):
            return blocked()
        return original(file, mode, *args, **kwargs)

    def checked_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            return blocked()
        return original_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', lambda file, mode='r', *a, **kw: checked_open(original_open, file, mode, *a, **kw))
    monkeypatch.setattr(io, 'open', lambda file, mode='r', *a, **kw: checked_open(original_io_open, file, mode, *a, **kw))
    monkeypatch.setattr(os, 'open', checked_os_open)
    for name in ('remove', 'unlink', 'rmdir', 'removedirs', 'rename', 'replace', 'mkdir', 'makedirs'):
        monkeypatch.setattr(os, name, blocked)

from engine_core.data_pipeline import limit_pool_hot_view as pool
from engine_core.storage.two_layer import TwoLayerStore
from test_guardian_presence_c1153 import Cold, DAY, RAW, Redis

ASOF = '2026-11-16T12:00:00+00:00'


def test_recording_provenance():
    provenance = json.loads((Path(__file__).parent / 'fixtures/real/c1147/PROVENANCE.json').read_text())
    assert hashlib.sha256(RAW).hexdigest() == provenance['limit_up_pool_1112_formal.json']['sha256']


def test_redis_failure_keeps_cold_rows_and_read_only_presence():
    redis = Redis(fail=True)
    store = TwoLayerStore(redis, Cold())
    current = store.get('limit_up_pool', DAY)
    historical = store.get('limit_up_pool', DAY, asof=ASOF)
    presence = store.presence('limit_up_pool', DAY)
    rows_equal = current.rows == historical.rows == json.loads(RAW)['rows']
    assert rows_equal
    assert presence.metadata.get('cold_recoverable') is True
    assert redis.writes == 0


def test_partial_source_quality_survives_cold_fallback(monkeypatch):
    value = json.loads(RAW)
    value['quality']['status'] = 'PARTIAL'
    value['business_status'] = 'PARTIAL'
    raw = json.dumps(value).encode()
    cold = Cold(False)
    from engine_core.data_pipeline.cold_archive import write_envelope
    write_envelope(cold, 'limit_up_pool', DAY, value['version'], raw, {})
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', lambda _: 2000000000)
    result = TwoLayerStore(Redis(), cold).get('limit_up_pool', DAY, asof=ASOF)
    rows_equal = result.rows == value['rows']
    assert rows_equal
    assert result.status == 'PARTIAL'
    assert result.metadata['quality']['status'] == 'PARTIAL'


def test_physical_corruption_is_not_missing(monkeypatch):
    class Physical:
        def read_versions(self, day): return []
        def read_day_rows(self, day):
            row = json.loads(RAW)['rows'][0]
            return {row['symbol']: dict(row, version='broken')}
    monkeypatch.setattr(pool, '_td_store', lambda: Physical())
    monkeypatch.setattr(Physical, 'close', lambda self: None, raising=False)
    rows, meta = pool.read(DAY, ASOF, redis_client=Redis())
    assert not rows
    assert meta['status'] == 'CORRUPT'
    assert meta['reason'] == 'td_version_metadata_mismatch'


def test_future_redis_falls_back_to_visible_cold():
    redis = Redis()
    value = json.loads(RAW)
    value['available_at'] = '2099-01-01T00:00:00+00:00'
    redis.values['core:view:limit_up_pool:' + DAY] = json.dumps(value).encode()
    result = TwoLayerStore(redis, Cold()).get('limit_up_pool', DAY, asof=ASOF)
    rows_equal = result.rows == json.loads(RAW)['rows']
    assert rows_equal
    assert result.metadata['storage_source'] == 'cold'
    assert result.metadata['fallback_reason'] == 'late_visible'
    assert redis.writes == 0


def test_presence_does_not_restore():
    redis = Redis()
    result = TwoLayerStore(redis, Cold()).presence('limit_up_pool', DAY)
    assert result.metadata['cold_recoverable'] is True
    assert redis.writes == 0


def test_guardian_read_only_and_explicit_restore(monkeypatch):
    from engine_core.guardian.presence import resolve_presence
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', lambda _: 2000000000)
    redis = Redis()
    assert resolve_presence('limit_up_pool', DAY, redis_client=redis, cold_store=Cold()) == 'DEGRADED'
    assert redis.writes == 0
    assert TwoLayerStore(redis, Cold()).restore('limit_up_pool', DAY).status == 'RESTORED_FROM_TD'
    assert resolve_presence('limit_up_pool', DAY, redis_client=redis, cold_store=Cold()) == 'PRESENT_REDIS'


def test_cold_older_visible_version_survives_corrupt_newer(monkeypatch):
    class Versions(Cold):
        def list_versions(self, dataset, day):
            return ['corrupt', json.loads(RAW)['version']]
        def read(self, dataset, day, version):
            return [] if version == 'corrupt' else super().read(dataset, day, version)
    monkeypatch.setattr(pool, '_td_store', lambda: (_ for _ in ()).throw(RuntimeError('offline')))
    rows, meta = pool.read(DAY, ASOF, redis_client=Redis(), cold_store=Versions())
    rows_equal = rows == json.loads(RAW)['rows']
    assert rows_equal
    assert meta['selection_uncertain'] is True


def test_restore_returns_concurrent_winner(monkeypatch):
    value = json.loads(RAW)
    value['quality']['status'] = 'PARTIAL'
    value['business_status'] = 'PARTIAL'
    winner = json.dumps(value).encode()
    class RacingRedis(Redis):
        def set(self, key, raw, **kwargs):
            self.values[key] = winner
            return True
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', lambda _: 2000000000)
    result = TwoLayerStore(RacingRedis(), Cold()).restore('limit_up_pool', DAY)
    rows_equal = result.rows == value['rows']
    assert rows_equal
    assert result.status == 'PARTIAL'
    assert result.metadata['storage_source'] == 'redis'


def test_cold_only_reads_are_read_only(monkeypatch):
    from engine_core.guardian.presence import resolve_presence
    monkeypatch.setattr(pool, '_td_store', lambda: (_ for _ in ()).throw(RuntimeError('offline')))
    redis = Redis()
    store = TwoLayerStore(redis, Cold())
    current = store.get('limit_up_pool', DAY)
    presence = store.presence('limit_up_pool', DAY)
    guardian = resolve_presence('limit_up_pool', DAY, redis_client=redis, cold_store=Cold())
    assert current.status == presence.status == guardian == 'DEGRADED'
    assert current.rows == json.loads(RAW)['rows']
    assert redis.writes == 0


def test_restore_requires_verified_readback(monkeypatch):
    class LostWriteRedis(Redis):
        def set(self, key, raw, **kwargs):
            self.writes += 1
            return True
    monkeypatch.setattr(pool, '_td_store', lambda: (_ for _ in ()).throw(RuntimeError('offline')))
    monkeypatch.setattr('engine_core.data_pipeline.redis_day_view.hot_view_expire_at', lambda _: 2000000000)
    redis = LostWriteRedis()
    result = TwoLayerStore(redis, Cold()).restore('limit_up_pool', DAY)
    assert redis.writes == 1
    assert result.status != 'RESTORED_FROM_TD'
    assert result.metadata['restore_status'] == 'UNKNOWN'
    assert result.rows == json.loads(RAW)['rows']
