import gzip
import json
from pathlib import Path

from engine_core.data_pipeline.auction_amount_facts import AuctionAmountFacts

FIXTURES = Path(__file__).parent / 'fixtures/real/auction_amount_facts_c731'


def rows(name):
    with gzip.open(FIXTURES / name, 'rt') as source:
        return [json.loads(line) for line in source]


class FakeLease:
    def __init__(self):
        self.released = False
    def acquire(self): return True
    def renew(self): return True
    def release(self): self.released = True


def fake_lease_factory(day, slot):
    return FakeLease()


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
    store.markers[facts._key('2026-11-05', '0925')[1]] = len(replacement)
    store.fail_read = True
    with pytest.raises(RuntimeError, match='read failed'):
        facts.read_days(['2026-11-05'], '0925')


def test_run_resumes_after_resource_exhaustion(tmp_path):
    from engine_core.data_pipeline.auction_amount_facts import run
    from taos.error import ProgrammingError
    amounts = {symbol: amount for _, symbol, day, _, amount in rows('td_inputs_auction.jsonl.gz') if day == '20261105'}
    facts = AuctionAmountFacts(FakeStore())
    calls = []
    def reader(day, slot):
        calls.append((day, slot))
        if day == '2026-11-04':
            raise ProgrammingError('[0x073a]: Query memory exhausted')
        return amounts
    code, reply = run({'days': ['2026-11-03', '2026-11-04', '2026-11-05'], 'max_days': 2}, tmp_path, reader=reader, store=facts, lease_factory=fake_lease_factory)
    assert code == 30 and reply['reason'] == 'TD_RESOURCE_EXHAUSTED'
    assert facts.complete_days(['2026-11-05'], '0925') == {'2026-11-05'}
    assert reply['detail']['completed'] == ['2026-11-05']
    assert reply['detail']['remaining'] == ['2026-11-04', '2026-11-03']


def test_run_latest_first_and_already_satisfied(tmp_path):
    from engine_core.data_pipeline.auction_amount_facts import run
    from engine_core.guardian.contract import check_reply
    amounts = {symbol: amount for _, symbol, day, _, amount in rows('td_inputs_auction.jsonl.gz') if day == '20261105'}
    facts = AuctionAmountFacts(FakeStore())
    calls = []
    def reader(day, slot):
        calls.append((day, slot))
        return amounts
    days = ['2026-11-03', '2026-11-04', '2026-11-05']
    code, reply = run({'days': days, 'max_days': 2}, tmp_path, reader=reader, store=facts, lease_factory=fake_lease_factory)
    assert code == 0 and not check_reply(code, reply)
    assert calls == [(day, slot) for day in days[:0:-1] for slot in ('0925', '0945')]
    assert reply['detail']['remaining'] == ['2026-11-03']
    code, reply = run({'days': days[1:]}, tmp_path, reader=reader, store=facts, lease_factory=fake_lease_factory)
    assert code == 20 and not check_reply(code, reply)
    assert len(calls) == 4


def test_run_incomplete_real_tick_slice(tmp_path):
    from engine_core.data_pipeline.auction_amount_facts import run
    from engine_core.guardian.contract import check_reply
    auction = {symbol: amount for _, symbol, day, _, amount in rows('td_inputs_auction.jsonl.gz') if day == '20261105'}
    tick = {symbol: amount for _, symbol, amount in rows('td_inputs_0945.jsonl.gz')}
    facts = AuctionAmountFacts(FakeStore())
    code, reply = run({'days': ['2026-11-05']}, tmp_path, reader=lambda day, slot: auction if slot == '0925' else tick, store=facts, lease_factory=fake_lease_factory)
    assert code == 10 and not check_reply(code, reply)
    assert reply['detail']['incomplete']['2026-11-05']['0945'] == len(tick) < 4000
    assert facts.complete_days(['2026-11-05'], '0925') == {'2026-11-05'}
    assert not facts.complete_days(['2026-11-05'], '0945')


def test_run_expired_does_not_read_source(tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from engine_core.data_pipeline.auction_amount_facts import run
    from engine_core.guardian.contract import check_reply
    day = (datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=61)).isoformat()
    facts = AuctionAmountFacts(FakeStore())
    def reader(day, slot):
        raise AssertionError('expired source queried')
    code, reply = run({'days': [day]}, tmp_path, reader=reader, store=facts, lease_factory=fake_lease_factory)
    assert code == 0 and not check_reply(code, reply)
    assert reply['detail']['expired'] == [{'day': day, 'reason': 'SOURCE_EXPIRED'}]


def test_run_skips_locked_slot_without_reading(tmp_path):
    from engine_core.data_pipeline.auction_amount_facts import run
    class Locked:
        def acquire(self): return False
    facts = AuctionAmountFacts(FakeStore())
    def reader(day, slot):
        raise AssertionError('locked source queried')
    code, reply = run({'days': ['2026-11-05']}, tmp_path, reader=reader, store=facts,
                      lease_factory=lambda day, slot: Locked())
    assert code in (10, 20)
    assert reply['detail']['skipped_locked'] == [
        {'day': '2026-11-05', 'slot': '0925'}, {'day': '2026-11-05', 'slot': '0945'}]
    assert not facts.store.data and not facts.store.markers



def test_replacement_failure_hides_day_during_write():
    import pytest
    amounts = {symbol: amount for _, symbol, day, _, amount in rows('td_inputs_auction.jsonl.gz') if day == '20261105'}
    class InterruptedStore(FakeStore):
        interrupt = False
        def _run(self, sql, **kwargs):
            if self.interrupt and sql.startswith('INSERT INTO core_data.auction_amount_0925_'):
                assert not facts.complete_days(['2026-11-05'], '0925')
                assert facts.read_days(['2026-11-05'], '0925') == {}
                raise RuntimeError('interrupted')
            return super()._run(sql, **kwargs)
    store = InterruptedStore()
    facts = AuctionAmountFacts(store)
    facts.write_day('2026-11-05', '0925', amounts, 'first')
    store.interrupt = True
    with pytest.raises(RuntimeError, match='interrupted'):
        facts.write_day('2026-11-05', '0925', amounts, 'replacement')
    assert not facts.complete_days(['2026-11-05'], '0925')


def test_successful_acquisition_releases_lease(tmp_path):
    from engine_core.data_pipeline.auction_amount_facts import run
    amounts = {symbol: amount for _, symbol, day, _, amount in rows('td_inputs_auction.jsonl.gz') if day == '20261105'}
    facts = AuctionAmountFacts(FakeStore())
    leases = []
    def factory(day, slot):
        lease = FakeLease()
        leases.append(lease)
        return lease
    code, reply = run({'days': ['2026-11-05']}, tmp_path, reader=lambda day, slot: amounts,
                      store=facts, lease_factory=factory)
    assert code == 0 and all(lease.released for lease in leases)
    assert facts.complete_days(['2026-11-05'], '0925') == {'2026-11-05'}
    assert facts.read_days(['2026-11-05'], '0925')['2026-11-05'] == amounts
