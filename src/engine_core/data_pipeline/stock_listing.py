"""Acquire and query versioned Baostock listing windows."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import time
from datetime import date, datetime, timezone

from .auction_amount_facts import _registered_failure, _reply
from .reference_snapshot_td import ReferenceSnapshotStore
from .td_sink import TdResourceExhausted, td_memory_exhausted

DATASET = 'stock_listing'
SOURCE = 'baostock'


def fetch(client):
    """Read one complete stock-basic response from an already logged-in client."""
    observed_at = datetime.now(timezone.utc).isoformat()
    result = client.query_stock_basic(code='')
    if result.error_code != '0':
        raise RuntimeError('baostock_stock_basic_failed')
    fields = list(result.fields)
    required = {'code', 'code_name', 'ipoDate', 'outDate', 'type', 'status'}
    if not required.issubset(fields):
        raise ValueError('stock_basic_fields_missing')
    rows = []
    while result.next():
        raw = dict(zip(fields, result.get_row_data()))
        if raw['type'] == '1':
            rows.append({'symbol': raw['code'], 'code_name': raw['code_name'],
                         'ipo_date': raw['ipoDate'], 'out_date': raw['outDate'] or None,
                         'status': raw['status'], 'observed_at': observed_at})
    if result.error_code != '0':
        raise RuntimeError('baostock_stock_basic_failed')
    return rows


def _fetch_live():
    import baostock
    with contextlib.redirect_stdout(io.StringIO()):
        login = baostock.login()
        if login.error_code != '0':
            raise RuntimeError('baostock_login_failed')
        try:
            return fetch(baostock)
        finally:
            baostock.logout()


def listed_on(rows, day):
    day = date.fromisoformat(str(day))
    return {row['symbol'] for row in rows
            if date.fromisoformat(row['ipo_date']) <= day
            and (not row['out_date'] or day < date.fromisoformat(row['out_date']))}


def read_active(store):
    return store.read_latest(DATASET)


def _digest(rows):
    # Observation time changes on every fetch but does not change listing facts.
    facts = [{k: v for k, v in row.items() if k not in ('observed_at', 'trade_date')} for row in rows]
    payloads = sorted(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':')) for row in facts)
    return hashlib.sha256('\n'.join(payloads).encode('utf-8')).hexdigest()


def run(trade_date, *, store=None, fetcher=None, min_rows=3000):
    started = time.monotonic()
    detail = {'rows': 0, 'types': {'1': 0}, 'delisted_count': 0, 'version': None, 'elapsed': 0}
    owned = store is None
    try:
        if date.fromisoformat(trade_date).isoformat() != trade_date:
            raise ValueError('invalid trade date')
        rows = list((fetcher or _fetch_live)())
        detail.update(rows=len(rows), types={'1': len(rows)},
                      delisted_count=sum(bool(row.get('out_date')) for row in rows))
        if len(rows) < min_rows:
            detail['message'] = 'stock listing row count below minimum'
            detail['elapsed'] = round(time.monotonic() - started, 3)
            return _registered_failure('QUALITY_INVALID', detail)
        symbols = set()
        for row in rows:
            if not row.get('symbol') or not row.get('ipo_date') or row.get('status') not in ('0', '1') or not row.get('observed_at'):
                raise ValueError('stock listing required field missing')
            date.fromisoformat(row['ipo_date'])
            if row.get('out_date'):
                date.fromisoformat(row['out_date'])
            if row['symbol'] in symbols:
                raise ValueError('stock listing duplicate symbol')
            symbols.add(row['symbol'])
        store = store if store is not None else ReferenceSnapshotStore()
        active = read_active(store)
        if active and active['rows'] and active['rows'][0].get('trade_date') == trade_date and _digest(active['rows']) == _digest(rows):
            detail['version'] = active['version']
            detail['elapsed'] = round(time.monotonic() - started, 3)
            return _reply(20, 'ALREADY_SATISFIED', detail)
        rows = [dict(row, trade_date=trade_date) for row in rows]
        available_at = datetime.now(timezone.utc).isoformat()
        version = store.begin(DATASET, source=SOURCE, available_at=available_at)
        store.write_and_activate(DATASET, version, rows, source=SOURCE, available_at=available_at,
                                 request_count=1, elapsed_ms=round((time.monotonic() - started) * 1000))
        detail['version'] = version
        detail['elapsed'] = round(time.monotonic() - started, 3)
        return _reply(0, detail=detail)
    except Exception as exc:
        detail['elapsed'] = round(time.monotonic() - started, 3)
        detail['message'] = f'{type(exc).__name__}: {str(exc)[:200]}'
        if isinstance(exc, TdResourceExhausted) or td_memory_exhausted(exc):
            return _registered_failure('TD_RESOURCE_EXHAUSTED', detail)
        return 1, {'contract': 1, 'status': 'failed', 'error_code': 'INTERNAL_ERROR',
                   'reason': 'INTERNAL_ERROR', 'retryable': True, 'detail': detail}
    finally:
        if owned and store is not None:
            store.close()
