import json
import shutil
import requests
import pytest
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import engine_core.data_pipeline.stock_cap as stock_cap
from engine_core.data_pipeline.stock_cap import fetch_stock_caps, read_stock_caps
from engine_core.data_pipeline.redis_day_view import expire_at

ROWS = json.loads((Path(__file__).parent / 'fixtures/real/c1365/wencai_excerpts.json').read_text())['rows']
UNIVERSE = {row['code'] for row in ROWS}
DAY = '2026-11-17'


@pytest.fixture(autouse=True)
def real_calendar(tmp_path, monkeypatch):
    root = tmp_path / 'core-data'
    (root / 'trade_calendar').mkdir(parents=True)
    shutil.copyfile('/home/exedev/core-data/trade_calendar/calendar.json', root / 'trade_calendar/calendar.json')
    monkeypatch.setenv('CORE_DATA_ROOT', str(root))


class Redis:
    def __init__(self): self.values, self.hashes, self.expiry = {}, {}, {}
    def delete(self, key): self.values.pop(key, None); self.hashes.pop(key, None)
    def pipeline(self): return self
    def execute(self): return []
    def hset(self, key, mapping): self.hashes[key] = mapping
    def hlen(self, key): return len(self.hashes.get(key, {}))
    def hgetall(self, key): return self.hashes.get(key, {})
    def expireat(self, key, deadline): self.expiry[key] = deadline
    def set(self, key, value, exat): self.values[key] = value; self.expiry[key] = exat
    def get(self, key): return self.values.get(key)


def replay(rows=ROWS):
    def query(**kwargs):
        page = kwargs['page']
        return {'data': rows[(page - 1) * 5:page * 5], 'row_count': len(rows)}
    return query


def fetch(query, **kwargs):
    return fetch_stock_caps(DAY, query_fn=query, cookie='', page_size=5,
                            universe=UNIVERSE, sleep_fn=lambda _: None,
                            now_fn=lambda: datetime(2026, 11, 17, 16, 0, 0, 123, tzinfo=ZoneInfo('Asia/Shanghai')),
                            **kwargs)


def test_real_rows_pagination_and_publication():
    redis = Redis()
    result = fetch(replay(), redis_client=redis)
    assert result['error'] is None
    assert len(result['rows']) == 10
    for source in ROWS:
        row = result['rows'][source['code']]
        assert row['float_cap_yuan'] == float(source['a股市值(不含限售股)[20261117]'])
        assert row['total_cap_yuan'] == float(source['总市值[20261117]'])
    assert result['rows']['300308']['float_cap_yuan'] == 436205240000
    assert result['rows']['600736']['float_cap_yuan'] > result['rows']['600736']['total_cap_yuan']
    assert result['meta']['quality']['float_gt_total_count'] == 1
    actual = read_stock_caps(DAY, redis)
    assert actual['rows'] == result['rows'] and actual['reason'] is None
    assert actual['meta']['quality'] == result['meta']['quality']
    assert actual['meta']['source'] == 'wencai'
    assert actual['meta']['available_at'] == '2026-11-17T16:00:01+08:00'
    key = 'core:view:stock_cap:2026-11-17'
    assert redis.expiry[key] == redis.expiry[key + ':meta'] == expire_at(DAY)


def test_incomplete_wrong_date_and_forbidden_never_publish():
    redis = Redis()
    calls = []
    def missing(**kwargs):
        calls.append(kwargs['page'])
        if kwargs['page'] == 2: return {'data': [], 'row_count': 10}
        return replay()(**kwargs)
    result = fetch(missing, redis_client=redis, max_cooldowns=1)
    assert result['error'] and result['retryable'] and result['meta']['missing_pages'] == [2]
    assert calls == [1, 2, 2] and not redis.values and not redis.hashes
    wrong = [{key.replace('[20261117]', '[20261116]'): value for key, value in row.items()} for row in ROWS]
    assert fetch(replay(wrong), redis_client=redis)['error'] == 'date_mismatch'
    def forbidden(**kwargs):
        calls.append('403')
        raise RuntimeError('HTTP 403')
    result = fetch(forbidden, redis_client=redis)
    assert result['error'] == 'http_403' and not result['retryable']
    assert calls.count('403') == 1 and not redis.values and not redis.hashes
    limited = fetch(replay(), redis_client=redis, max_requests=1)
    assert limited['error'] == 'incomplete_pages' and limited['meta']['missing_pages'] == [2]
    incomplete_universe = fetch_stock_caps(DAY, query_fn=replay(), cookie='', page_size=5,
                                           universe=UNIVERSE | {'920029'}, sleep_fn=lambda _: None,
                                           redis_client=redis)
    assert incomplete_universe['error'] == 'stock_universe_coverage'
    assert not redis.values and not redis.hashes


def test_missing_view_returns_reason():
    result = read_stock_caps(DAY, Redis())
    assert not result['rows'] and result['reason']


def test_live_http_guard_restores_send_after_upstream_exception(monkeypatch):
    original = requests.sessions.Session.send

    def boom(*args, **kwargs):
        raise RuntimeError('upstream failure')

    monkeypatch.setattr(stock_cap, '_fetch_query', boom)
    monkeypatch.setattr(stock_cap, '_read_cookie', lambda _: '')
    result = fetch_stock_caps(DAY, universe=UNIVERSE)

    assert result['error'] == 'upstream_error'
    assert requests.sessions.Session.send is original
