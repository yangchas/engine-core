"""Read a Baostock adjustment factor without inventing missing history."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation


def factor_for(events, day):
    """Return the most recent event's factor and its quality for a date.

    An empty response does not establish that the security never had an event.
    Invalid or missing event fields remain missing rather than becoming one.
    """
    try:
        target = date.fromisoformat(str(day))
        if not events:
            return None, 'NO_EVENTS'
        candidates = [(date.fromisoformat(str(row['dividOperateDate'])), row)
                      for row in events]
        eligible = [item for item in candidates if item[0] <= target]
        if not eligible:
            return None, 'BEFORE_FIRST_EVENT'
        _, row = max(eligible, key=lambda item: item[0])
        value = Decimal(str(row['foreAdjustFactor']))
        if not value.is_finite() or value <= 0:
            return None, 'INVALID_FACTOR'
        result = float(value)
        if result == float('inf') or result == 0:
            return None, 'INVALID_FACTOR'
        return result, 'FORMAL'
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        return None, 'INVALID_FACTOR'


class AdjustFactorStore:
    """One atomic snapshot row per symbol, timestamped at processing time."""
    def __init__(self, store):
        from .td_sink import validate_database_name
        self.store = store
        self.database = validate_database_name(store.database)

    def ensure_schema(self):
        db = self.database
        self.store._run(f'CREATE STABLE IF NOT EXISTS {db}.adjust_factor_v1 '
                        '(ts TIMESTAMP, version VARCHAR(24), event_count INT, sha256 VARCHAR(64), '
                        'events_json VARCHAR(60000)) TAGS (symbol VARCHAR(8))')
        schema = self.store._run(f'DESCRIBE {db}.adjust_factor_v1', strict=True)
        if [str(row[0]).lower() for row in schema] != ['ts', 'version', 'event_count', 'sha256', 'events_json', 'symbol']:
            raise ValueError('adjust_factor_v1_incompatible_schema')
        self.store._run(f'CREATE TABLE IF NOT EXISTS {db}.adjust_factor_days '
                        '(ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), '
                        'business_status VARCHAR(16), row_count INT, sha256 VARCHAR(64), '
                        'available_at VARCHAR(40), quality VARCHAR(4000), missing_whitelist VARCHAR(4000))')

    def listing_symbols(self, day):
        from .reference_snapshot_td import ReferenceSnapshotStore
        reference = ReferenceSnapshotStore(database=self.database)
        try:
            snapshot = reference.read_latest('stock_listing')
        finally:
            reference.close()
        if snapshot is None:
            return set(self.store.read_day_rows(day)), 'fallback'
        symbols = set()
        for row in snapshot['rows']:
            ipo, out = row.get('ipo_date'), row.get('out_date')
            if ipo and ipo <= day and (not out or day < out):
                symbol = str(row.get('symbol', '')).split('.')[-1]
                symbols.add(self._symbol(symbol))
        return symbols, 'stock_listing'

    @staticmethod
    def _symbol(symbol):
        import re
        if not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}', symbol):
            raise ValueError('bad_symbol:' + str(symbol))
        return symbol

    @staticmethod
    def _decode(snapshot):
        import json
        version, count, digest, payload = snapshot
        entries = json.loads(payload)
        if len(entries) != count:
            raise ValueError('adjust_factor_event_count_mismatch')
        rows = [{'dividOperateDate': x['date'], 'foreAdjustFactor': x['fore'],
                 'backAdjustFactor': x['back'], 'adjustFactor': x['adjust']} for x in entries]
        if _snapshot_digest(rows) != digest:
            raise ValueError('adjust_factor_sha256_mismatch')
        return rows, 'FORMAL' if rows else 'NO_EVENTS'

    def read_events(self, symbol):
        symbol = self._symbol(symbol)
        rows = self.store._run(f'SELECT version, event_count, sha256, events_json '
                               f'FROM {self.database}.adjust_factor_{symbol} ORDER BY ts DESC LIMIT 1',
                               missing_ok=True, strict=True)
        return self._decode(rows[0]) if rows else ([], 'UNPROCESSED')

    def read_events_many(self, symbols):
        from .td_sink import _literal
        result = {}
        symbols = sorted({self._symbol(s) for s in symbols})
        for start in range(0, len(symbols), 200):
            batch = symbols[start:start + 200]
            sql = (f'SELECT symbol, LAST_ROW(version), LAST_ROW(event_count), LAST_ROW(sha256), '
                   f'LAST_ROW(events_json) FROM {self.database}.adjust_factor_v1 WHERE symbol IN ('
                   + ', '.join(_literal(s) for s in batch) + ')')
            rows, _ = self.store._day_scan(sql, suffix=' GROUP BY tbname, symbol', strict=True)
            for symbol, *snapshot in rows:
                result[str(symbol)] = self._decode(snapshot)
        return result

    def replace_snapshot(self, symbol, rows, version):
        from .td_sink import _literal
        import json, re, time
        symbol = self._symbol(symbol)
        if not re.fullmatch(r'\d{14}-[0-9a-f]{8}', version):
            raise ValueError('bad_version')
        entries = []
        for row in rows:
            day = date.fromisoformat(row['dividOperateDate']).isoformat()
            factors = [str(row[key]) for key in ('foreAdjustFactor', 'backAdjustFactor', 'adjustFactor')]
            if any(not Decimal(v).is_finite() or Decimal(v) <= 0 for v in factors):
                raise ValueError('invalid_factor')
            entries.append(dict(zip(('date', 'fore', 'back', 'adjust'), [day] + factors)))
        entries.sort(key=lambda x: x['date'])
        if len({x['date'] for x in entries}) != len(entries):
            raise ValueError('duplicate_event_date')
        payload = json.dumps(entries, ensure_ascii=False, separators=(',', ':'))
        if len(payload.encode('utf-8')) > 60000:
            raise ValueError('adjust_factor_events_json_exceeds_60000_bytes')
        old = self.store._run(f'SELECT ts FROM {self.database}.adjust_factor_{symbol} ORDER BY ts DESC LIMIT 1', missing_ok=True, strict=True)
        ts = time.time_ns() // 1000
        if old:
            previous = old[0][0]
            if hasattr(previous, 'timestamp'):
                previous = int(previous.timestamp() * 1000000)
            ts = max(ts, int(previous) + 1)
        # KEEP eventually expires even the latest row; schedule periodic full refresh later.
        self.store._run(f'INSERT INTO {self.database}.adjust_factor_{symbol} USING {self.database}.adjust_factor_v1 '
                        f"TAGS('{symbol}') (ts, version, event_count, sha256, events_json) VALUES "
                        f'({ts}, {_literal(version)}, {len(entries)}, {_literal(_snapshot_digest(rows))}, {_literal(payload)})')

    def read_day_marker(self, day):
        from .td_sink import trade_day_ts_us
        rows = self.store._run(f'SELECT trade_date, version, business_status, row_count, sha256, '
                               f'available_at, quality, missing_whitelist FROM {self.database}.adjust_factor_days '
                               f'WHERE ts = {trade_day_ts_us(day)}', missing_ok=True, strict=True)
        return dict(zip(('trade_date', 'version', 'business_status', 'row_count', 'sha256',
                         'available_at', 'quality', 'missing_whitelist'), rows[0])) if rows else None

    def invalidate_day_marker(self, day):
        from .td_sink import trade_day_ts_us
        self.store._run(f'DELETE FROM {self.database}.adjust_factor_days WHERE ts = {trade_day_ts_us(day)}',
                        missing_ok=True)

    def write_day_marker(self, day, version, business_status, row_count, sha256, available_at, quality, missing_whitelist='[]'):
        from .td_sink import _literal, trade_day_ts_us
        if len(quality) > 4000 or len(missing_whitelist) > 4000:
            raise ValueError('marker_too_large')
        values = [day, version, business_status, row_count, sha256, available_at, quality, missing_whitelist]
        self.store._run(f'INSERT INTO {self.database}.adjust_factor_days VALUES '
                        f"({trade_day_ts_us(day)}, " + ', '.join(_literal(value) for value in values) + ')')


def detect_suspected(day_rows, prev_rows):
    """Find relative pre-close differences; missing or invalid prices are not zero."""
    result = set()
    for symbol, row in day_rows.items():
        try:
            previous = Decimal(str(prev_rows[symbol]['close']))
            current = Decimal(str(row['pre_close']))
            if (previous.is_finite() and current.is_finite() and previous > 0 and current > 0
                    and abs(current - previous) / previous > Decimal('0.000001')):
                result.add(symbol)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return result


def _fetch_live(symbol):
    import contextlib
    import io
    import baostock
    from .stock_listing import BaostockLoginError
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            login = baostock.login()
        except Exception as exc:
            raise BaostockLoginError(str(exc)) from exc
        if login.error_code != '0':
            raise RuntimeError('baostock_login_failed')
        try:
            code = ('sh.' if symbol.startswith(('5', '6', '9')) else 'sz.') + symbol
            result = baostock.query_adjust_factor(code=code, start_date='1990-01-01', end_date=date.today().isoformat())
            if result.error_code != '0':
                raise RuntimeError('baostock_range_failed')
            fields = list(result.fields)
            required = {'code', 'dividOperateDate', 'foreAdjustFactor', 'backAdjustFactor', 'adjustFactor'}
            if not required.issubset(fields):
                raise ValueError('adjust_factor_fields_missing')
            rows = []
            while result.next():
                rows.append(dict(zip(fields, result.get_row_data())))
            if result.error_code != '0':
                raise RuntimeError('baostock_range_failed')
            return rows
        finally:
            baostock.logout()


def _snapshot_digest(rows):
    import hashlib
    import json
    normalized = []
    for row in rows:
        normalized.append([row['dividOperateDate']] + [str(row[key])
                          for key in ('foreAdjustFactor', 'backAdjustFactor', 'adjustFactor')])
    payload = json.dumps(sorted(normalized), separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()


def _lease_factory(mode, day):
    import os
    import redis
    from .lease import RedisLease
    url = os.getenv('CORE_REDIS_URL') or os.getenv('REDIS_URL') or 'redis://127.0.0.1:6379/0'
    client = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
    # One shared lock prevents overlap across modes and dates.
    return RedisLease(client, 'core:lease:fact:adjust_factor:all', ttl_seconds=300)


def run(request, root, *, store=None, fetcher=None, sleep=None, clock=None, dry_run=False, lease_factory=None):
    import time
    from datetime import datetime, timezone
    from .auction_amount_facts import _reply, _registered_failure, core_td_database
    from .stock_listing import BaostockLoginError
    from .td_sink import TaosStore, TdResourceExhausted, td_memory_exhausted
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    started = clock()
    detail = {'mode': None, 'suspected_total': 0, 'requested': 0, 'snapshots_replaced': 0,
              'unchanged': 0, 'failed_total': 0, 'aborted': False, 'elapsed_seconds': 0,
              'listing_source': None, 'samples': []}
    try:
        day = request['trade_date']
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('invalid trade date')
        mode = request['args']['mode']
        if mode not in ('incremental', 'bootstrap'):
            raise ValueError('unsupported mode')
        detail['mode'] = mode
    except (KeyError, TypeError, ValueError) as exc:
        detail['message'] = str(exc)
        return _reply(64, 'CONFIG_ERROR', detail)
    if dry_run:
        return _reply(0, detail=detail)
    owned = store is None
    lease = None
    acquired = False
    facts = store if store is not None else AdjustFactorStore(TaosStore(core_td_database()))
    try:
        from .auction_amount_facts import LeaseUnavailable
        try:
            lease = (lease_factory or _lease_factory)(mode, day)
            acquired = lease.acquire()
        except Exception as exc:
            raise LeaseUnavailable(type(exc).__name__) from exc
        if not acquired:
            detail['skipped_locked'] = True
            return _reply(10, 'INCOMPLETE', detail)
        facts.ensure_schema()
        if mode == 'incremental':
            if owned:
                from .trade_calendar import TradeCalendarStore
                previous_day = TradeCalendarStore(root).previous_trade_date(day).isoformat()
                read_day = facts.store.read_day_rows
            else:
                previous_day = facts.previous_day(day)
                read_day = facts.read_day_rows
            current, previous = read_day(day), read_day(previous_day)
            if not current or not previous:
                raise RuntimeError('daily_kline_missing')
            symbols = sorted(detect_suspected(current, previous))
            detail['suspected_total'] = len(symbols)
        else:
            universe, detail['listing_source'] = facts.listing_symbols(day)
            symbols = []
            for symbol in sorted(universe):
                _, quality = facts.read_events(symbol)
                if quality == 'UNPROCESSED':
                    symbols.append(symbol)
            detail['bootstrap_remaining'] = len(symbols)
            detail['bootstrap_processed'] = 0
        facts.invalidate_day_marker(day)
        last_request = None
        snapshot_hashes = []
        for symbol in symbols:
            if last_request is not None:
                delay = 1.0 - (clock() - last_request)
                if delay > 0:
                    sleep(delay)
            last_request = clock()
            detail['requested'] += 1
            old, old_quality = facts.read_events(symbol)
            rows = list((fetcher or _fetch_live)(symbol))
            try:
                if not lease.renew():
                    raise LeaseUnavailable('lease_lost')
            except LeaseUnavailable:
                raise
            except Exception as exc:
                raise LeaseUnavailable(type(exc).__name__) from exc
            old_hash, new_hash = _snapshot_digest(old), _snapshot_digest(rows)
            snapshot_hashes.append(symbol + ':' + new_hash)
            if old_quality != 'UNPROCESSED' and old_hash == new_hash:
                detail['unchanged'] += 1
            else:
                version = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S') + '-' + new_hash[:8]
                facts.replace_snapshot(symbol, rows, version)
                detail['snapshots_replaced'] += 1
            if mode == 'bootstrap':
                detail['bootstrap_processed'] += 1
                if detail['bootstrap_processed'] % 100 == 0:
                    import logging
                    logging.getLogger(__name__).info('adjust_factor bootstrap processed=%s total=%s', detail['bootstrap_processed'], len(symbols))
            if len(detail['samples']) < 20:
                detail['samples'].append({'symbol': symbol, 'old_rows': len(old), 'new_rows': len(rows),
                                          'old_sha256': old_hash, 'new_sha256': new_hash})
        try:
            if not lease.renew():
                raise LeaseUnavailable('lease_lost')
        except LeaseUnavailable:
            raise
        except Exception as exc:
            raise LeaseUnavailable(type(exc).__name__) from exc
        import hashlib
        digest = hashlib.sha256('\n'.join(snapshot_hashes).encode()).hexdigest()
        version = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S') + '-' + digest[:8]
        facts.write_day_marker(day, version, 'PARTIAL', detail['snapshots_replaced'], digest,
                               datetime.now(timezone.utc).isoformat(), 'PARTIAL')
        detail['elapsed_seconds'] = round(clock() - started, 3)
        return _reply(0, detail=detail)
    except Exception as exc:
        detail['failed_total'] += 1
        detail['aborted'] = True
        detail['elapsed_seconds'] = round(clock() - started, 3)
        if isinstance(exc, LeaseUnavailable):
            return _registered_failure('REDIS_UNAVAILABLE', detail)
        if isinstance(exc, BaostockLoginError):
            return _registered_failure('BAOSTOCK_LOGIN_ERROR', detail)
        if isinstance(exc, TdResourceExhausted) or td_memory_exhausted(exc):
            return _registered_failure('TD_RESOURCE_EXHAUSTED', detail)
        if isinstance(exc, RuntimeError):
            code = {'baostock_login_failed': 'BAOSTOCK_LOGIN_FAILED',
                    'baostock_range_failed': 'BAOSTOCK_RANGE_FAILED'}.get(str(exc))
            if code:
                return _registered_failure(code, detail)
        raise
    finally:
        if lease is not None and acquired:
            lease.release()
        if owned:
            facts.store.close()
