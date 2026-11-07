import gzip
import json
from pathlib import Path

from engine_core.data_pipeline.auction_amount_facts import AuctionAmountFacts

FIXTURES = Path(__file__).parent / 'fixtures/real/auction_amount_facts_c731'


def rows(name):
    with gzip.open(FIXTURES / name, 'rt') as source:
        return [json.loads(line) for line in source]


def test_real_auction_roundtrip():
    store = FakeStore()
    facts = AuctionAmountFacts(store)
    facts.ensure_schema()
    expected = {}
    for _, symbol, day, _, amount in rows('td_inputs_auction.jsonl.gz'):
        day = f'{day[:4]}-{day[4:6]}-{day[6:]}'
        expected.setdefault(day, {})[symbol] = amount
    for day, amounts in expected.items():
        assert facts.write_day(day, '0925', amounts, 'C731') == len(amounts)
    assert facts.read_days(expected, '0925') == expected
    assert facts.complete_days(expected, '0925') == set(expected)


class FakeStore:
    database = 'core_data'

    def __init__(self):
        self.data = {}
        self.markers = {}
        self.statements = []
        self.fail_insert = False
        self.fail_read = False

    def _run(self, sql, *, missing_ok=False, strict=False):
        import re
        self.statements.append(sql)
        if sql.startswith('CREATE '):
            return []
        if sql.startswith('SELECT row_count'):
            assert strict
            ts = int(re.search(r'WHERE ts = (\d+)', sql)[1])
            return [(self.markers[ts],)] if ts in self.markers else []
        if sql.startswith('SELECT symbol'):
            assert strict
            if self.fail_read:
                raise RuntimeError('read failed')
            ts, slot = re.search(r"WHERE ts = (\d+) AND slot = '(\d+)'", sql).groups()
            return [(symbol, amount) for (t, s, symbol), amount in self.data.items()
                    if t == int(ts) and s == slot]
        if sql.startswith('DELETE FROM core_data.auction_amount_facts_days'):
            self.markers.pop(int(re.search(r'WHERE ts = (\d+)', sql)[1]), None)
        elif sql.startswith('DELETE FROM'):
            slot, symbol, ts = re.search(r'auction_amount_(\d+)_(\d+) WHERE ts = (\d+)', sql).groups()
            self.data.pop((int(ts), slot, symbol), None)
        elif sql.startswith('INSERT INTO core_data.auction_amount_facts_days'):
            ts, count = re.search(r"VALUES \((\d+), '[^']+', '\d+', (\d+),", sql).groups()
            self.markers[int(ts)] = int(count)
        elif sql.startswith('INSERT INTO'):
            if self.fail_insert:
                raise RuntimeError('write failed')
            for slot, symbol, ts, amount in re.findall(
                    r"auction_amount_(\d+)_(\d+) USING \S+ TAGS\('\d+', '\d+'\) VALUES \((\d+), (-?\d+)\)", sql):
                self.data[int(ts), slot, symbol] = int(amount)
        else:
            raise AssertionError(sql)
        return []


def test_incomplete_real_slice_does_not_write():
    import pytest
    amounts = {symbol: amount for _, symbol, amount in rows('td_inputs_0945.jsonl.gz')}
    store = FakeStore()
    facts = AuctionAmountFacts(store)
    with pytest.raises(ValueError, match=f'^AUCTION_FACTS_INCOMPLETE:2026-09-18:0945:{len(amounts)}$'):
        facts.write_day('2026-09-18', '0945', amounts, 'C731 partial')
    assert not store.statements and not store.data and not store.markers


def test_replace_and_failure_invalidate_marker():
    import pytest
    by_day = {}
    for _, symbol, day, _, amount in rows('td_inputs_auction.jsonl.gz'):
        if day == '20261105':
            by_day[symbol] = amount
    store = FakeStore()
    facts = AuctionAmountFacts(store)
    facts.write_day('2026-11-05', '0925', by_day, 'first')
    replacement = dict(by_day)
    replacement.pop(next(iter(replacement)))
    replacement[next(iter(replacement))] += 1
    facts.write_day('2026-11-05', '0925', replacement, 'second')
    assert facts.read_days(['2026-11-05'], '0925')['2026-11-05'] == replacement
    assert len(store.data) == len(replacement)
    store.fail_insert = True
    with pytest.raises(RuntimeError, match='write failed'):
        facts.write_day('2026-11-05', '0925', replacement, 'failed')
    assert facts.complete_days(['2026-11-05'], '0925') == set()
    store.fail_read = True
    with pytest.raises(RuntimeError, match='read failed'):
        facts.read_days(['2026-11-05'], '0925')
