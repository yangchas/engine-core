# DATA: real:core_artifacts/c664/plate_members/trade_date=2026-10-29 rows, td/plate_amount_2day, td/plate_amount_slice_halving, td/plate_amount_auction_halving, td/plate_amount_other_error (+ td/errors/*)
"""plate_amount_baseline over RECORDED TD answers (tests/replay.py): the 09:25 auction snapshots and 09:45 tick
amounts are what the real TD returned for the statements the code really issues; failures are real recorded errors."""
import gzip
import json
from pathlib import Path

import pytest

from engine_core import plate_amount_baseline as module
from engine_core.plate_amount_baseline import build_plate_amount_baseline
from tests.replay import ReplayTd, load_recording

MEMBERS = (Path(__file__).parent / "fixtures" / "real" / "core_artifacts" / "c664" / "plate_members"
           / "trade_date=2026-10-29" / "source=kaipan_getHisPlateIds" / "sha256-9a9492578bf6327b" / "rows.jsonl.gz")
DAYS = ["2026-10-29", "2026-10-30"]


def _members():
    with gzip.open(MEMBERS, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _observations(monkeypatch):
    from engine_core.data_pipeline import auction_amount_facts as facts
    connection = ReplayTd("plate_amount_2day")
    cursor = connection.cursor()
    auction = facts._read_auction_rows(cursor, DAYS)
    latest = {}
    for stamp, symbol, day, _, amount in auction:
        key = (module._day(day), module._symbol(symbol))
        if key not in latest or str(stamp) >= latest[key][0]:
            latest[key] = (str(stamp), amount)
    result = [{'trade_date': day, 'slot': '0925', 'symbol': symbol, 'amount_yuan': value}
              for (day, symbol), (_, value) in latest.items() if value is not None]
    return result + facts._read_0945_slice(cursor, DAYS, set(DAYS))


def _plate_with_every_member_observed(rows, observations, slot):
    seen = {(r["trade_date"], r["symbol"]) for r in observations if r["slot"] == slot}
    by_plate = {}
    for row in rows:
        by_plate.setdefault(row["plate_code"], set()).add(row["symbol"])
    for code in sorted(by_plate):
        if len(by_plate[code]) >= 3 and all((day, symbol) in seen for day in DAYS for symbol in by_plate[code]):
            return code, by_plate[code]
    raise AssertionError("no real plate has every member observed on both days")


def test_baseline_sums_each_dates_real_members_with_the_real_amounts(monkeypatch):
    members, observations = _members(), _observations(monkeypatch)
    code, symbols = _plate_with_every_member_observed(members, observations, "0945")
    amount = {(r["trade_date"], r["symbol"]): r["amount_yuan"] for r in observations if r["slot"] == "0945"}
    expected = [sum(amount[(day, symbol)] for symbol in symbols) for day in DAYS]
    result = build_plate_amount_baseline(
        target_date="2026-11-02", membership_by_date={day: members for day in DAYS},
        observations=observations, candidate_dates=DAYS)
    row = next(r for r in result["rows"] if r["plate_code"] == code and r["slot"] == "0945")
    assert row["amounts_yuan"] == expected and row["dates_used"] == DAYS and row["missing_dates"] == []
    assert row["sample_days"] == row["n_obs"] == 2 and row["median_yuan"] is None
    assert row["p25_yuan"] is None and row["p75_yuan"] is None and row["warm"] is False and row["quality"] == "PARTIAL"


def test_a_member_without_an_amount_makes_the_date_missing_and_is_never_zero_filled(monkeypatch):
    members, observations = _members(), _observations(monkeypatch)
    code, symbols = _plate_with_every_member_observed(members, observations, "0945")
    dropped = sorted(symbols)[0]
    thinner = [r for r in observations if not (r["slot"] == "0945" and r["trade_date"] == DAYS[0]
                                                and r["symbol"] == dropped)]
    result = build_plate_amount_baseline(target_date="2026-11-02", membership_by_date={day: members for day in DAYS},
                                         observations=thinner, candidate_dates=DAYS)
    row = next(r for r in result["rows"] if r["plate_code"] == code and r["slot"] == "0945")
    assert row["missing_dates"] == [DAYS[0]] and row["dates_used"] == [DAYS[1]] and row["sample_days"] == 1


def test_a_date_without_membership_is_missing(monkeypatch):
    members, observations = _members(), _observations(monkeypatch)
    code, _symbols = _plate_with_every_member_observed(members, observations, "0945")
    result = build_plate_amount_baseline(target_date="2026-11-02", membership_by_date={DAYS[1]: members},
                                         observations=observations, candidate_dates=DAYS)
    row = next(r for r in result["rows"] if r["plate_code"] == code and r["slot"] == "0945")
    assert row["missing_dates"] == [DAYS[0]] and row["sample_days"] == 1


def test_resource_exhaustion_stops_after_one_query_and_cli_reports_retry(monkeypatch, capsys, tmp_path):
    from engine_core import ladder_daily_adapter
    from engine_core.data_pipeline import auction_amount_facts
    from engine_core.data_pipeline.td_sink import TdResourceExhausted
    from examples import build_plate_amount_baseline as cli
    calls = []
    class Facts:
        def __init__(self, store): pass
        def complete_days(self, dates, slot):
            calls.append(slot)
            raise TdResourceExhausted('recorded 0x073a')
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days', lambda root, upto: DAYS)
    monkeypatch.setattr(auction_amount_facts, 'AuctionAmountFacts', Facts)
    assert cli.main(['--date', '2026-11-02', '--write', '--artifact-root', str(tmp_path)]) == 30
    assert calls == ['0925']
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['error_code'] == 'TD_RESOURCE_EXHAUSTED'


def test_cli_contract_success_outcomes(monkeypatch, capsys):
    from examples import build_plate_amount_baseline as cli
    from engine_core.guardian.contract import check_reply
    for status, reused, expected in [('READY', False, 0), ('PARTIAL', False, 10), ('READY', True, 20)]:
        result = {'status': status, 'reused': reused, 'version': 'v1', 'row_count': 1,
                  'date_count': 2, 'membership_date_count': 1}
        monkeypatch.setattr(cli, 'build_and_store_plate_amount_baseline', lambda *a, **kw: result)
        code = cli.main(['--date', '2026-11-02', '--write', '--available-at', '2026-11-02T00:00:00Z'])
        lines = capsys.readouterr().out.splitlines()
        assert json.loads(lines[0]) == result
        reply = json.loads(lines[-1])
        assert code == expected and not check_reply(code, reply)
        assert reply['error_code'] == ('ALREADY_SATISFIED' if reused else '')
        if status == 'PARTIAL':
            assert reply['detail']['membership_date_count'] == 1


def test_c731_real_input_rows_and_provenance(monkeypatch, tmp_path):
    import hashlib
    root = Path(__file__).parent / 'fixtures/real'
    index = json.loads((root / 'PROVENANCE.json').read_text())['_plate_amount_baseline_c731']
    inputs = {}
    for name, meta in index.items():
        path = root / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == meta['sha256']
        with gzip.open(path, 'rt', encoding='utf-8') as handle:
            inputs[path.name] = [json.loads(line) for line in handle]
        assert len(inputs[path.name]) == meta['rows']
    auction = inputs['td_inputs_auction.jsonl.gz']
    ticks = inputs['td_inputs_0945.jsonl.gz']
    observations = [{'trade_date': str(day), 'slot': '0925', 'symbol': symbol, 'amount_yuan': amount}
                    for _, symbol, day, _, amount in auction if amount is not None]
    observations += [{'trade_date': str(stamp)[:10], 'slot': '0945', 'symbol': symbol, 'amount_yuan': amount}
                     for stamp, symbol, amount in ticks if str(stamp)[11:19] == '09:30:01' and amount is not None]
    dates = sorted({module._day(row[2]) for row in auction})
    members = _members()
    versions = {day: 'sha256-9a9492578bf6327b' for day in dates}
    result = build_plate_amount_baseline(target_date='2026-11-06', membership_by_date={day: members for day in dates},
                                         observations=observations, candidate_dates=dates, membership_versions=versions,
                                         available_at='2026-11-06T00:00:00Z')
    assert result['status'] == 'PARTIAL' and result['rows'] and len(ticks) == 5796
    assert any(row['slot'] == '0945' and row['missing_dates'] for row in result['rows'])
    assert {ref['dataset'] for ref in result['input_refs']} == {'plate_members', 'auction_snapshot_v2', 'stock_tick_v2'}
    assert all(ref['version'] == versions[ref['trade_date']] for ref in result['input_refs'] if ref['dataset'] == 'plate_members')



def test_facts_only_partial_window(monkeypatch, tmp_path):
    from datetime import date, timedelta
    from engine_core import ladder_daily_adapter
    from engine_core.data_pipeline import auction_amount_facts
    dates = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(64)]
    members = _members()
    symbol = members[0]['symbol']
    plate = members[0]['plate_code']
    members = [r for r in members if r['symbol'] == symbol and r['plate_code'] == plate]
    with gzip.open(Path(__file__).parent / 'fixtures/real/auction_amount_facts_c731/td_inputs_auction.jsonl.gz', 'rt') as f:
        amounts = {row[1]: row[4] for row in map(json.loads, f) if row[4] is not None}
    with gzip.open(Path(__file__).parent / 'fixtures/real/auction_amount_facts_c731/td_inputs_0945.jsonl.gz', 'rt') as f:
        ticks = {row[1]: row[2] for row in map(json.loads, f) if row[2] is not None}
    symbol = next(r['symbol'] for r in _members() if r['symbol'] in amounts and r['symbol'] in ticks)
    members = [r for r in _members() if r['symbol'] == symbol][:1]
    class Facts:
        def __init__(self, store): pass
        def complete_days(self, days, slot): return set(dates[:3])
        def read_days(self, days, slot): return {day: {symbol: (amounts if slot == '0925' else ticks)[symbol]} for day in days}
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days', lambda root, upto: dates)
    monkeypatch.setattr(auction_amount_facts, 'AuctionAmountFacts', Facts)
    monkeypatch.setattr(module, '_membership_inputs', lambda catalog, days: ({day: members for day in days}, {}))
    calls = []
    monkeypatch.setattr(module, '_td_connect', lambda: calls.append('market_data1') or pytest.fail('market_data1 read'), raising=False)
    result = module.build_and_store_plate_amount_baseline('2026-11-06', artifact_root=tmp_path, publish=False)
    rows = module.ArtifactCatalog(tmp_path).read_rows(module.ArtifactCatalog(tmp_path).locate(module.DATASET, '2026-11-06'))
    assert result['status'] == 'PARTIAL' and all(row['sample_days'] == 3 for row in rows)
    assert calls == []


