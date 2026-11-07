"""Daily, slot-specific auction amount facts in the Core TD database."""

import re
from collections.abc import Mapping

from ..plate_amount_baseline import _day, _symbol
from .td_sink import td_memory_exhausted, td_statement_error
import os
from typing import Any, Sequence

MIN_FINAL_SYMBOLS = 4000
SLOT_0945_WINDOW_START = "09:30:01"
SLICE_DAYS = 5

def _td_connect():
    if not os.environ.get("TD_CONFIG"):
        raise RuntimeError("TD_CONFIG_required")
    from .td_sink import connect_td
    return connect_td("market_data1")


def _query_error(exc: Exception, reason: str) -> Exception:
    try:
        from taos.error import Error as TdDriverError
    except ImportError:
        TdDriverError = ()
    if isinstance(exc, TdDriverError):
        return td_statement_error(exc, reason)
    return RuntimeError(reason)


def _read_0945_slice(cursor: Any, days: Sequence[str], wanted: set[str],
                     symbols: tuple[str, str] | None = None, td_split_retry_depth: int = 0) -> list[dict[str, Any]]:
    first, last = min(days), max(days)
    symbol_filter = f' AND symbol >= "{symbols[0]}" AND symbol < "{symbols[1]}"' if symbols else ""
    try:
        cursor.execute("SELECT _wstart, symbol, LAST(amt_yuan) FROM market_data1.stock_tick_v2 "
                       f'WHERE ts >= "{first} 09:30:00" AND ts <= "{last} 09:45:00"{symbol_filter} '
                       "PARTITION BY symbol INTERVAL(15m, 1s)")
        fetched = cursor.fetchall()
    except Exception as exc:
        if td_split_retry_depth > 0 and td_memory_exhausted(exc):
            if len(days) > 1:
                middle = len(days) // 2
                return (_read_0945_slice(cursor, days[:middle], wanted, symbols, td_split_retry_depth - 1)
                        + _read_0945_slice(cursor, days[middle:], wanted, symbols, td_split_retry_depth - 1))
            low, high = (int(symbols[0]), int(symbols[1])) if symbols else (0, 1000000)
            if high - low > 1:
                middle = (low + high) // 2
                return (_read_0945_slice(cursor, days, wanted, (f"{low:06d}", f"{middle:06d}"), td_split_retry_depth - 1)
                        + _read_0945_slice(cursor, days, wanted, (f"{middle:06d}", f"{high:06d}"), td_split_retry_depth - 1))
        raise _query_error(exc, f"plate_amount_baseline_tick_slice {first}..{last}"
                           f"{' ' + symbols[0] + '-' + symbols[1] if symbols else ''}: {exc}") from exc
    out = []
    for window_start, symbol, amount in fetched:
        stamp = str(window_start)
        if stamp[11:19] != SLOT_0945_WINDOW_START or stamp[:10] not in wanted or amount is None:
            continue
        out.append({"trade_date": stamp[:10], "slot": "0945", "symbol": _symbol(symbol), "amount_yuan": amount})
    return out


def _read_auction_rows(cursor: Any, days: Sequence[str], td_split_retry_depth: int = 0) -> list[tuple]:
    """09:25 auction snapshots of ``days``; SLICE_DAYS per query, with optional bounded splitting.
    No ORDER BY: the caller keeps the latest snapshot per (day, symbol) itself, and sorting is what costs memory."""
    rows: list[tuple] = []
    for index in range(0, len(days), SLICE_DAYS):
        rows.extend(_auction_slice(cursor, list(days[index:index + SLICE_DAYS]), td_split_retry_depth=td_split_retry_depth))
    return rows


