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


def _replayed(monkeypatch, *names):
    connection = ReplayTd(*names)
    monkeypatch.setattr(module, "_td_connect", lambda: connection)
    return connection


def _oracle_ticks(statements, days):
    """Independent re-statement of what the code must keep from the recorded tick rows."""
    keep = set()
    for entry in statements:
        if "stock_tick_v2" not in entry["sql"] or "rows" not in entry:
            continue
        for window_start, symbol, amount in entry["rows"]:
            stamp = window_start["$dt"]
            if stamp[11:19] == "09:30:01" and stamp[:10] in days and amount is not None:
                keep.add((stamp[:10], str(symbol).zfill(6), amount))
    return keep


def _observations(monkeypatch):
    _replayed(monkeypatch, "plate_amount_2day")
    return module._read_td("2026-11-02", DAYS)


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
    assert row["sample_days"] == 2 and row["median_yuan"] == sum(expected) / 2


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


def test_amounts_come_from_two_recorded_statements_not_one_per_day(monkeypatch):
    connection = _replayed(monkeypatch, "plate_amount_2day")
    rows = module._read_td("2026-11-02", DAYS)
    recorded = load_recording("plate_amount_2day")["statements"]
    assert connection.statements == [entry["sql"] for entry in recorded]            # auction + ONE 2-day tick slice
    tick = {(r["trade_date"], r["symbol"], r["amount_yuan"]) for r in rows if r["slot"] == "0945"}
    assert tick == _oracle_ticks(recorded, DAYS) and tick
    auction = [r for r in rows if r["slot"] == "0925"]
    assert {r["trade_date"] for r in auction} == {"2026-10-30"}                    # the real 10-29 snapshots are empty


def test_a_tick_slice_that_runs_out_of_query_memory_is_halved(monkeypatch):
    connection = _replayed(monkeypatch, "plate_amount_slice_halving")
    rows = module._read_td("2026-11-02", DAYS, td_split_retry_depth=1)
    recorded = load_recording("plate_amount_slice_halving")["statements"]
    assert connection.statements == [entry["sql"] for entry in recorded]            # auction, failing 2-day, two 1-day
    assert {(r["trade_date"], r["symbol"], r["amount_yuan"]) for r in rows if r["slot"] == "0945"} == \
        _oracle_ticks(recorded, DAYS)


def test_an_auction_slice_that_runs_out_of_query_memory_is_halved(monkeypatch):
    connection = _replayed(monkeypatch, "plate_amount_auction_halving")
    rows = module._read_auction_rows(connection.cursor(), DAYS, td_split_retry_depth=1)
    recorded = load_recording("plate_amount_auction_halving")["statements"]
    assert connection.statements == [entry["sql"] for entry in recorded]            # failing 2-day, then each day alone
    assert len(rows) == sum(entry["row_count"] for entry in recorded if "rows" in entry) == 5223


def test_a_failure_that_is_not_memory_exhaustion_surfaces_with_its_stage_and_range(monkeypatch):
    _replayed(monkeypatch, "plate_amount_other_error")
    with pytest.raises(RuntimeError, match=r"plate_amount_baseline_tick_slice 2026-10-30\.\.2026-10-30.*Table does not exist"):
        module._read_td("2026-11-02", ["2026-10-30"])


def test_no_dates_means_no_td_connection(monkeypatch):
    # DATA: spec (no data involved: asserts that no connection is attempted)
    monkeypatch.setattr(module, "_td_connect", lambda: pytest.fail("must not connect without dates"))
    assert module._read_td("2026-11-02", []) == []


def test_recorded_td_memory_exhaustion_surfaces_with_its_code():
    from taos.error import ProgrammingError
    from engine_core.data_pipeline.td_sink import TdResourceExhausted
    record = json.loads((Path(__file__).parent / "fixtures" / "real" / "td" / "errors" /
                         "query_memory_exhausted_0x073a.json").read_text(encoding="utf-8"))

    class Cursor:
        def execute(self, sql):
            raise ProgrammingError(record["message"], int(record["errno"], 16))

    for query in (lambda: module._auction_slice(Cursor(), [DAYS[0]]),
                  lambda: module._read_0945_slice(Cursor(), [DAYS[0]], set(DAYS), ("000000", "300000"))):
        with pytest.raises(TdResourceExhausted) as caught:
            query()
        assert caught.value.error_code == "TD_RESOURCE_EXHAUSTED"


