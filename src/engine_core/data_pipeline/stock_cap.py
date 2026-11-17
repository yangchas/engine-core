"""WenCai market capitalization day view; incomplete acquisitions never publish."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .providers.wencai_daily_adapter import _Run, _Stop, _fetch_query, _symbol_of_raw, _read_cookie, DEFAULT_COOKIE_FILE
from .redis_day_view import meta_key, view_key, write_day_view
from .time_format import shanghai_seconds_ceil

MAX_HTTP_REQUESTS = 220
MIN_REQUEST_INTERVAL = 4.2
MIN_COVERAGE = 0.98
FIELDS = ('symbol', 'float_cap_yuan', 'total_cap_yuan', 'a_total_cap_yuan', 'cap_as_of')
_HTTP_LOCK = threading.Lock()


def _number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _failure(reason, *, retryable=True, missing_pages=()):
    return {'rows': {}, 'meta': {'missing_pages': list(missing_pages)},
            'error': reason, 'retryable': retryable}


def fetch_stock_caps(trade_date, query_fn=None, cookie=None, redis_client=None, *, universe=None,
                     page_size=100, max_requests=MAX_HTTP_REQUESTS, sleep_fn=time.sleep,
                     clock_fn=time.monotonic, now_fn=None, max_cooldowns=3, context=None):
    day = date.fromisoformat(str(trade_date)).isoformat()
    if not universe:
        return _failure('stock_universe_unavailable')
    universe = {str(s).zfill(6) for s in universe}
    d = date.fromisoformat(day)
    question = f'{d.year}年{d.month}月{d.day}日A股流通市值;{d.year}年{d.month}月{d.day}日A股总市值'
    live = query_fn is None
    if live:
        from pywencai.wencai import get_robot_data
        query_fn = get_robot_data
        if cookie is None:
            cookie = _read_cookie(DEFAULT_COOKIE_FILE)
    last = [None]
    count = [0]
    circuit_open = [False]
    def throttle():
        if count[0] >= max_requests:
            raise _Stop('http_budget_exhausted')
        if last[0] is not None:
            delay = MIN_REQUEST_INTERVAL - (clock_fn() - last[0])
            if delay > 0:
                sleep_fn(delay)
        last[0] = clock_fn()
        count[0] += 1
    def call(**kwargs):
        if not live:
            throttle()
        result = query_fn(**kwargs)
        if isinstance(result, dict) and str(result.get('status_code', '')) == '403':
            raise RuntimeError('HTTP 403')
        return result
    run = _Run(query_fn=call, context=context, page_size=page_size, cookie=cookie,
               cooldown_seconds=60, cooldown_factor=2, cooldown_max_seconds=300,
               max_cooldowns=max_cooldowns, max_requests=max_requests, sleep_fn=sleep_fn,
               clock_fn=clock_fn, wall_budget_seconds=None, checkpoint_path=None,
               trade_date=day, question=question)
    def acquire():
        return _fetch_query(run, question, unit_prefix='cap', max_pages=max_requests, first_unit=False)
    try:
        if live:
            import requests
            # Guard pywencai's individual HTTP sends, including its internal retries.
            with _HTTP_LOCK:
                original = requests.sessions.Session.send
                owner_thread = threading.get_ident()
                def guarded(session, request, **kwargs):
                    if threading.get_ident() != owner_thread:
                        return original(session, request, **kwargs)
                    if circuit_open[0]:
                        raise RuntimeError('HTTP 403 circuit open')
                    throttle()
                    response = original(session, request, **kwargs)
                    if response.status_code == 403:
                        circuit_open[0] = True
                        raise RuntimeError('HTTP 403')
                    return response
                requests.sessions.Session.send = guarded
                try:
                    outcome = acquire()
                finally:
                    requests.sessions.Session.send = original
        else:
            outcome = acquire()
    except Exception as exc:
        blocked = circuit_open[0] or bool(re.search('403|forbidden', str(exc), re.I))
        return _failure('http_403' if blocked else 'upstream_error',
                        retryable=not blocked, missing_pages=[1])
    if circuit_open[0]:
        return _failure('http_403', retryable=False)
    total = outcome.total
    expected = math.ceil(total / page_size) if total and total > 0 else 0
    missing = [p for p in range(1, expected + 1) if p not in outcome.pages or
               len(outcome.pages[p]) < min(page_size, total - (p - 1) * page_size)]
    error = str(outcome.error or outcome.stop_reason or '')
    if re.search('403|forbidden', error, re.I):
        return _failure('http_403', retryable=False, missing_pages=missing)
    if not total or outcome.error or outcome.stop_reason or outcome.truncated or missing or len(outcome.rows) < total * MIN_COVERAGE:
        return _failure('incomplete_pages', missing_pages=missing)
    suffix = day.replace('-', '')
    headers = {'float_cap_yuan': f'a股市值(不含限售股)[{suffix}]',
               'total_cap_yuan': f'总市值[{suffix}]',
               'a_total_cap_yuan': f'a股市值(含限售股)[{suffix}]'}
    rows = {}
    for raw in outcome.rows:
        cap_headers = [str(k) for k in raw if '市值' in str(k) and re.search(r'\[\d{8}\]$', str(k))]
        if any(not h.endswith(f'[{suffix}]') for h in cap_headers) or any(headers[k] not in raw for k in ('float_cap_yuan', 'total_cap_yuan')):
            return _failure('date_mismatch')
        symbol = _symbol_of_raw(raw)
        if not symbol or symbol in rows:
            return _failure('invalid_or_duplicate_symbol')
        rows[symbol] = {'symbol': symbol, **{key: _number(raw.get(col)) for key, col in headers.items()}, 'cap_as_of': day}
    valid = {s for s, row in rows.items() if row['float_cap_yuan'] is not None and row['total_cap_yuan'] is not None}
    if len(valid & universe) < len(universe) * MIN_COVERAGE:
        return _failure('stock_universe_coverage')
    quality = {'float_gt_total_count': sum(r['float_cap_yuan'] is not None and r['total_cap_yuan'] is not None and r['float_cap_yuan'] > r['total_cap_yuan'] for r in rows.values()),
               'universe_count': len(universe), 'universe_covered': len(valid & universe), 'reported_total': total}
    available_at = shanghai_seconds_ceil((now_fn or (lambda: datetime.now(ZoneInfo('Asia/Shanghai'))))())
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    meta = {'quality': quality, 'available_at': available_at, 'version': f'{day}-{digest}', 'missing_pages': []}
    result = {'rows': rows, 'meta': meta, 'error': None, 'retryable': False}
    if redis_client is not None:
        try:
            publish_stock_caps(day, redis_client, result)
        except Exception:
            return _failure('publish_failed')
    return result


class _MetadataPipeline:
    def __init__(self, pipeline, quality):
        self.pipe, self.quality = pipeline, quality
    def __getattr__(self, name):
        return getattr(self.pipe, name)
    def set(self, key, value, **kwargs):
        meta = json.loads(value)
        meta['quality'] = self.quality
        return self.pipe.set(key, json.dumps(meta, sort_keys=True, separators=(',', ':')), **kwargs)


class _MetadataRedis:
    def __init__(self, redis, quality):
        self.redis, self.quality = redis, quality
    def __getattr__(self, name):
        return getattr(self.redis, name)
    def pipeline(self):
        return _MetadataPipeline(self.redis.pipeline(), self.quality)


def publish_stock_caps(trade_date, redis_client, result):
    if result.get('error') or not result.get('rows'):
        raise ValueError('incomplete_stock_caps')
    meta = result['meta']
    write_day_view(_MetadataRedis(redis_client, meta['quality']), 'stock_cap', trade_date, result['rows'],
                   source='wencai', version=meta['version'], available_at=meta['available_at'], fields=FIELDS)


def read_stock_caps(trade_date, redis_client):
    try:
        value = redis_client.get(meta_key('stock_cap', trade_date))
        if not value:
            return {'rows': {}, 'meta': {}, 'reason': 'metadata_missing'}
        meta = json.loads(value)
        if meta.get('status') != 'COMPLETE':
            return {'rows': {}, 'meta': meta, 'reason': 'metadata_incomplete'}
        raw = redis_client.hgetall(view_key('stock_cap', trade_date))
        rows = {(k.decode() if isinstance(k, bytes) else k): json.loads(v) for k, v in raw.items()}
        if len(rows) != meta.get('row_count'):
            return {'rows': {}, 'meta': meta, 'reason': 'row_count_mismatch'}
        return {'rows': rows, 'meta': meta, 'reason': None}
    except Exception:
        return {'rows': {}, 'meta': {}, 'reason': 'read_error'}
