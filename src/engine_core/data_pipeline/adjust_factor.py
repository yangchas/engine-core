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
