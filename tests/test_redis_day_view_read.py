# 逻辑测试:验证顺序与分支,不代表真实 Redis 行为。
import json
from pathlib import Path

from engine_core.data_pipeline.redis_day_view_read import read_day_view


def real_rows():
    path = Path(__file__).parent / 'fixtures/real/redis_view_stock_extra_2026-11-09.json'
    return {key: json.loads(value) for key, value in json.loads(path.read_text()).items()}


class FakeRedis:
    def __init__(self, meta, rows):
        self.meta = None if meta is None else json.dumps(meta).encode()
        self.rows = rows
        self.calls = []

    def get(self, key):
        return self.meta

    def hmget(self, key, fields):
        self.calls.append(list(fields))
        return [json.dumps(self.rows[symbol]).encode() if symbol in self.rows else None for symbol in fields]


def test_complete_reads_redis():
    rows = real_rows()
    redis = FakeRedis({'status': 'COMPLETE', 'row_count': len(rows)}, rows)
    def unexpected(*args):
        raise AssertionError('fallback must not be called')
    result = read_day_view(redis, 'stock_extra', '2026-11-09', list(rows)[:2], expected_rows=len(rows), td_loader=unexpected, ledger=unexpected)
    assert result == {'source': 'redis', 'reason': None, 'rows': dict(list(rows.items())[:2]), 'row_count': 2, 'meta': {'status': 'COMPLETE', 'row_count': len(rows)}}


def test_missing_metadata_falls_back_and_records_event():
    rows = real_rows()
    symbols = list(rows)[:2]
    calls, events = [], []
    def loader(*args):
        calls.append(args)
        return {s: rows[s] for s in symbols}
    redis = FakeRedis(None, rows)
    result = read_day_view(redis, 'stock_extra', '2026-11-09', symbols, td_loader=loader, ledger=lambda *args: events.append(args))
    assert result == {'source': 'td_fallback', 'reason': 'REDIS_VIEW_MISSING', 'rows': {s: rows[s] for s in symbols}, 'row_count': 2, 'meta': {}}
    assert calls == [('stock_extra', '2026-11-09', symbols)]
    assert events == [('VIEW_DEGRADED', {'family': 'stock_extra', 'trade_date': '2026-11-09', 'reason': 'REDIS_VIEW_MISSING', 'row_count': 2})]
    assert redis.calls == []


def test_incomplete_status_falls_back():
    rows = real_rows()
    for status in ('EMPTY', 'PARTIAL'):
        redis = FakeRedis({'status': status, 'row_count': 0}, rows)
        result = read_day_view(redis, 'stock_extra', '2026-11-09', [], td_loader=lambda *args: {}, ledger=lambda *args: None)
        assert result['reason'] == 'REDIS_VIEW_PARTIAL' and result['source'] == 'td_fallback'
        assert not redis.calls


def test_count_mismatch_falls_back():
    redis = FakeRedis({'status': 'COMPLETE', 'row_count': 1}, real_rows())
    result = read_day_view(redis, 'stock_extra', '2026-11-09', [], expected_rows=2, td_loader=lambda *args: {}, ledger=lambda *args: None)
    assert result['reason'] == 'REDIS_VIEW_PARTIAL' and result['source'] == 'td_fallback'
    assert not redis.calls


def test_hmget_batches_and_merges():
    rows = real_rows()
    assert len(rows) == 501
    redis = FakeRedis({'status': 'COMPLETE', 'row_count': len(rows)}, rows)
    result = read_day_view(redis, 'stock_extra', '2026-11-09', list(rows), td_loader=lambda *args: None, ledger=lambda *args: None)
    assert [len(batch) for batch in redis.calls] == [500, 1]
    assert result['rows'] == rows and result['row_count'] == 501


def test_real_stock_extra_sample_has_501_rows():
    path = Path(__file__).parent / 'fixtures/real/redis_view_stock_extra_2026-11-09.json'
    assert len(json.loads(path.read_text())) == 501
