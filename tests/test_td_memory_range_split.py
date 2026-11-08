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


def test_symbols_on_day_retries_memory_exhaustion(monkeypatch):
    from engine_core.data_pipeline.td_sink import TdResourceExhausted

    store = TaosStore('core_data')
    rows = [('000001',), ('600000',), ('600000',)]
    monkeypatch.setattr(store, '_run', lambda sql, **kw: rows)
    expected = store.symbols_on_day('2026-11-06')
    calls = []
    def query(sql, **kw):
        calls.append(sql)
        if len(calls) == 1:
            raise TdResourceExhausted('memory limit')
        assert kw['strict'] is True
        lower = re.search(r"symbol >= '([^']+)'", sql)
        upper = re.search(r"symbol < '([^']+)'", sql)
        return [row for row in rows if (not lower or row[0] >= lower.group(1))
                and (not upper or row[0] < upper.group(1))]
    monkeypatch.setattr(store, '_run', query)
    assert store.symbols_on_day('2026-11-06') == expected
    assert store.shard_events == [{'segments': 4}]


def test_all_stable_scan_readers_retry_memory_exhaustion(monkeypatch):
    import ast
    import inspect
    import textwrap
    from engine_core.data_pipeline.td_sink import TdResourceExhausted

    readers = {
        'day_aggregates': lambda s: s.day_aggregates('2026-11-06'),
        'read_day_rows': lambda s: s.read_day_rows('2026-11-06'),
        'day_counts': lambda s: s.day_counts(['2026-11-06']),
        'symbols_on_day': lambda s: s.symbols_on_day('2026-11-06'),
        'latest_watermarks': lambda s: s.latest_watermarks(),
    }
    tree = ast.parse(textwrap.dedent(inspect.getsource(TaosStore)))
    discovered = set()
    for node in tree.body[0].body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            strings = (ast.get_source_segment(textwrap.dedent(inspect.getsource(TaosStore)), part)
                       for part in ast.walk(node) if isinstance(part, ast.JoinedStr))
            if any('SUPER_TABLE' in source and 'SELECT ' in source for source in strings):
                discovered.add(node.name)
    assert discovered == set(readers)
    for name, read in readers.items():
        store = TaosStore('core_data')
        calls = []
        def query(sql, **kwargs):
            calls.append(sql)
            if len(calls) == 1:
                raise TdResourceExhausted('memory limit')
            return []
        monkeypatch.setattr(store, '_run', query)
        read(store)
        assert len(calls) > 1, name
        assert all('symbol ' in sql for sql in calls[1:]), name
        if name != 'read_day_rows':
            assert store.shard_events == [{'segments': 4}], name
            def exhausted(sql, **kwargs):
                raise TdResourceExhausted('memory limit')
            monkeypatch.setattr(store, '_run', exhausted)
            with pytest.raises(TdResourceExhausted):
                read(store)
            assert store.shard_events[-3:] == [{'segments': 4}, {'segments': 8}, {'segments': 16}]