def _auction_slice(cursor: Any, days: list[str], symbols: tuple[int | None, int | None] | None = None, td_split_retry_depth: int = 0) -> list[tuple]:
    quoted = ",".join(f'"{day.replace("-", "")}"' for day in days)
    lo, hi = symbols if symbols is not None else (None, None)
    symbol_filter = (f" AND symbol >= '{lo:06d}'" if lo is not None else "")
    symbol_filter += f" AND symbol < '{hi:06d}'" if hi is not None else ""
    try:
        cursor.execute("SELECT ts, symbol, trade_date, auction_tag, match_amt_yuan "
                       "FROM market_data1.auction_snapshot_v2 "
                       f'WHERE trade_date IN ({quoted}) AND auction_tag="0925"{symbol_filter}')
        return list(cursor.fetchall())
    except Exception as exc:
        if td_split_retry_depth > 0 and td_memory_exhausted(exc):
            if len(days) > 1:
                middle = len(days) // 2
                return _auction_slice(cursor, days[:middle], symbols, td_split_retry_depth - 1) + _auction_slice(cursor, days[middle:], symbols, td_split_retry_depth - 1)
            low, high = (0 if lo is None else lo), (1000000 if hi is None else hi)
            if high - low > 1:
                middle = (low + high) // 2
                return (_auction_slice(cursor, days, (lo, middle), td_split_retry_depth - 1)
                        + _auction_slice(cursor, days, (middle, hi), td_split_retry_depth - 1))
        raise _query_error(exc, f"plate_amount_baseline_auction_query {days[0]}..{days[-1]}: {exc}") from exc



from .td_sink import INSERT_CHUNK_ROWS, _check_date, _literal, trade_day_ts_us, validate_database_name

SUPER_TABLE = 'auction_amount_facts_v1'
META_TABLE = 'auction_amount_facts_days'
# The window contract specifies 300 seconds; the observed 0945 query was 0.313s,
# but end-to-end per-slot duration remains unmeasured.
LEASE_TTL_SECONDS = 300


class AuctionAmountFacts:
    def __init__(self, store):
        self.store = store
        self.database = validate_database_name(store.database)

    def ensure_schema(self):
        db = self.database
        self.store._run(f'CREATE STABLE IF NOT EXISTS {db}.{SUPER_TABLE} '
                        '(ts TIMESTAMP, amount_yuan BIGINT) TAGS (symbol VARCHAR(8), slot VARCHAR(4))')
        self.store._run(f'CREATE TABLE IF NOT EXISTS {db}.{META_TABLE} '
                        '(ts TIMESTAMP, trade_date VARCHAR(10), slot VARCHAR(4), row_count INT, '
                        'source_note VARCHAR(1024), written_at TIMESTAMP)')

    @staticmethod
    def _key(day, slot):
        _check_date(day)
        if slot not in ('0925', '0945'):
            raise ValueError('AUCTION_FACTS_INVALID_SLOT:' + str(slot))
        ts = trade_day_ts_us(day)
        return ts, ts + (1 if slot == '0945' else 0)

    def _marker_count(self, day, slot):
        _, marker_ts = self._key(day, slot)
        rows = self.store._run(f'SELECT row_count FROM {self.database}.{META_TABLE} '
                               f'WHERE ts = {marker_ts}', missing_ok=True, strict=True)
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def complete_days(self, days, slot):
        return {day for day in days if (count := self._marker_count(day, slot)) is not None
                and count >= MIN_FINAL_SYMBOLS}

    def read_days(self, days, slot):
        result = {}
        for day in days:
            ts, _ = self._key(day, slot)
            before = self._marker_count(day, slot)
            if before is None or before < MIN_FINAL_SYMBOLS:
                continue
            rows = self.store._run(f'SELECT symbol, amount_yuan FROM {self.database}.{SUPER_TABLE} '
                                   f"WHERE ts = {ts} AND slot = '{slot}'", strict=True)
            if self._marker_count(day, slot) != before:
                raise RuntimeError(f'AUCTION_FACTS_CHANGED_DURING_READ:{day}:{slot}')
            result[day] = {symbol: int(amount) for symbol, amount in rows}
        return result

    def write_day(self, day, slot, amounts: Mapping[str, int], source_note: str):
        """Invalidate the marker before in-place replacement; no version column is added.

        This prevents new readers from accepting an incomplete day, but cannot
        provide snapshot isolation for readers concurrent with a replacement.
        """
        ts, marker_ts = self._key(day, slot)
        if len(amounts) < MIN_FINAL_SYMBOLS:
            raise ValueError(f'AUCTION_FACTS_INCOMPLETE:{day}:{slot}:{len(amounts)}')
        for symbol, amount in amounts.items():
            if not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}', symbol):
                raise ValueError('bad_symbol:' + str(symbol))
            if type(amount) is not int:
                raise ValueError('bad_amount:' + symbol)
        db = self.database
        # Invalidate the old completion marker before touching any data.
        self.store._run(f'DELETE FROM {db}.{META_TABLE} WHERE ts = {marker_ts}', missing_ok=True)
        previous = self.store._run(f'SELECT symbol, amount_yuan FROM {db}.{SUPER_TABLE} '
                                   f"WHERE ts = {ts} AND slot = '{slot}'", strict=True)
        # TD DELETE only permits timestamp predicates; slot-specific child tables
        # keep the other slot intact, including when symbols disappear on replacement.
        for symbol, _ in previous:
            self.store._run(f'DELETE FROM {db}.auction_amount_{slot}_{symbol} WHERE ts = {ts}', missing_ok=True)
        items = sorted(amounts.items())
        for start in range(0, len(items), INSERT_CHUNK_ROWS):
            parts = [f"{db}.auction_amount_{slot}_{symbol} USING {db}.{SUPER_TABLE} "
                     f"TAGS('{symbol}', '{slot}') VALUES ({ts}, {amount})"
                     for symbol, amount in items[start:start + INSERT_CHUNK_ROWS]]
            self.store._run('INSERT INTO ' + ' '.join(parts))
        self.store._run(f'INSERT INTO {db}.{META_TABLE} '
                        '(ts, trade_date, slot, row_count, source_note, written_at) VALUES '
                        f'({marker_ts}, {_literal(day)}, {_literal(slot)}, {len(items)}, {_literal(source_note)}, NOW)')
        return len(items)


