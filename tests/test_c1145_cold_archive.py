import hashlib
import json
from pathlib import Path

from engine_core.data_pipeline import cold_archive
from engine_core.data_pipeline.jobs import JobRunner, JobState
from engine_core.data_pipeline.providers.limit_up_today_sources import publish_hot_snapshot
from engine_core.data_pipeline.registry import get_dataset

ROOT = Path(__file__).parent / 'fixtures/real'


def fixture(folder, name):
    root = ROOT / folder
    raw = (root / name).read_bytes()
    provenance = json.loads((root / 'PROVENANCE.json').read_bytes())
    assert hashlib.sha256(raw).hexdigest() == provenance[name]['sha256']
    return json.loads(raw)


class Redis:
    def __init__(self): self.values = {}
    def get(self, key): return self.values.get(key)
    def set(self, key, value, **kwargs): self.values[key] = value; return True
    def delete(self, key): self.values.pop(key, None)


def check_archive(monkeypatch, publish, dataset):
    calls = []
    class MemoryStore:
        def __init__(self): self.rows = {}
        def read(self, *args): return list(self.rows.values())
        def write(self, dataset, day, version, rows, *, meta):
            self.rows.update((row['chunk_no'], dict(row)) for row in rows)
    store = MemoryStore()
    def archive(*args):
        calls.append(args)
        cold_archive.write_envelope(store, *args[:4], args[4])
    monkeypatch.setattr(cold_archive, 'archive', archive)
    monkeypatch.setattr(cold_archive, 'flush', lambda: True)
    redis = Redis()
    result = publish(redis)
    assert len(calls) == 1
    name, day, version, raw, meta = calls[0]
    assert name == dataset
    assert raw == redis.values[f'core:cache:data:{dataset}:{day}:{version}'].encode()
    assert meta['version'] == version
    assert cold_archive.read_envelope(dataset, day, version, store=store) == raw
    def fail(*args): raise RuntimeError('cold failure')
    monkeypatch.setattr(cold_archive, 'archive', fail)
    again = Redis()
    assert publish(again) == result
    assert again.values == redis.values


def test_hot_plates_archive(tmp_path, monkeypatch):
    from datetime import date
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, 'nth_trade_date', lambda *args, **kwargs: date(2099, 1, 1))
    view = fixture('c1094', 'view.json')
    rows = view.pop('rows')
    runner = JobRunner(artifact_root=tmp_path, state=JobState())
    def publish(redis):
        runner.redis = redis
        return runner._publish_redis(get_dataset('hot_plates'), view['trade_date'], view, rows, view['source'])
    check_archive(monkeypatch, publish, 'hot_plates')


def test_limit_up_archive(monkeypatch):
    from engine_core.data_pipeline import redis_day_view
    monkeypatch.setattr(redis_day_view, 'hot_view_expire_at', lambda day: 4070908800)
    view = fixture('c1120', 'version.json')
    rows = view.pop('rows')
    check_archive(monkeypatch, lambda redis: publish_hot_snapshot(view, rows, client=redis), 'limit_up_pool_today')