def test_resource_exhaustion_stops_after_one_query_and_cli_reports_retry(monkeypatch, capsys, tmp_path):
    from taos.error import ProgrammingError
    from engine_core.data_pipeline.td_sink import TdResourceExhausted
    from examples import build_plate_amount_baseline as cli
    record = json.loads((Path(__file__).parent / 'fixtures/real/td/errors/query_memory_exhausted_0x073a.json').read_text())

    class Cursor:
        calls = 0
        def execute(self, sql):
            self.calls += 1
            raise ProgrammingError(record['message'], int(record['errno'], 16))

    cursor = Cursor()
    with pytest.raises(TdResourceExhausted):
        module._read_auction_rows(cursor, DAYS)
    assert cursor.calls == 1
    tick = Cursor()
    with pytest.raises(TdResourceExhausted):
        module._read_0945_slice(tick, DAYS, set(DAYS))
    assert tick.calls == 1

    class Connection:
        def __init__(self, cursor): self._cursor = cursor
        def cursor(self): return self._cursor
        def close(self): pass

    class DayCursor:
        def execute(self, sql): pass
        def fetchall(self): return [(day,) for day in DAYS]

    failing = Cursor()
    connections = iter([Connection(DayCursor()), Connection(failing)])
    monkeypatch.setattr(module, '_td_connect', lambda: next(connections))
    monkeypatch.setattr(module, '_obs_store', lambda: None)
    assert cli.main(['--date', '2026-11-02', '--write', '--artifact-root', str(tmp_path)]) == 30
    assert failing.calls == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['error_code'] == 'TD_RESOURCE_EXHAUSTED'


def test_cli_contract_success_outcomes(monkeypatch, capsys):
    from examples import build_plate_amount_baseline as cli
    from engine_core.guardian.contract import check_reply
    for status, reused, expected in [('READY', False, 0), ('PARTIAL', False, 10), ('READY', True, 20)]:
        result = {'status': status, 'reused': reused, 'version': 'v1', 'row_count': 1,
                  'date_count': 2, 'membership_date_count': 1}
        monkeypatch.setattr(cli, 'build_and_store_plate_amount_baseline', lambda *a, **kw: result)
        code = cli.main(['--date', '2026-11-02', '--write', '--available-at', '2026-11-02T00:00:00Z', '--td-split-retry-depth', '2'])
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

    class Connection:
        def cursor(self): return self
        def execute(self, sql): pass
        def fetchall(self): return [(day,) for day in dates]
        def close(self): pass
    monkeypatch.setattr(module, '_td_connect', lambda: Connection())
    monkeypatch.setattr(module, '_membership_inputs', lambda catalog, days: ({day: members for day in days}, versions))
    monkeypatch.setattr(module, '_obs_store', lambda: None)
    monkeypatch.setattr(module, 'read_observations', lambda days, **kwargs: (observations, {}))
    saved = module.build_and_store_plate_amount_baseline('2026-11-06', artifact_root=tmp_path, publish=False,
                                                         available_at='2026-11-06T00:00:00Z')
    catalog = module.ArtifactCatalog(tmp_path)
    manifest = json.loads(catalog.locate(module.DATASET, '2026-11-06').read_text())
    assert saved['status'] == 'PARTIAL'
    assert manifest['input_refs'] == result['input_refs'] and manifest['membership_versions'] == versions
    assert manifest['declared_available_at'] == '2026-11-06T00:00:00Z'
    assert all(row['available_at'] == '2026-11-06T00:00:00Z' for row in catalog.read_rows(catalog.locate(module.DATASET, '2026-11-06')))