def _query_description(day, slot):
    if slot == '0925':
        return ('SELECT ts, symbol, trade_date, auction_tag, match_amt_yuan '
                'FROM market_data1.auction_snapshot_v2 '
                f'WHERE trade_date IN ("{day.replace("-", "")}") AND auction_tag="0925"')
    return ('SELECT _wstart, symbol, LAST(amt_yuan) FROM market_data1.stock_tick_v2 '
            f'WHERE ts >= "{day} 09:30:00" AND ts <= "{day} 09:45:00" '
            'PARTITION BY symbol INTERVAL(15m, 1s)')


def _read_source(day, slot):
    conn = _td_connect()
    try:
        cursor = conn.cursor()
        if slot == '0945':
            return {row['symbol']: row['amount_yuan'] for row in _read_0945_slice(cursor, [day], {day})}
        latest = {}
        for ts, symbol, source_day, _, amount in _read_auction_rows(cursor, [day]):
            if _day(source_day) != day:
                continue
            symbol = _symbol(symbol)
            stamp = str(ts)
            if symbol not in latest or stamp >= latest[symbol][0]:
                latest[symbol] = (stamp, amount)
        return {symbol: amount for symbol, (_, amount) in latest.items() if amount is not None}
    finally:
        conn.close()


def _lease_factory(day, slot):
    import os
    import redis
    from .lease import RedisLease
    url = os.getenv('CORE_REDIS_URL') or os.getenv('REDIS_URL') or 'redis://127.0.0.1:6379/0'
    client = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
    return RedisLease(client, f'core:lease:fact:auction_amount_{slot}:{day}', ttl_seconds=LEASE_TTL_SECONDS)


class LeaseUnavailable(Exception):
    pass


def acquire_day(day, *, reader, store, slots=('0925', '0945'), lease_factory=None, skipped_locked=None):
    """Acquire per-slot leases; Redis errors fail closed and require retry, without reading the source."""
    incomplete = {}
    skipped_locked = skipped_locked if skipped_locked is not None else []
    for slot in slots:
        lease = (lease_factory or _lease_factory)(day, slot)
        try:
            acquired = lease.acquire()
        except Exception as exc:
            raise LeaseUnavailable(type(exc).__name__) from exc
        if not acquired:
            skipped_locked.append({'day': day, 'slot': slot})
            continue
        try:
            if day in store.complete_days([day], slot):
                continue
            amounts = reader(day, slot)
            if len(amounts) < MIN_FINAL_SYMBOLS:
                incomplete[slot] = len(amounts)
                continue
            try:
                renewed = lease.renew()
            except Exception as exc:
                raise LeaseUnavailable(type(exc).__name__) from exc
            if not renewed:
                raise LeaseUnavailable('lease_lost')
            store.write_day(day, slot, amounts, 'market_data1')
        finally:
            try:
                lease.release()
            except Exception as exc:
                raise LeaseUnavailable(type(exc).__name__) from exc
    return incomplete


