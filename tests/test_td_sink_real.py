"""C567: the sink and the TD coverage source against a REAL TDengine, in a throw-away ``core_dev_c567_<pid>`` database.

Never touches ``core_data`` or ``market_data1`` (the latter is only described, read-only). The database is dropped
at the end of the module; the drop is itself asserted.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from td_sink_support import NROWS, commit_day, load_real

DAY = "2026-10-23"
CFG = Path("/home/exedev/validation/operator-runs/taos-cfg")


@pytest.fixture(scope="module")
def td():
    from engine_core.data_pipeline.td_sink import TaosStore, TdUnavailable

    if not os.environ.get("TD_CONFIG") and CFG.exists():
        os.environ["TD_CONFIG"] = str(CFG)
    try:
        import taos  # noqa: F401
        probe = TaosStore("core_dev_c567_probe_" + str(os.getpid()))
        probe._cursor()
        probe.close()
    except (ImportError, TdUnavailable) as exc:
        pytest.skip("no reachable TDengine: " + str(exc)[:80])
    name = "core_dev_c567_" + str(os.getpid())
    store = TaosStore(name)
    yield store
    store.drop_database()
    check = TaosStore(name)
    assert check.schema_exists() is False                    # the temporary database is really gone
    check.close()
    store.close()


def _fresh(td):
    td.drop_database()
    td.ensure_schema()


def test_real_schema_matches_contract(td):
    _fresh(td)
    rows = td._run(f"DESCRIBE {td.database}.daily_kline_v1")
    columns = {r[0]: (r[1], r[3]) for r in rows}
    for name, kind in (("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"),
                       ("pre_close", "DOUBLE"), ("pct_chg", "DOUBLE"), ("volume_shares", "BIGINT"),
                       ("amount_yuan", "DOUBLE"), ("turnover_pct", "DOUBLE"), ("suspended", "BOOL")):
        assert columns[name][0] == kind
    assert columns["symbol"] == ("VARCHAR", "TAG")
    # the legacy table is only described for contrast: single-precision floats, which Core must not use
    legacy = {r[0]: r[1] for r in td._run("DESCRIBE market_data1.daily_kline")}
    assert legacy["close"] == "FLOAT" and columns["close"][0] == "DOUBLE"


def test_real_write_readback_idempotent_and_supersede(td, tmp_path):
    from engine_core.data_pipeline.td_sink import DailyKlineTdSink, pick_best

    _fresh(td)
    _m, rows = load_real("formal")
    commit_day(tmp_path, DAY, "baostock", rows=rows[:NROWS])           # Baostock provisional partition
    sink = DailyKlineTdSink(td, tmp_path)
    low = sink.sync_dates([DAY])[0]
    assert low.status == "synced" and low.row_count == NROWS and td.day_counts([DAY]) == {DAY: NROWS}
    commit_day(tmp_path, DAY, "baostock", rows=rows[:NROWS - 2])       # FORMAL without two of them
    formal = sink.sync_dates([DAY])[0]
    assert formal.status == "synced" and formal.business_status == "FORMAL"
    assert td.day_counts([DAY]) == {DAY: NROWS - 2}                    # the two stale PROVISIONAL rows are gone
    assert td.read_day_rows(DAY, [rows[NROWS - 1]["symbol"]]) == {}
    versions = td.read_versions(DAY)
    assert len(versions) == 2 and pick_best(versions)["version"] == formal.version
    assert sink.sync_dates([DAY])[0].status == "unchanged"
    assert len(td.read_versions(DAY)) == 2
    got = td.read_day_rows(DAY, ["000001"])["000001"]
    want = rows[0]
    assert got["close"] == want["close"] and got["pct_chg"] == want["pct_chg"] == -0.885
    assert got["volume_shares"] == int(want["volume"]) and got["suspended"] is False and got["version"] == formal.version
    agg = td.day_aggregates(DAY)
    assert agg["count"] == NROWS - 2 and abs(
        agg["sum_close"] - sum(r["close"] for r in rows[:NROWS - 2] if r["close"] is not None)) < 1e-6


def test_real_unit_mutation_fails_verification(td, tmp_path, monkeypatch):
    from engine_core.data_pipeline import td_sink
    from engine_core.data_pipeline.td_sink import DailyKlineTdSink, TdVerifyError

    _fresh(td)
    commit_day(tmp_path, DAY, "baostock")
    mapping = tuple((n, k, (lambda r, f: None if r.get("amount_yuan") is None else r["amount_yuan"] / 1e4)
                     if n == "amount_yuan" else fn) for n, k, fn in td_sink.COLUMN_MAP)   # yuan -> 10k yuan
    monkeypatch.setattr(td_sink, "COLUMN_MAP", mapping)
    with pytest.raises(TdVerifyError, match="sum_amount"):
        DailyKlineTdSink(td, tmp_path).sync_dates([DAY])
    assert td.read_versions(DAY) == []


def test_real_coverage_source_states(td, tmp_path):
    from engine_core.data_pipeline.td_sink import DailyKlineTdSink
    from engine_core.guardian.daily_kline_coverage import INCOMPLETE, LOW_QUALITY, MISSING, OK, TdCoverageSource

    _fresh(td)
    _m, rows = load_real("formal")
    commit_day(tmp_path, "2026-10-21", "baostock", rows=rows[:200])
    commit_day(tmp_path, "2026-10-22", "baostock")
    sink = DailyKlineTdSink(td, tmp_path)
    sink.sync_dates(["2026-10-21", "2026-10-22"])
    source = TdCoverageSource(td)
    states = source.coverage(["2026-10-20", "2026-10-21", "2026-10-22"])
    assert {d: c.state for d, c in states.items()} == {"2026-10-20": MISSING, "2026-10-21": LOW_QUALITY,
                                                         "2026-10-22": OK}
    td._run(f"DELETE FROM {td.database}.daily_kline_v1 WHERE ts = {td_ts('2026-10-22')}")
    assert source.coverage(["2026-10-22"])["2026-10-22"].state == MISSING or \
        source.coverage(["2026-10-22"])["2026-10-22"].state == INCOMPLETE
    assert sink.sync_dates(["2026-10-22"])[0].status == "synced"        # lost rows are rewritten
    assert source.coverage(["2026-10-22"])["2026-10-22"].state == OK


def td_ts(day):
    from engine_core.data_pipeline.td_sink import trade_day_ts_us
    return trade_day_ts_us(day)


def test_real_outage_then_recovery(td, tmp_path):
    from engine_core.data_pipeline.td_sink import DailyKlineTdSink, TaosStore, TdUnavailable, sync_with_retry

    _fresh(td)
    commit_day(tmp_path, DAY, "baostock")
    state = {"up": False}

    def flaky():
        if not state["up"]:
            raise OSError("connection refused (simulated outage)")
        return TaosStore._default_connect()

    store = TaosStore(td.database, connect=flaky)
    sink = DailyKlineTdSink(store, tmp_path)
    with pytest.raises(TdUnavailable):
        sync_with_retry(sink, [DAY], attempts=2, backoff_seconds=0.01)
    assert td.day_counts([DAY]) == {DAY: 0}
    state["up"] = True
    assert sync_with_retry(sink, [DAY], attempts=2, backoff_seconds=0.01)[0].status == "synced"
    assert td.day_counts([DAY]) == {DAY: NROWS}
    store.close()


def test_real_missing_database_is_missing_not_unavailable(td):
    from engine_core.data_pipeline.td_sink import TaosStore
    from engine_core.guardian.daily_kline_coverage import MISSING, TdCoverageSource

    ghost = TaosStore("core_dev_c567_ghost_" + str(os.getpid()))
    states = TdCoverageSource(ghost).coverage([DAY])
    assert states[DAY].state == MISSING and states[DAY].reason == "td_schema_absent"
    ghost.close()