def test_no_complete_days_reports_missing_dependencies(monkeypatch, tmp_path, capsys):
    from engine_core import ladder_daily_adapter
    from engine_core.data_pipeline import auction_amount_facts
    from examples import build_plate_amount_baseline as cli
    days = ['2026-11-04', '2026-11-05']
    class Facts:
        def __init__(self, store): pass
        def complete_days(self, dates, slot): return set()
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days', lambda root, upto: days)
    monkeypatch.setattr(auction_amount_facts, 'AuctionAmountFacts', Facts)
    assert cli.main(['--date', '2026-11-06', '--write', '--artifact-root', str(tmp_path)]) == 30
    reply = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert reply['error_code'] == 'NOT_YET_AVAILABLE' and reply['retryable']
    assert {(item['dataset'], item['trade_date']) for item in reply['detail']['missing']} == {
        (dataset, day) for dataset in ('auction_amount', 'slot0945_amount') for day in days}


def test_pull_upstream_rechecks_facts(monkeypatch, tmp_path):
    from engine_core import ladder_daily_adapter
    from engine_core.data_pipeline import auction_amount_facts
    from examples import build_plate_amount_baseline as cli
    day = '2026-11-05'
    with gzip.open(Path(__file__).parent / 'fixtures/real/auction_amount_facts_c731/td_inputs_auction.jsonl.gz', 'rt') as f:
        amounts = {row[1]: row[4] for row in map(json.loads, f) if row[2] == '20261105' and row[4] is not None}
    with gzip.open(Path(__file__).parent / 'fixtures/real/auction_amount_facts_c731/td_inputs_0945.jsonl.gz', 'rt') as f:
        ticks = {row[1]: row[2] for row in map(json.loads, f) if row[2] is not None}
    member = next(row for row in _members() if row['symbol'] in amounts and row['symbol'] in ticks)
    class Facts:
        ready = False
        def __init__(self, store): pass
        def complete_days(self, dates, slot): return {day} if self.ready else set()
        def read_days(self, dates, slot): return {day: amounts if slot == '0925' else ticks}
    calls = []
    def run(request, root, *, store):
        calls.append(request)
        store.ready = True
        return 0, {'error_code': '', 'detail': {}}
    monkeypatch.setattr(ladder_daily_adapter, 'trading_days', lambda root, upto: [day])
    monkeypatch.setattr(auction_amount_facts, 'AuctionAmountFacts', Facts)
    monkeypatch.setattr(auction_amount_facts, 'run', run)
    monkeypatch.setattr(module, '_membership_inputs', lambda catalog, days: ({day: [member]}, {}))
    monkeypatch.setattr(module, '_publish', lambda *args: False)
    assert cli.main(['--date', '2026-11-06', '--write', '--artifact-root', str(tmp_path), '--pull-upstream']) == 10
    rows = module.ArtifactCatalog(tmp_path).read_rows(module.ArtifactCatalog(tmp_path).locate(module.DATASET, '2026-11-06'))
    assert calls == [{'days': [day]}] and all(row['dates_used'] == [day] for row in rows)