def _reply(code, error_code='', detail=None):
    status = {0: 'ok', 10: 'degraded', 20: 'not_applicable', 30: 'failed', 40: 'failed', 64: 'failed'}[code]
    return code, {'contract': 1, 'status': status, 'error_code': error_code,
                  'reason': error_code, 'retryable': code == 30, 'detail': detail or {}}


def run(request, root, *, reader=None, store=None, dry_run=False, lease_factory=None):
    from datetime import date, datetime, timedelta
    from pathlib import Path
    from zoneinfo import ZoneInfo
    from ..ladder_daily_adapter import trading_days
    from .td_sink import TaosStore, TdResourceExhausted, TdUnavailable, td_memory_exhausted
    try:
        if not isinstance(request, Mapping):
            raise ValueError('request must be an object')
        max_days = request.get('max_days', 5)
        if type(max_days) is not int or max_days < 1:
            raise ValueError('invalid max_days')
        if request.get('available_at') is not None:
            datetime.fromisoformat(request['available_at'])
        if 'days' in request:
            if 'end_date' in request or 'window' in request or not isinstance(request['days'], list) or not request['days']:
                raise ValueError('invalid days')
            days = request['days']
        else:
            end = request['end_date']
            _check_date(end)
            window = request.get('window', 64)
            if type(window) is not int or window < 1:
                raise ValueError('invalid window')
            days = trading_days(Path(root).expanduser(), end)[-window:]
            if len(days) != window:
                raise ValueError('calendar window unavailable')
        for day in days:
            _check_date(day)
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError('invalid date')
        days = sorted(set(days), reverse=True)
    except (ValueError, TypeError, KeyError) as exc:
        return _reply(64, 'CONFIG_ERROR', {'message': str(exc)})
    facts = store if store is not None else AuctionAmountFacts(TaosStore('core_data'))
    detail = {'completed': [], 'remaining': [], 'incomplete': {}, 'expired': [], 'queries': [], 'skipped_locked': []}
    try:
        if not dry_run:
            facts.ensure_schema()
        complete = {slot: facts.complete_days(days, slot) for slot in ('0925', '0945')}
        missing = [day for day in days if any(day not in complete[s] for s in complete)]
        if not missing:
            return _reply(20, 'ALREADY_SATISFIED', detail)
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        expired = [day for day in missing if date.fromisoformat(day) < today - timedelta(days=60)]
        detail['expired'] = [{'day': day, 'reason': 'SOURCE_EXPIRED'} for day in expired]
        eligible = [day for day in missing if day not in expired]
        selected = eligible[:max_days]
        detail['remaining'] = list(eligible)
        if dry_run:
            detail['queries'] = [{'day': day, 'slot': slot, 'sql': _query_description(day, slot)}
                                 for day in selected for slot in complete if day not in complete[slot]]
            return _reply(0, detail=detail)
        for day in selected:
            slots = tuple(slot for slot in complete if day not in complete[slot])
            incomplete = acquire_day(day, reader=reader or _read_source, store=facts, slots=slots, lease_factory=lease_factory,
                                     skipped_locked=detail['skipped_locked'])
            if incomplete:
                detail['incomplete'][day] = incomplete
            elif not any(item['day'] == day for item in detail['skipped_locked']):
                detail['completed'].append(day)
                detail['remaining'].remove(day)
        return _reply(10 if detail['incomplete'] or detail['skipped_locked'] else 0,
                      'INCOMPLETE' if detail['incomplete'] or detail['skipped_locked'] else '', detail)
    except Exception as exc:
        if isinstance(exc, LeaseUnavailable):
            detail['lease_error'] = str(exc)
            return _reply(30, 'LEASE_UNAVAILABLE', detail)
        if isinstance(exc, TdResourceExhausted) or td_memory_exhausted(exc):
            return _reply(30, 'TD_RESOURCE_EXHAUSTED', detail)
        if isinstance(exc, TdUnavailable):
            return _reply(30, exc.error_code, detail)
        raise
