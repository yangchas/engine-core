import hashlib
import json
from pathlib import Path
from datetime import datetime

from engine_core.data_pipeline import hot_plates_gaps, limit_pool_hot_view
from engine_core.data_pipeline.storage import ArtifactCatalog


def test_real_gap_published_only_to_redis(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / 'fixtures/real/c1106'
    raw = (fixture / 'row.jsonl').read_bytes()
    provenance = json.loads((fixture / 'PROVENANCE.json').read_text())
    assert hashlib.sha256(raw).hexdigest() == provenance['row.jsonl']['sha256']
    row = json.loads(raw)

    class Redis:
        def __init__(self):
            self.values = {}
            self.expiries = {}
        def get(self, key):
            return self.values.get(key)
        def set(self, key, value, ex, nx=False):
            if nx and key in self.values:
                return False
            self.values[key] = value
            self.expiries[key] = ex
            return True
        def scan_iter(self, match, count=100):
            from fnmatch import fnmatch
            return [key for key in self.values if fnmatch(key, match)]

    redis = Redis()
    monkeypatch.setattr(limit_pool_hot_view, '_client', lambda: redis)
    monkeypatch.setattr(hot_plates_gaps, '_snapshot_manifests', lambda *_: [{
        'captured_at': row['stale_captured_at'], 'version': row['stale_version'],
        'manifest_uri': row['stale_manifest']}])
    from engine_core.data_pipeline import redis_day_view
    monkeypatch.setattr(redis_day_view, "hot_view_expire_at", lambda _day: 4070908800)
    gap = hot_plates_gaps.record_hot_plates_gap(
        tmp_path, row['trade_date'], row['event_id'], row['scheduled_at'], row['deadline'],
        row['observed_at'], row['gap_reason'])
    observed = datetime.fromisoformat(row['observed_at']).isoformat(timespec='seconds')
    expected = {**row, 'observed_at': observed, 'schema_version': 1,
                'dataset': 'hot_plates_gaps', 'source': 'guardian_gap_only',
                'version': row['event_id'], 'captured_at': observed, 'available_at': observed}
    assert gap == expected
    assert not (tmp_path / 'catalog.json').exists()
    assert not (tmp_path / 'hot_plates_gaps').exists()
    assert hot_plates_gaps._existing_gap_rows(ArtifactCatalog(tmp_path), row['trade_date']) == [expected]
    assert redis.values
    assert all(ttl > 0 for ttl in redis.expiries.values())
    from engine_core.data_pipeline.reader import load_dataset
    class ForbiddenCatalog:
        def locate(self, *_args):
            raise AssertionError("catalog must not be read")
    result = load_dataset("hot_plates_gaps", row['trade_date'], redis=redis, catalog=ForbiddenCatalog())
    assert result.rows == [expected] and result.discovered_by == "redis"
    assert load_dataset("hot_plates_gaps", row['trade_date'], redis=None, catalog=ForbiddenCatalog()) is None
