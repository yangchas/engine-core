import hashlib
import json
from pathlib import Path

from engine_core.slices import SliceProviders, SliceReader

FIXTURE = Path(__file__).parent / 'fixtures/real/c1094'


def test_hot_plates_redis_view_and_missing_without_catalog(tmp_path):
    provenance = json.loads((FIXTURE / 'PROVENANCE.json').read_bytes())
    bodies = {name: (FIXTURE / name).read_bytes() for name in ('pointer.json', 'view.json')}
    for name, body in bodies.items():
        assert hashlib.sha256(body).hexdigest() == provenance[name]['sha256']
    view = json.loads(bodies['view.json'])
    class Redis:
        def get(self, key):
            if isinstance(key, bytes):
                key = key.decode()
            return next((bodies[name] for name in bodies if provenance[name]['key'] == key), None)
        def scan_iter(self, match, count=10):
            yield provenance['view.json']['key'].encode()
    class Catalog:
        root = tmp_path
        def __getattr__(self, name):
            raise AssertionError('catalog must not be read')
    reader = SliceReader(SliceProviders(catalog=Catalog(), redis=Redis()).providers())
    result = reader.get('hot_plates_today', trade_date='2026-11-13', asof='2026-11-13T15:30:00+08:00')
    assert result.meta['source'] == 'core_redis'
    assert len(result.rows) == len(view['rows']) == 243
    missing = SliceReader(SliceProviders(catalog=Catalog(), redis=None).providers()).get(
        'hot_plates_today', trade_date='2026-11-13', asof='2026-11-13T15:30:00+08:00')
    assert missing.meta['status'] == 'MISSING'
    assert missing.meta['reason'] == 'REDIS_VIEW_MISSING'


def test_hot_plates_requires_redis_and_does_not_write_catalog(tmp_path, monkeypatch):
    import pytest
    from engine_core.data_pipeline.jobs import JobRunner, JobState

    view = json.loads((FIXTURE / 'view.json').read_bytes())
    runner = JobRunner(artifact_root=tmp_path, state=JobState())
    with pytest.raises(RuntimeError, match='redis'):
        runner.run('hot_plates', view['trade_date'],
                   fetch=lambda _day: {**view, 'expected_count': len(view['rows'])},
                   now=view['captured_at'], source=view['source'], publish=True)
    assert not (tmp_path / 'catalog.json').exists()
    assert not (tmp_path / 'hot_plates').exists()
    monkeypatch.setenv('CORE_DATA_ROOT', str(Path.home() / 'core-data'))
    from engine_core.data_pipeline.registry import get_dataset
    from engine_core.data_pipeline.redis_day_view import expire_at
    import time

    class Redis:
        def __init__(self):
            self.values = {}
            self.expiries = {}
        def get(self, key):
            return self.values.get(key)
        def set(self, key, value, ex):
            self.values[key] = value
            self.expiries[key] = ex
        def delete(self, key):
            self.values.pop(key, None)
        def scan_iter(self, match, count=10):
            from fnmatch import fnmatch
            return (key for key in self.values if fnmatch(key, match))

    redis = Redis()
    runner = JobRunner(artifact_root=tmp_path, state=JobState(), redis=redis)
    result = runner.run('hot_plates', view['trade_date'],
                        fetch=lambda _day: {**view, 'expected_count': len(view['rows'])},
                        now=view['captured_at'], available_at=view['committed_at'],
                        source=view['source'], publish=True)
    spec = get_dataset('hot_plates')
    assert spec.primary_backend == 'redis' and spec.redis_ttl_trade_days == 5
    registry = json.loads((Path(__file__).parents[1] / 'src/engine_core/guardian/datasets.registry.json').read_text())
    for item in registry['items']:
        if item['id'] in {'hot_plates', 'hot_plates_yesterday'}:
            assert item['redis_ttl_trade_days'] == spec.redis_ttl_trade_days
            redis_view = next(location for location in item['storage'] if location['kind'] == 'redis_view')
            assert redis_view['retention'] == {'kind': 'trading_days', 'count': 5, 'anchor': 'data_date',
                                               'expire_time': '00:00:00', 'timezone': 'Asia/Shanghai'}
    assert result.manifest['cache_publish'] == 'READY'
    version_key = f"core:cache:data:hot_plates:{view['trade_date']}:{result.manifest['version']}"
    assert abs(time.time() + redis.expiries[version_key] - expire_at(view['trade_date'])) < 3
    written = json.loads(redis.values[version_key])
    assert len(written['rows']) == 243
    from datetime import datetime
    def standardized(value):
        parsed = datetime.fromisoformat(value)
        assert value == parsed.isoformat(timespec='seconds')
        assert value.endswith('+08:00')
    for field in ('captured_at', 'available_at', 'fetched_at', 'declared_available_at', 'committed_at'):
        if written.get(field) is not None:
            standardized(written[field])
    from zoneinfo import ZoneInfo
    assert written['declared_available_at'] == datetime.fromisoformat(view['committed_at']).astimezone(
        ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    for row in written['rows']:
        for field in ('captured_at', 'available_at'):
            standardized(row[field])
    assert {'schema_version', 'dataset', 'trade_date', 'source', 'version', 'captured_at',
            'available_at', 'row_count', 'rows'} <= written.keys()
    assert json.loads(redis.values[f"core:cache:data:hot_plates:{view['trade_date']}"]) == {
        'manifest': None, 'trade_date': view['trade_date'], 'version': written['version']}
    assert not (tmp_path / 'catalog.json').exists()
    assert not (tmp_path / 'hot_plates').exists()


def test_hot_plates_redis_connection_failure_returns_missing(tmp_path):
    from redis.exceptions import ConnectionError

    provenance = json.loads((FIXTURE / 'PROVENANCE.json').read_bytes())
    view = json.loads((FIXTURE / 'view.json').read_bytes())

    class Redis:
        def scan_iter(self, match, count=100):
            yield provenance['view.json']['key'].encode()
        def get(self, key):
            raise ConnectionError('connection lost while reading captured view')

    class Catalog:
        root = tmp_path
        def __getattr__(self, name):
            raise AssertionError('catalog must not be read')

    reader = SliceReader(SliceProviders(catalog=Catalog(), redis=Redis()).providers())
    result = reader.get('hot_plates_today', trade_date=view['trade_date'],
                        asof=view['available_at'])
    assert result.rows == []
    assert result.meta['status'] == 'MISSING'
    assert result.meta['reason'] == 'REDIS_VIEW_MISSING'


def test_hot_plate_postcondition_reports_missing_when_redis_read_fails(tmp_path, monkeypatch):
    from redis.exceptions import ConnectionError
    from engine_core.data_pipeline import limit_pool_hot_view
    from engine_core.guardian.commands import validate_handler_postcondition

    view = json.loads((FIXTURE / 'view.json').read_bytes())
    class Redis:
        def scan_iter(self, match, count=100):
            raise ConnectionError('captured view unavailable')
    monkeypatch.setattr(limit_pool_hot_view, '_client', lambda: Redis())
    assert validate_handler_postcondition('kaipan_hot_capture', view['trade_date'], tmp_path,
                                          ['hot_plates'], event_args={'event_id': view['event_id']}) == (
        False, 'hot_plates_redis_unavailable')
