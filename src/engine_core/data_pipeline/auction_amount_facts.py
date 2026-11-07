"""Daily, slot-specific auction amount facts in the Core TD database."""

import re
from collections.abc import Mapping

from ..plate_amount_baseline import MIN_FINAL_SYMBOLS
from .td_sink import INSERT_CHUNK_ROWS, _check_date, _literal, trade_day_ts_us, validate_database_name

SUPER_TABLE = 'auction_amount_facts_v1'
META_TABLE = 'auction_amount_facts_days'


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

    def complete_days(self, days, slot):
        result = set()
        for day in days:
            _, marker_ts = self._key(day, slot)
            rows = self.store._run(f'SELECT row_count FROM {self.database}.{META_TABLE} '
                                   f'WHERE ts = {marker_ts}', missing_ok=True, strict=True)
            if rows and rows[0][0] is not None and int(rows[0][0]) >= MIN_FINAL_SYMBOLS:
                result.add(day)
        return result

    def read_days(self, days, slot):
        result = {}
        for day in days:
            ts, _ = self._key(day, slot)
            rows = self.store._run(f'SELECT symbol, amount_yuan FROM {self.database}.{SUPER_TABLE} '
                                   f"WHERE ts = {ts} AND slot = '{slot}'", strict=True)
            result[day] = {symbol: int(amount) for symbol, amount in rows}
        return result

    def write_day(self, day, slot, amounts: Mapping[str, int], source_note: str):
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
        previous = self.read_days([day], slot)[day]
        # TD DELETE only permits timestamp predicates; slot-specific child tables
        # keep the other slot intact, including when symbols disappear on replacement.
        for symbol in previous:
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
    from ..plate_amount_baseline import _td_connect, _read_auction_rows, _read_0945_slice, _day, _symbol
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


def acquire_day(day, *, reader, store, slots=('0925', '0945')):
    incomplete = {}
    for slot in slots:
        if day in store.complete_days([day], slot):
            continue
        amounts = reader(day, slot)
        if len(amounts) < MIN_FINAL_SYMBOLS:
            incomplete[slot] = len(amounts)
            continue
        store.write_day(day, slot, amounts, 'market_data1')
    return incomplete


def _reply(code, error_code='', detail=None):
    status = {0: 'ok', 10: 'degraded', 20: 'not_applicable', 30: 'failed', 40: 'failed', 64: 'failed'}[code]
    return code, {'contract': 1, 'status': status, 'error_code': error_code,
                  'reason': error_code, 'retryable': code == 30, 'detail': detail or {}}


def run(request, root, *, reader=None, store=None, dry_run=False):
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
    detail = {'completed': [], 'remaining': [], 'incomplete': {}, 'expired': [], 'queries': []}
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
            incomplete = acquire_day(day, reader=reader or _read_source, store=facts, slots=slots)
            if incomplete:
                detail['incomplete'][day] = incomplete
            else:
                detail['completed'].append(day)
                detail['remaining'].remove(day)
        return _reply(10 if detail['incomplete'] else 0, 'INCOMPLETE' if detail['incomplete'] else '', detail)
    except Exception as exc:
        if isinstance(exc, TdResourceExhausted) or td_memory_exhausted(exc):
            return _reply(30, 'TD_RESOURCE_EXHAUSTED', detail)
        if isinstance(exc, TdUnavailable):
            return _reply(30, exc.error_code, detail)
        raise
