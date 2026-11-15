import hashlib
import json
import queue
from pathlib import Path

import pytest

from engine_core.data_pipeline import hot_plates_gaps, limit_pool_hot_view, redis_day_view

FIXTURE = Path(__file__).parent / 'fixtures/real/c1142'


def sample():
    raw = (FIXTURE / 'envelope.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == json.loads((FIXTURE / 'PROVENANCE.json').read_text())['envelope.json']['sha256']
    return raw, json.loads(raw)


def test_publish_archives_exact_redis_bytes_and_survives_failure(tmp_path, monkeypatch):
    from engine_core.data_pipeline import cold_archive
    raw, row = sample()
    class Redis:
        def __init__(self): self.values = {}
        def scan_iter(self, match, count=100): return []
        def get(self, key): return self.values.get(key)
        def set(self, key, value, ex, nx): self.values[key] = value; return True
    redis = Redis()
    calls = []
    monkeypatch.setattr(limit_pool_hot_view, '_client', lambda: redis)
    monkeypatch.setattr(hot_plates_gaps, '_snapshot_manifests', lambda *_: [])
    monkeypatch.setattr(redis_day_view, 'hot_view_expire_at', lambda _: 4070908800)
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: calls.append(args))
    args = (tmp_path, row['trade_date'], row['event_id'], row['scheduled_at'], row['deadline'], row['observed_at'], row['gap_reason'])
    assert hot_plates_gaps.record_hot_plates_gap(*args) == row
    assert len(calls) == 1 and calls[0][3] == raw
    monkeypatch.setattr(cold_archive, 'archive', lambda *args: (_ for _ in ()).throw(RuntimeError('td failure')))
    assert hot_plates_gaps.record_hot_plates_gap(*args) == row


def test_roundtrip_idempotence_and_corruption():
    from engine_core.data_pipeline import cold_archive
    raw, row = sample()
    class Store:
        def __init__(self): self.rows = {}
        def write(self, dataset, day, version, rows, *, meta):
            for item in rows: self.rows[item['ts']] = dict(item)
        def read(self, dataset, day, version): return list(self.rows.values())
    store = Store()
    cold_archive.write_envelope(store, 'hot_plates_gaps', row['trade_date'], row['version'], raw, row, chunk_size=100)
    first = dict(store.rows)
    cold_archive.write_envelope(store, 'hot_plates_gaps', row['trade_date'], row['version'], raw, row, chunk_size=100)
    assert store.rows == first
    assert cold_archive.read_envelope('hot_plates_gaps', row['trade_date'], row['version'], store=store) == raw
    store.rows[next(ts for ts, item in store.rows.items() if item['chunk_no'] == 0)]['payload'] = b'corrupt'
    with pytest.raises(ValueError): cold_archive.read_envelope('hot_plates_gaps', row['trade_date'], row['version'], store=store)


def test_queue_full_does_not_block(monkeypatch):
    from engine_core.data_pipeline import cold_archive
    raw, row = sample()
    worker = cold_archive.Archiver(lambda: None, max_queue=1, start=False)
    assert worker.submit('hot_plates_gaps', row['trade_date'], row['version'], raw, row)
    assert not worker.submit('hot_plates_gaps', row['trade_date'], row['version'], raw, row)
    assert worker.gaps == 1

    # Default pytest and sealed replay may never open a TD connection.
    monkeypatch.setenv('TD_CONFIG', 'not-a-connection')
    monkeypatch.setenv('CORE_REPLAY_DIR', '/sealed')
    monkeypatch.setattr(cold_archive, 'TdStore', lambda: pytest.fail('unexpected TD connection'))
    assert cold_archive.archive('hot_plates_gaps', row['trade_date'], row['version'], raw, row) is False
    monkeypatch.delenv('CORE_REPLAY_DIR')
    assert cold_archive.archive('hot_plates_gaps', row['trade_date'], row['version'], raw, row) is False

    # A failing TD worker records an actionable gap without raising on submit.
    import threading
    finished = threading.Event()
    def failing_store():
        finished.set()
        raise RuntimeError('TD unavailable')
    failing = cold_archive.Archiver(failing_store)
    assert failing.submit('hot_plates_gaps', row['trade_date'], row['version'], raw, row)
    assert finished.wait(2)
    failing.queue.join()
    assert failing.gaps == 1
