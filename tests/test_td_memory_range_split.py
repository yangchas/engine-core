import re

import pytest

from engine_core.data_pipeline.td_sink import DATA_COLUMNS, TD_ERRNO_MEMORY_EXHAUSTED, TaosStore
from engine_core.data_pipeline.auction_amount_facts import _auction_slice, _read_0945_slice


class MemoryExhausted(Exception):
    errno = TD_ERRNO_MEMORY_EXHAUSTED


class RangeCursor:
    def __init__(self, rows, limit):
        self.rows = rows
        self.limit = limit
        self.queries = []

    def cursor(self):
        return self

    def close(self):
        pass

    def execute(self, sql):
        self.queries.append(sql)
        assert 'DISTINCT symbol' not in sql
        lower = re.search(r"symbol\s*>=\s*['\"](\d+)['\"]", sql)
        upper = re.search(r"symbol\s*<\s*['\"](\d+)['\"]", sql)
        lo, hi = int(lower.group(1)) if lower else 0, int(upper.group(1)) if upper else 1000000
        if hi - lo > self.limit:
            raise MemoryExhausted('memory limit')
        self.result = [row for symbol, rows in self.rows.items() if lo <= int(symbol) < hi for row in rows]

    def fetchall(self):
        return self.result


def test_memory_exhaustion_splits_fixed_code_ranges():
    symbols = ['000000', '000001', '123456', '500000', '999999']
    daily = {s: [(s, *([i] * len(DATA_COLUMNS)), 'v1')] for i, s in enumerate(symbols)}
    cursor = RangeCursor(daily, 125000)
    expected = {s: {**dict(zip(DATA_COLUMNS, rows[0][1:-1])), 'version': 'v1'} for s, rows in daily.items()}
    assert TaosStore('core_data', connect=lambda: cursor).read_day_rows('2026-11-06') == expected
    auction = {s: [('2026-11-06 09:25:00', s, '20261106', '0925', i)] for i, s in enumerate(symbols)}
    cursor = RangeCursor(auction, 125000)
    assert sorted(_auction_slice(cursor, ['2026-11-06'], td_split_retry_depth=20)) == sorted(row for rows in auction.values() for row in rows)
    with pytest.raises(Exception) as failure:
        _auction_slice(RangeCursor(auction, 0), ['2026-11-06'], td_split_retry_depth=20)
    assert isinstance(failure.value.__cause__, MemoryExhausted)
    ticks = {s: [('2026-11-06 09:30:01', s, i)] for i, s in enumerate(symbols)}
    cursor = RangeCursor(ticks, 125000)
    expected_ticks = [{'trade_date': '2026-11-06', 'slot': '0945', 'symbol': s, 'amount_yuan': i}
                      for i, s in enumerate(symbols)]
    assert sorted(_read_0945_slice(cursor, ['2026-11-06'], {'2026-11-06'}, td_split_retry_depth=20),
                  key=lambda row: row['symbol']) == expected_ticks
    with pytest.raises(Exception) as failure:
        _read_0945_slice(RangeCursor(ticks, 0), ['2026-11-06'], {'2026-11-06'}, td_split_retry_depth=20)
    assert isinstance(failure.value.__cause__, MemoryExhausted)
