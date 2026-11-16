import hashlib
import json
import pytest
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
    failed = publish(again)
    if failed is not None:
        assert {k: v for k, v in failed.items() if k != 'cold_archive_warning'} == result
        assert failed['cold_archive_warning'] == {'category': 'RuntimeError', 'td_error_code': None, 'successful_chunks': 0}
    else:
        assert failed == result
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


@pytest.mark.parametrize("dataset", ["hot_plates", "hot_rank"])
def test_cold_failure_receipts_reach_hot_results(tmp_path, monkeypatch, dataset):
    from datetime import date
    from engine_core.data_pipeline import hot_plates_gaps, trade_calendar
    from test_c1142_cold_archive import sample

    class RejectStore:
        def read(self, *args): return []
        def write(self, *args, **kwargs): raise RuntimeError('TD rejected 0x2603')
        def close(self): pass

    worker = cold_archive.Archiver(RejectStore)
    monkeypatch.setattr(cold_archive, '_worker', worker)
    monkeypatch.setattr(cold_archive, '_enabled', lambda: True)
    monkeypatch.setattr(trade_calendar, 'nth_trade_date', lambda *args, **kwargs: date(2099, 1, 1))
    if dataset == 'hot_rank':
        from engine_core.data_pipeline import hot_rank_view
        from engine_core.data_pipeline.providers import wencai_hot_rank
        from engine_core.data_pipeline.jobs import RequestBudget
        raw = (ROOT / 'c1223/2026-11-13.json').read_bytes()
        assert hashlib.sha256(raw).hexdigest() == json.loads((ROOT / 'c1223/PROVENANCE.json').read_bytes())['sha256']
        payload = json.loads(raw)['payload']
        monkeypatch.setattr(hot_rank_view, 'hot_view_expire_at', lambda _: 4070908800)
        runner = JobRunner(artifact_root=tmp_path, redis=Redis(), budget=RequestBudget(min_interval_seconds=0))
        result = runner.run('hot_rank', '2026-11-13', fetch=lambda day: wencai_hot_rank.fetch(day, cookie_file=None, query_fn=lambda **kw: {'payload': payload}), source='wencai_hot_rank', publish=True)
        view = result.manifest
    else:
        view = fixture('c1094', 'view.json')
        rows = view.pop('rows')
        runner = JobRunner(artifact_root=tmp_path, state=JobState(), redis=Redis())
        runner._publish_redis(get_dataset('hot_plates'), view['trade_date'], view, rows, view['source'])
    assert view['cold_archive_warning'] == {'category': 'RuntimeError', 'td_error_code': '0x2603', 'successful_chunks': 0}



def test_gap_failure_receipt_is_returned(tmp_path, monkeypatch):
    from engine_core.data_pipeline import hot_plates_gaps, limit_pool_hot_view, redis_day_view
    from test_c1142_cold_archive import sample
    raw, row = sample()
    class RejectStore:
        def read(self, *args): return []
        def write(self, *args, **kwargs): raise RuntimeError('TD rejected 0x2603')
        def close(self): pass
    class GapRedis(Redis):
        def scan_iter(self, **kwargs): return []
    redis = GapRedis()
    monkeypatch.setattr(limit_pool_hot_view, '_client', lambda: redis)
    monkeypatch.setattr(redis_day_view, 'hot_view_expire_at', lambda _: 4070908800)
    monkeypatch.setattr(hot_plates_gaps, '_snapshot_manifests', lambda *_: [])
    monkeypatch.setattr(cold_archive, '_worker', cold_archive.Archiver(RejectStore))
    monkeypatch.setattr(cold_archive, '_enabled', lambda: True)
    args = (tmp_path, row['trade_date'], row['event_id'], row['scheduled_at'], row['deadline'], row['observed_at'], row['gap_reason'])
    result = hot_plates_gaps.record_hot_plates_gap(*args)
    assert result['cold_archive_warning'] == {'category': 'RuntimeError', 'td_error_code': '0x2603', 'successful_chunks': 0}
    assert json.loads(next(iter(redis.values.values()))) == row


def test_today_td_rejection_warns_after_hot_publish(monkeypatch):
    from engine_core.data_pipeline import redis_day_view
    class RejectStore:
        def read(self, *args): return []
        def write(self, *args, **kwargs): raise RuntimeError('TD rejected 0x2603')
        def close(self): pass
    monkeypatch.setattr(cold_archive, '_worker', cold_archive.Archiver(RejectStore))
    monkeypatch.setattr(cold_archive, '_enabled', lambda: True)
    monkeypatch.setattr(redis_day_view, 'hot_view_expire_at', lambda day: 4070908800)
    view = fixture('c1120', 'version.json')
    rows = view.pop('rows')
    redis = Redis()
    result = publish_hot_snapshot(view, rows, client=redis)
    assert result['status'] == 'published' and redis.values
    assert result['cold_archive_warning'] == {'category': 'RuntimeError', 'td_error_code': '0x2603', 'successful_chunks': 0}
