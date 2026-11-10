# 逻辑测试:验证顺序与分支,不代表真实 Redis 行为。
import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.redis_day_view import meta_key, view_key, write_day_view
from engine_core.data_pipeline.redis_day_view_read import read_day_view

DATE = '2026-11-09'
FAMILY = 'stock_extra'
KEY = view_key(FAMILY, DATE)
META = meta_key(FAMILY, DATE)


@pytest.fixture(autouse=True)
def calendar(monkeypatch):
    monkeypatch.setenv('CORE_DATA_ROOT', '/home/exedev/core-data')


def rows():
    path = Path(__file__).parent / 'fixtures/real/redis_view_stock_extra_2026-11-09.json'
    data = json.loads(path.read_text())
    return {key: json.loads(value) for key, value in list(data.items())[:2]}


class Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.commands.append((name, args, kwargs))
            return self
        return queue

    def execute(self):
        commands, self.commands = self.commands, []
        for name, args, kwargs in commands:
            getattr(self.redis, name)(*args, **kwargs)


class Redis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.expiries = {}
        self.fail = None
        self.fail_cleanup = False
        self.meta_deletes = 0
        self.after_get = None

    def pipeline(self):
        return Pipeline(self)

    def delete(self, key):
        if key == META:
            self.meta_deletes += 1
            if self.fail_cleanup and self.meta_deletes > 1:
                raise ConnectionError('cleanup failed')
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.expiries.pop(key, None)

    def hset(self, key, *, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    def hlen(self, key):
        return len(self.hashes.get(key, {}))

    def expireat(self, key, deadline):
        if self.fail == ('expireat', key):
            raise ConnectionError('expireat failed')
        self.expiries[key] = deadline

    def set(self, key, value, *, exat):
        if self.fail == ('set', key):
            raise ConnectionError('set failed')
        self.values[key] = value
        self.expiries[key] = exat
        if self.fail == ('set_after', key):
            raise ConnectionError('response lost after set')

    def get(self, key):
        result = self.values.get(key)
        if self.after_get:
            callback, self.after_get = self.after_get, None
            callback()
        return result

    def hmget(self, key, symbols):
        return [self.hashes.get(key, {}).get(symbol) for symbol in symbols]


def write(redis, data):
    write_day_view(redis, FAMILY, DATE, data, source='baostock', version='v1',
                   available_at='2026-10-28T23:01:00+08:00', fields=['symbol'])


def read(redis, symbols):
    return read_day_view(redis, FAMILY, DATE, symbols, td_loader=lambda *args: {}, ledger=lambda *args: None)


def test_disconnect_before_metadata():
    redis = Redis()
    redis.fail = ('set', META)
    with pytest.raises(ConnectionError):
        write(redis, rows())
    assert read(redis, list(rows()))['source'] == 'td_fallback'


def test_disconnect_after_metadata_requires_expiry():
    redis = Redis()
    redis.fail = ('set_after', META)
    redis.fail_cleanup = True
    with pytest.raises(ConnectionError):
        write(redis, rows())
    if redis.values.get(META) is not None:
        assert META in redis.expiries


def test_failed_compensation_does_not_publish_incomplete_view():
    redis = Redis()
    redis.fail = ('set', META)
    redis.fail_cleanup = True
    with pytest.raises(ConnectionError):
        write(redis, rows())
    assert redis.meta_deletes == 2
    assert read(redis, list(rows()))['source'] == 'td_fallback'


def test_read_interleaved_with_rewrite_matches_metadata_count():
    redis = Redis()
    data = rows()
    write(redis, data)
    redis.after_get = lambda: write(redis, dict(list(data.items())[:1]))
    result = read(redis, list(data))
    assert result['source'] != 'redis' or result['row_count'] == result['meta']['row_count']
