"""Read-only hot_rank fallback with rows from the recorded THS response."""
import hashlib
import json
from pathlib import Path

import pytest

from engine_core.data_pipeline import cold_archive, hot_rank_view
from engine_core.data_pipeline.providers.ths_hot_rank import ThsHotRankProvider
from engine_core.guardian.presence import resolve_presence
from engine_core.storage.two_layer import TwoLayerStore

DAY = '2026-10-23'
FIXTURES = Path(__file__).parent / 'fixtures' / 'real'


@pytest.fixture(autouse=True)
def _isolated_slice_root(monkeypatch):
    monkeypatch.setenv("CORE_STATE_CACHE", "off")
    monkeypatch.setenv("CORE_PROVIDER_STATE", "memory")


@pytest.fixture
def recorded_archive(monkeypatch):
    name = 'ths_hot_rank_2026-10-23_1441.json'
    raw = (FIXTURES / name).read_bytes()
    provenance = json.loads((FIXTURES / 'PROVENANCE.json').read_text())[name]
    assert hashlib.sha256(raw).hexdigest() == provenance['sha256']
    rows = ThsHotRankProvider.normalize(json.loads(raw))
    # Use the production serializer to wrap the recorded upstream rows; intercept
    # its write in memory, so no Redis or cold archive is mutated.
    class Capture:
        def set(self, key, value, **kwargs):
            self.raw = value
            raise RuntimeError('blocked')
    capture = Capture()
    monkeypatch.setattr(hot_rank_view, 'hot_view_expire_at', lambda _: 2000000000)
    with pytest.raises(RuntimeError, match='blocked'):
        hot_rank_view.publish(capture, DAY, rows, {'version': provenance['sha256'], 'available_at': provenance['fetched_at']})
    monkeypatch.setattr(cold_archive, 'read_envelope', lambda *a, **kw: capture.raw)
    class Cold:
        def latest_version(self, dataset, day):
            return provenance['sha256']
    return Cold(), rows


class BlockWrites:
    def __init__(self):
        self.writes = 0
    def get(self, key):
        return None
    def set(self, *args, **kwargs):
        self.writes += 1
        raise RuntimeError('write blocked')


def test_cold_get_and_presence_are_read_only(recorded_archive):
    cold, rows = recorded_archive
    redis = BlockWrites()
    store = TwoLayerStore(redis, cold)
    result = store.get('hot_rank', DAY)
    assert redis.writes == 0
    assert result.status == 'DEGRADED' and result.rows == rows
    assert store.presence('hot_rank', DAY).status == 'DEGRADED'
    assert resolve_presence('hot_rank', DAY, redis_client=redis, cold_store=cold) == 'DEGRADED'
    assert redis.writes == 0


def test_historical_visibility_remains_read_only(recorded_archive):
    cold, rows = recorded_archive
    redis = BlockWrites()
    store = TwoLayerStore(redis, cold)
    assert store.get('hot_rank', DAY, asof='2026-10-23T14:40:00+08:00').status == 'MISSING'
    result = store.get('hot_rank', DAY, asof='2026-10-23T15:00:00+08:00')
    assert result.status == 'DEGRADED' and result.rows == rows
    assert redis.writes == 0


def test_corrupt_reason_is_sanitized(recorded_archive, monkeypatch):
    cold, _ = recorded_archive
    def fail(*args, **kwargs):
        raise ValueError('sensitive details')
    monkeypatch.setattr(hot_rank_view, 'validate', fail)
    result = TwoLayerStore(BlockWrites(), cold).get('hot_rank', DAY)
    assert result.status == 'CORRUPT'
    assert result.reason == 'invalid_hot_rank_or_archive'
