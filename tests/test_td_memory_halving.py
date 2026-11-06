import re

import pytest

from engine_core.data_pipeline.td_sink import DATA_COLUMNS, TD_ERRNO_MEMORY_EXHAUSTED, TaosStore
from engine_core.plate_amount_baseline import _auction_slice


class MemoryErrorFromTD(Exception):
    errno = TD_ERRNO_MEMORY_EXHAUSTED


class Cursor:
    def __init__(self, rows, limit):
        self.rows = rows
        self.limit = limit

    def cursor(self):
        return self

    def close(self):
        pass

    def execute(self, sql):
        if 'SELECT DISTINCT symbol' in sql:
            self.result = [(s,) for s in reversed(sorted(self.rows))]
            return
        match = re.search(r'symbol IN \(([^)]*)\)', sql)
        symbols = re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)) if match else list(self.rows)
        if len(symbols) > self.limit:
            raise MemoryErrorFromTD('memory limit')
        self.result = [row for symbol in symbols for row in self.rows.get(symbol, [])]

    def fetchall(self):
        return self.result


def test_memory_exhaustion_halves_symbols_for_both_queries():
    symbols = [f'{i:06d}' for i in range(5)]
    daily = {s: [(s, *([i] * len(DATA_COLUMNS)), 'v1')] for i, s in enumerate(symbols)}
    store = TaosStore('core_data', connect=lambda: Cursor(daily, 2))
    expected = {s: {**dict(zip(DATA_COLUMNS, row[0][1:-1])), 'version': 'v1'} for s, row in daily.items()}
    assert store.read_day_rows('2026-11-06') == expected
    assert store.read_day_rows('2026-11-06', symbols[::-1]) == expected
    auction = {s: [(f'2026-11-06 09:25:0{i}', s, '20261106', '0925', i)] for i, s in enumerate(symbols)}
    assert sorted(_auction_slice(Cursor(auction, 2), ['2026-11-06'])) == sorted(r for rows in auction.values() for r in rows)
    with pytest.raises(Exception):
        _auction_slice(Cursor(auction, 0), ['2026-11-06'])
