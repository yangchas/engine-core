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
    """TD event storage; U2 is unresolved, so replacement is temporary and non-atomic."""
    def __init__(self, store):
        from .td_sink import validate_database_name
        self.store = store
        self.database = validate_database_name(store.database)

    def ensure_schema(self):
        db = self.database
        self.store._run(f'CREATE STABLE IF NOT EXISTS {db}.adjust_factor_v1 '
                        '(ts TIMESTAMP, fore_adjust_factor DOUBLE, back_adjust_factor DOUBLE, '
                        'adjust_factor DOUBLE, version VARCHAR(24)) TAGS (symbol VARCHAR(8))')
        self.store._run(f'CREATE TABLE IF NOT EXISTS {db}.adjust_factor_days '
                        '(ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), '
                        'business_status VARCHAR(16), row_count INT, sha256 VARCHAR(64), '
                        'available_at VARCHAR(40), quality VARCHAR(4000), missing_whitelist VARCHAR(4000))')

    @staticmethod
    def _symbol(symbol):
        import re
        if not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}', symbol):
            raise ValueError('bad_symbol:' + str(symbol))
        return symbol

    def _all_events(self, symbol):
        symbol = self._symbol(symbol)
        rows = self.store._run(f'SELECT ts, fore_adjust_factor, back_adjust_factor, adjust_factor, version '
                               f'FROM {self.database}.adjust_factor_{symbol} ORDER BY ts', missing_ok=True, strict=True)
        return [{'dividOperateDate': str(ts)[:10], 'foreAdjustFactor': str(fore),
                 'backAdjustFactor': str(back), 'adjustFactor': str(adjust), 'version': str(version)}
                for ts, fore, back, adjust, version in rows]

    def read_events(self, symbol):
        rows = self._all_events(symbol)
        versions = {row['version'] for row in rows}
        if not versions:
            return [], 'NO_EVENTS'
        latest = max(versions)
        return [row for row in rows if row['version'] == latest], ('MIXED_VERSION' if len(versions) > 1 else 'FORMAL')

    def replace_snapshot(self, symbol, rows, version):
        from .td_sink import _literal, trade_day_ts_us
        import re
        symbol = self._symbol(symbol)
        if not re.fullmatch(r'\d{14}-[0-9a-f]{8}', version):
            raise ValueError('bad_version')
        old = self._all_events(symbol)
        values = []
        days = set()
        for row in rows:
            day = date.fromisoformat(row['dividOperateDate']).isoformat()
            if day in days:
                raise ValueError('duplicate_event_date')
            days.add(day)
            factors = [Decimal(str(row[key])) for key in ('foreAdjustFactor', 'backAdjustFactor', 'adjustFactor')]
            if any(not value.is_finite() or value <= 0 for value in factors):
                raise ValueError('invalid_factor')
            values.append(f"({trade_day_ts_us(day)}, {', '.join(str(value) for value in factors)}, {_literal(version)})")
        db = self.database
        if values:
            self.store._run(f"INSERT INTO {db}.adjust_factor_{symbol} USING {db}.adjust_factor_v1 "
                            f"TAGS('{symbol}') VALUES " + ' '.join(values))
        for day in sorted({row['dividOperateDate'] for row in old} - days):
            self.store._run(f'DELETE FROM {db}.adjust_factor_{symbol} WHERE ts = {trade_day_ts_us(day)}', missing_ok=True)

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
        normalized.append([row['dividOperateDate']] + [str(Decimal(str(row[key])).normalize())
                          for key in ('foreAdjustFactor', 'backAdjustFactor', 'adjustFactor')])
    payload = json.dumps(sorted(normalized), separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()


def run(request, root, *, store=None, fetcher=None, sleep=None, clock=None, dry_run=False):
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
        if mode != 'incremental':
            raise ValueError('unsupported mode')
        detail['mode'] = mode
    except (KeyError, TypeError, ValueError) as exc:
        detail['message'] = str(exc)
        return _reply(64, 'CONFIG_ERROR', detail)
    if dry_run:
        return _reply(0, detail=detail)
    owned = store is None
    facts = store if store is not None else AdjustFactorStore(TaosStore(core_td_database()))
    try:
        facts.ensure_schema()
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
            old, _quality = facts.read_events(symbol)
            rows = list((fetcher or _fetch_live)(symbol))
            old_hash, new_hash = _snapshot_digest(old), _snapshot_digest(rows)
            snapshot_hashes.append(symbol + ':' + new_hash)
            if old_hash == new_hash:
                detail['unchanged'] += 1
            version = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S') + '-' + new_hash[:8]
            facts.replace_snapshot(symbol, rows, version)
            detail['snapshots_replaced'] += 1
            if len(detail['samples']) < 20:
                detail['samples'].append({'symbol': symbol, 'old_rows': len(old), 'new_rows': len(rows),
                                          'old_sha256': old_hash, 'new_sha256': new_hash})
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
        if owned:
            facts.store.close()
