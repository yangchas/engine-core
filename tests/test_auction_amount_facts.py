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


def test_preopen_end_date_uses_previous_trade_day(monkeypatch, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine_core import ladder_daily_adapter
    from engine_core.data_pipeline.auction_amount_facts import run
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days',
                        lambda root, end: [d for d in ('2026-11-05', '2026-11-06', '2026-11-09') if d <= end])
    calls = []
    code, reply = run({'end_date': '2026-11-09', 'window': 1}, tmp_path,
                      now=datetime(2026, 11, 9, 5, 30, tzinfo=ZoneInfo('Asia/Shanghai')),
                      reader=lambda day, slot: calls.append((day, slot)) or {},
                      store=AuctionAmountFacts(FakeStore()), lease_factory=fake_lease_factory)
    assert reply['detail']['effective_end'] == '2026-11-06'
    assert all(day != '2026-11-09' for day, _ in calls)


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
            ts, count = re.search(r"VALUES \((\d+), '[^']+', '\d+', (-?\d+),", sql).groups()
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


def test_end_date_after_cutoff_and_weekend(monkeypatch, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine_core import ladder_daily_adapter
    from engine_core.data_pipeline.auction_amount_facts import run
    calendar = ['2026-11-05', '2026-11-06', '2026-11-09']
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days', lambda root, end: [d for d in calendar if d <= end])
    for end, hour, minute, expected in [('2026-11-09', 9, 46, '2026-11-09'),
                                        ('2026-11-08', 5, 30, '2026-11-06')]:
        now = datetime.fromisoformat(f'{end}T{hour:02}:{minute:02}:00').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        _, reply = run({'end_date': end, 'window': 1}, tmp_path, now=now,
                       store=AuctionAmountFacts(FakeStore()), dry_run=True)
        assert reply['detail']['effective_end'] == expected


def test_confirmed_empty_source_is_resolved_but_not_a_fact(monkeypatch, tmp_path):
    # Interface substitutes replay the recorded C890/C615 10-29 snapshot absence.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine_core import ladder_daily_adapter
    from engine_core.data_pipeline.auction_amount_facts import run
    calendar = ['2026-10-29', '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09']
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days', lambda root, end: [d for d in calendar if d <= end])
    facts = AuctionAmountFacts(FakeStore())
    # 09:45 is already complete in the C890 record; only 09:25 needs a query.
    facts.store.markers[facts._key('2026-10-29', '0945')[1]] = 5216
    calls = []
    counter_calls = []
    def reader(day, slot):
        calls.append((day, slot))
        return {}
    def counter(day, slot):
        counter_calls.append((day, slot))
        return 0
    kwargs = dict(now=datetime(2026, 11, 9, 5, 30, tzinfo=ZoneInfo('Asia/Shanghai')),
                  store=facts, reader=reader, source_counter=counter, lease_factory=fake_lease_factory)
    code, reply = run({'days': ['2026-10-29']}, tmp_path, **kwargs)
    assert code == 0 and reply['detail']['source_absent'] == [{'day': '2026-10-29', 'slot': '0925'}]
    assert facts.complete_days(['2026-10-29'], '0925') == set()
    assert facts.read_days(['2026-10-29'], '0925') == {}
    code, reply = run({'days': ['2026-10-29']}, tmp_path, **kwargs)
    assert code == 20 and len(calls) == len(counter_calls) == 1


def test_empty_normalized_source_with_raw_rows_stays_incomplete(monkeypatch, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine_core import ladder_daily_adapter
    from engine_core.data_pipeline.auction_amount_facts import run
    calendar = ['2026-10-29', '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09']
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days', lambda root, end: [d for d in calendar if d <= end])
    facts = AuctionAmountFacts(FakeStore())
    # C906 recorded 5,224 source snapshot rows; the reader is an interface substitute.
    code, reply = run({'days': ['2026-10-29']}, tmp_path, reader=lambda day, slot: {},
                      source_counter=lambda day, slot: 5224, store=facts,
                      now=datetime(2026, 11, 9, 5, 30, tzinfo=ZoneInfo('Asia/Shanghai')),
                      lease_factory=fake_lease_factory)
    assert code == 10 and reply['detail']['incomplete']['2026-10-29'] == {'0925': 0, '0945': 0}
    assert not reply['detail']['source_absent'] and reply['detail']['reason']


def test_recent_partial_source_remains_incomplete(monkeypatch, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine_core import ladder_daily_adapter
    from engine_core.data_pipeline.auction_amount_facts import run
    calendar = ['2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09']
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days', lambda root, end: [d for d in calendar if d <= end])
    tick = {symbol: amount for _, symbol, amount in rows('td_inputs_0945.jsonl.gz')}
    def counter(day, slot):
        raise AssertionError('protected source must not be classified absent')
    code, reply = run({'days': ['2026-11-06']}, tmp_path, reader=lambda day, slot: tick,
                      source_counter=counter, store=AuctionAmountFacts(FakeStore()),
                      now=datetime(2026, 11, 9, 5, 30, tzinfo=ZoneInfo('Asia/Shanghai')),
                      lease_factory=fake_lease_factory)
    assert code == 10 and reply['detail']['incomplete']['2026-11-06']['0945'] == len(tick) < 4000


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
    assert code == 75 and reply['reason'] == 'TD_RESOURCE_EXHAUSTED'
    assert facts.complete_days(['2026-11-05'], '0925') == {'2026-11-05'}
    assert reply['detail']['completed'] == ['2026-11-05']
    assert reply['detail']['remaining'] == ['2026-11-04', '2026-11-03']
    from engine_core.data_pipeline.td_sink import TdUnavailable
    from engine_core.guardian.contract import check_reply
    def unavailable(day, slot):
        raise TdUnavailable('unavailable')
    code, reply = run({'days': ['2026-11-04']}, tmp_path, reader=unavailable, store=facts, lease_factory=fake_lease_factory)
    assert code == 75 and reply['error_code'] == 'TD_UNAVAILABLE' and not check_reply(code, reply)
    class BrokenLease:
        def acquire(self): raise ConnectionError('unavailable')
    code, reply = run({'days': ['2026-11-04']}, tmp_path, store=facts, lease_factory=lambda day, slot: BrokenLease())
    assert code == 75 and reply['error_code'] == 'REDIS_UNAVAILABLE' and not check_reply(code, reply)


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


def test_read_rejects_changed_or_deleted_marker():
    import pytest
    amounts = {symbol: amount for _, symbol, day, _, amount in rows('td_inputs_auction.jsonl.gz') if day == '20261105'}
    for replacement in (None, len(amounts) + 1):
        class ChangingStore(FakeStore):
            def _run(self, sql, **kwargs):
                result = super()._run(sql, **kwargs)
                if sql.startswith('SELECT symbol') and self.change:
                    marker = facts._key('2026-11-05', '0925')[1]
                    if replacement is None:
                        self.markers.pop(marker)
                    else:
                        self.markers[marker] = replacement
                return result
        store = ChangingStore()
        store.change = False
        facts = AuctionAmountFacts(store)
        facts.write_day('2026-11-05', '0925', amounts, 'C731')
        store.change = True
        with pytest.raises(RuntimeError, match='AUCTION_FACTS_CHANGED_DURING_READ:2026-11-05:0925'):
            facts.read_days(['2026-11-05'], '0925')


def test_facts_and_baseline_share_configured_database(monkeypatch, tmp_path):
    from engine_core import ladder_daily_adapter, plate_amount_baseline
    from engine_core.data_pipeline import auction_amount_facts, td_sink

    stores = []
    class RecordingStore:
        def __init__(self, database):
            self.database = database
            self.sql = []
            stores.append(self)
        def _run(self, sql, **kwargs):
            self.sql.append(sql)
            return []

    monkeypatch.setattr(td_sink, 'TaosStore', RecordingStore)
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days', lambda root, target: ['2026-11-05'])
    for configured, expected in [('c812_test', 'c812_test'), (None, 'core_data'), ('', 'core_data')]:
        if configured is None:
            monkeypatch.delenv('CORE_TD_DATABASE', raising=False)
        else:
            monkeypatch.setenv('CORE_TD_DATABASE', configured)
        stores.clear()
        auction_amount_facts.run({'days': ['2026-11-05']}, tmp_path)
        plate_amount_baseline.build_and_store_plate_amount_baseline('2026-11-06', artifact_root=tmp_path, publish=False)
        assert [store.database for store in stores] == [expected, expected]
        assert all(f'{expected}.' in sql for store in stores for sql in store.sql)
        assert any('CREATE STABLE' in sql for sql in stores[0].sql)
        assert any('CREATE TABLE' in sql for sql in stores[0].sql)
        assert any('SELECT row_count' in sql for sql in stores[1].sql)

    calls = []
    original = auction_amount_facts.core_td_database
    def resolve():
        calls.append(True)
        return original()
    monkeypatch.setattr(auction_amount_facts, 'core_td_database', resolve)
    auction_amount_facts.run({'days': ['2026-11-05']}, tmp_path)
    plate_amount_baseline.build_and_store_plate_amount_baseline('2026-11-06', artifact_root=tmp_path, publish=False)
    assert len(calls) == 2
