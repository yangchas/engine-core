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
