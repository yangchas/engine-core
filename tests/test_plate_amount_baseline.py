# DATA: real:core_artifacts/c664/plate_members/trade_date=2026-10-29 rows, td/plate_amount_2day, td/plate_amount_slice_halving, td/plate_amount_symbol_split, td/plate_amount_auction_halving, td/plate_amount_other_error (+ td/errors/*)
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
    rows = module._read_td("2026-11-02", DAYS)
    recorded = load_recording("plate_amount_slice_halving")["statements"]
    assert connection.statements == [entry["sql"] for entry in recorded]            # auction, failing 2-day, two 1-day
    assert {(r["trade_date"], r["symbol"], r["amount_yuan"]) for r in rows if r["slot"] == "0945"} == \
        _oracle_ticks(recorded, DAYS)


def test_a_single_day_that_still_runs_out_of_memory_is_split_by_symbol_range(monkeypatch):
    connection = _replayed(monkeypatch, "plate_amount_symbol_split")
    rows = module._read_td("2026-11-02", ["2026-10-30"])
    recorded = load_recording("plate_amount_symbol_split")["statements"]
    assert connection.statements == [entry["sql"] for entry in recorded]            # auction, failing day, 3 ranges
    assert sum(1 for entry in recorded if "symbol >=" in entry["sql"]) == 3
    assert {(r["trade_date"], r["symbol"], r["amount_yuan"]) for r in rows if r["slot"] == "0945"} == \
        _oracle_ticks(recorded, ["2026-10-30"])


def test_an_auction_slice_that_runs_out_of_query_memory_is_halved(monkeypatch):
    connection = _replayed(monkeypatch, "plate_amount_auction_halving")
    rows = module._read_auction_rows(connection.cursor(), DAYS)
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