def test_recorded_auction_five_day_plate_statistics():
    fixture = Path(__file__).parent / "fixtures/real/plate_amount_baseline_c731/td_inputs_auction.jsonl.gz"
    with gzip.open(fixture, "rt") as source:
        raw = [json.loads(line) for line in source]
    dates = sorted({module._day(row[2]) for row in raw})[-5:]
    observations = [{"trade_date": module._day(day), "slot": "0925", "symbol": symbol, "amount_yuan": amount}
                    for _, symbol, day, _, amount in raw if module._day(day) in dates and amount is not None]
    members = _members()
    seen = {(r["trade_date"], r["symbol"]) for r in observations}
    by_plate = {}
    for member in members:
        by_plate.setdefault(member["plate_code"], set()).add(member["symbol"])
    code, symbols = next((code, symbols) for code, symbols in sorted(by_plate.items())
                         if symbols and all((day, symbol) in seen for day in dates for symbol in symbols))
    result = build_plate_amount_baseline(target_date="2026-11-06", membership_by_date={day: members for day in dates},
                                         observations=observations, candidate_dates=dates)
    row = next(r for r in result["rows"] if r["plate_code"] == code and r["slot"] == "0925")
    import statistics
    assert row["n_obs"] == 5 and row["warm"] and row["quality"] == "READY"
    assert row["median_yuan"] == statistics.median(row["amounts_yuan"])
