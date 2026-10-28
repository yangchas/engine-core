"""C567: Core TD sink for daily_kline - mapping, versions, idempotence, outage/recovery, mutations (fake store)."""
from __future__ import annotations

import json

import pytest

from td_sink_support import NROWS, commit_day, load_real

DAY = "2026-10-23"


def _sink(tmp_path, store=None):
    from engine_core.data_pipeline.td_sink import DailyKlineTdSink, MemoryTdStore
    store = store or MemoryTdStore()
    return DailyKlineTdSink(store, tmp_path), store


def test_mapping_columns_units_and_nulls():
    from engine_core.data_pipeline.td_sink import DATA_COLUMNS, map_rows, volume_factor

    _manifest, rows = load_real("formal")
    row = rows[0]
    mapped, dropped = map_rows([row], {"provider": "baostock", "field_units": {"volume": "shares"}})
    td = mapped["000001"]
    assert dropped == 0 and set(td) == set(DATA_COLUMNS)
    assert td["close"] == row["close"] and td["pct_chg"] == row["pct_chg"] == -0.885          # percent, not bp
    assert td["amount_yuan"] == row["amount_yuan"] and td["turnover_pct"] == row["turnover_pct"]
    assert td["volume_shares"] == int(row["volume"]) and td["suspended"] is False
    # lots -> shares only where the unit says so (C579: legacy q2_final carries lots with an UNKNOWN label)
    assert volume_factor({"field_units": {"volume": "lots"}}) == 100
    assert volume_factor({"provider": "legacy_q2_final", "field_units": {"volume": "UNKNOWN"}}) == 100
    assert volume_factor({"provider": "baostock", "field_units": {}}) == 1
    q2 = map_rows([{"symbol": "600519", "volume": 41599.0, "close": 1.0}], {"provider": "legacy_q2_final"})[0]["600519"]
    assert q2["volume_shares"] == 4159900
    # a missing value is NULL, never 0
    gap = map_rows([{"symbol": "000002", "close": 3.0}], {"provider": "baostock"})[0]["000002"]
    assert gap["volume_shares"] is None and gap["amount_yuan"] is None and gap["suspended"] is None


def test_sync_writes_reads_back_and_is_idempotent(tmp_path):
    commit_day(tmp_path, DAY, "baostock")
    sink, store = _sink(tmp_path)
    first = sink.sync_dates([DAY])[0]
    assert first.status == "synced" and first.row_count == NROWS and first.business_status == "FORMAL"
    assert store.day_counts([DAY]) == {DAY: NROWS}
    versions = store.read_versions(DAY)
    assert len(versions) == 1 and versions[0]["row_count"] == NROWS and versions[0]["version"] == first.version
    writes = store.calls.count("replace_day")
    again = sink.sync_dates([DAY])[0]
    assert again.status == "unchanged" and store.calls.count("replace_day") == writes   # no second write
    assert len(store.read_versions(DAY)) == 1


def test_formal_supersedes_provisional_and_lower_never_replaces(tmp_path):
    from engine_core.data_pipeline.td_sink import pick_best

    _manifest, rows = load_real("formal")
    commit_day(tmp_path, DAY, "wencai", rows=rows[:200])           # fewer rows, degraded provider
    sink, store = _sink(tmp_path)
    low = sink.sync_dates([DAY])[0]
    assert low.status == "synced" and low.business_status != "FORMAL" and low.row_count == 200
    commit_day(tmp_path, DAY, "baostock")                           # FORMAL arrives with a different row set
    formal = sink.sync_dates([DAY])[0]
    assert formal.status == "synced" and formal.business_status == "FORMAL" and formal.row_count == NROWS
    assert store.day_counts([DAY]) == {DAY: NROWS}                  # the 200-row day was replaced, not merged
    assert pick_best(store.read_versions(DAY))["version"] == formal.version
    assert {r["version"] for r in store.read_day_rows(DAY, ["000001"]).values()} == {formal.version}
    commit_day(tmp_path, DAY, "wencai", rows=rows[:150])           # a later low-quality write
    lower = sink.sync_dates([DAY])[0]
    assert lower.status == "unchanged" or lower.status == "skipped_lower"
    assert store.day_counts([DAY]) == {DAY: NROWS}
    assert pick_best(store.read_versions(DAY))["version"] == formal.version


def test_pending_dates_and_missing_catalog_day(tmp_path):
    commit_day(tmp_path, DAY, "baostock")
    commit_day(tmp_path, "2026-10-26", "baostock")
    sink, _store = _sink(tmp_path)
    assert sink.pending_dates(10) == [DAY, "2026-10-26"]
    assert sink.pending_dates(1) == ["2026-10-26"]
    assert sink.sync_date("2026-10-27").status == "no_catalog_version"


def test_td_outage_retries_then_recovers_and_catches_up(tmp_path):
    from engine_core.data_pipeline.td_sink import TdUnavailable, sync_with_retry

    commit_day(tmp_path, DAY, "baostock")
    commit_day(tmp_path, "2026-10-26", "baostock")
    sink, store = _sink(tmp_path)
    store.fail = "unavailable"
    sleeps = []
    with pytest.raises(TdUnavailable):
        sync_with_retry(sink, [DAY, "2026-10-26"], attempts=3, backoff_seconds=2.0, sleep=sleeps.append)
    assert sleeps == [2.0, 4.0] and not store.created                # 3 attempts, exponential backoff, nothing written
    store.fail = None                                                # TD is back: the next run catches up everything
    outcomes = sync_with_retry(sink, [DAY, "2026-10-26"], attempts=1)
    assert [o.status for o in outcomes] == ["synced", "synced"]
    assert store.day_counts([DAY, "2026-10-26"]) == {DAY: NROWS, "2026-10-26": NROWS}


def test_handler_outage_is_retryable_exit_30_and_recovery_clears_alert_marker(tmp_path, monkeypatch):
    from engine_core.guardian.td_sync import run_td_sync
    from engine_core.data_pipeline.td_sink import MemoryTdStore

    commit_day(tmp_path, DAY, "baostock")
    monkeypatch.setenv("CORE_TD_DATABASE", "core_dev_c567_unit")
    store = MemoryTdStore()
    store.fail = "unavailable"
    request = {"trade_date": DAY, "args": {}, "now": "2026-10-23T18:00:00+08:00"}
    code, body = run_td_sync(request, tmp_path, store=store)
    assert code == 30 and body["error_code"] == "TD_UNAVAILABLE" and body["retryable"] is True
    store.fail = None
    code, body = run_td_sync(request, tmp_path, store=store)
    assert code == 0 and body["detail"]["outcomes"][0]["status"] == "synced"
    code, body = run_td_sync(request, tmp_path, store=store)
    assert code == 0 and body["detail"]["outcomes"][0]["status"] == "unchanged"


def test_handler_refuses_forbidden_database(tmp_path, monkeypatch):
    from engine_core.guardian.td_sync import run_td_sync
    from engine_core.data_pipeline.td_sink import MemoryTdStore

    commit_day(tmp_path, DAY, "baostock")
    monkeypatch.setenv("CORE_TD_DATABASE", "market_data1")
    code, body = run_td_sync({"trade_date": DAY, "args": {}}, tmp_path, store=MemoryTdStore())
    assert code == 40 and body["error_code"] == "CONFIG_ERROR"


# ---- mutations: a wrong mapping/unit/partial write must fail read-back verification ------------------------------------
def _mutate_column(monkeypatch, column, extractor):
    from engine_core.data_pipeline import td_sink
    mapping = tuple((name, kind, extractor if name == column else fn) for name, kind, fn in td_sink.COLUMN_MAP)
    monkeypatch.setattr(td_sink, "COLUMN_MAP", mapping)


def test_mutation_wrong_unit_is_caught(tmp_path, monkeypatch):
    from engine_core.data_pipeline.td_sink import TdVerifyError

    commit_day(tmp_path, DAY, "baostock")
    _mutate_column(monkeypatch, "volume_shares",
                   lambda r, f: None if r.get("volume") is None else int(r["volume"]) * 1000)   # wrong unit
    sink, store = _sink(tmp_path)
    with pytest.raises(TdVerifyError, match="sum_volume"):
        sink.sync_dates([DAY])
    assert store.read_versions(DAY) == []                          # no version row: the day stays INCOMPLETE/missing


def test_mutation_swapped_column_is_caught(tmp_path, monkeypatch):
    from engine_core.data_pipeline.td_sink import TdVerifyError

    commit_day(tmp_path, DAY, "baostock")
    _mutate_column(monkeypatch, "close", lambda r, f: r.get("open"))   # close column filled from open
    sink, _store = _sink(tmp_path)
    with pytest.raises(TdVerifyError, match="sum_close"):
        sink.sync_dates([DAY])


def test_mutation_pct_in_basis_points_is_caught(tmp_path, monkeypatch):
    from engine_core.data_pipeline.td_sink import TdVerifyError

    commit_day(tmp_path, DAY, "baostock")
    _mutate_column(monkeypatch, "pct_chg", lambda r, f: None if r.get("pct_chg") is None else r["pct_chg"] * 100)
    sink, _store = _sink(tmp_path)
    with pytest.raises(TdVerifyError, match="sum_pct"):
        sink.sync_dates([DAY])


def test_mutation_partial_write_is_caught(tmp_path):
    from engine_core.data_pipeline.td_sink import MemoryTdStore, TdVerifyError

    class Truncating(MemoryTdStore):
        def replace_day(self, trade_date, version, mapped):
            super().replace_day(trade_date, version, dict(list(sorted(mapped.items()))[:-5]))

    commit_day(tmp_path, DAY, "baostock")
    sink, store = _sink(tmp_path, Truncating())
    with pytest.raises(TdVerifyError, match="count"):
        sink.sync_dates([DAY])
    assert store.read_versions(DAY) == []


def test_mutation_stale_rows_from_older_version_are_caught(tmp_path):
    from engine_core.data_pipeline.td_sink import MemoryTdStore, TdVerifyError

    class KeepsOldRows(MemoryTdStore):          # "merge" instead of replace: FORMAL would inherit PROVISIONAL leftovers
        def replace_day(self, trade_date, version, mapped):
            old = dict(self.days.get(trade_date, {}))
            super().replace_day(trade_date, version, mapped)
            self.days[trade_date] = {**old, **self.days[trade_date]}

    _manifest, rows = load_real("formal")
    commit_day(tmp_path, DAY, "wencai", rows=rows[300:420])        # disjoint symbols
    sink, store = _sink(tmp_path, KeepsOldRows())
    sink.sync_dates([DAY])
    commit_day(tmp_path, DAY, "baostock")
    with pytest.raises(TdVerifyError):
        sink.sync_dates([DAY])


def test_forbidden_and_malformed_database_names():
    from engine_core.data_pipeline.td_sink import TaosStore, validate_database_name

    for bad in ("market_data1", "log", "Core-Data", "", "a;drop"):
        with pytest.raises(ValueError):
            validate_database_name(bad)
    assert validate_database_name("core_data") == "core_data"
    with pytest.raises(ValueError):
        TaosStore("core_data").drop_database()                       # production name can never be dropped here


def test_ddl_declares_contract_columns_and_keep():
    from engine_core.data_pipeline.td_sink import ddl_statements

    text = "\n".join(ddl_statements("core_dev_c567_x"))
    for fragment in ("open DOUBLE", "close DOUBLE", "pre_close DOUBLE", "pct_chg DOUBLE", "volume_shares BIGINT",
                     "amount_yuan DOUBLE", "turnover_pct DOUBLE", "suspended BOOL", "TAGS (symbol VARCHAR(8))",
                     "KEEP 3650d", "daily_kline_versions", "sha256 VARCHAR(64)", "available_at", "quality"):
        assert fragment in text
    assert "market_data1" not in text


# ---- guardian wiring -----------------------------------------------------------------------------------------------
def test_schedule_declares_td_sync_events_with_retry_policy_and_go_whitelist():
    from pathlib import Path

    from engine_core.guardian.management import HANDLERS

    root = Path(__file__).resolve().parents[1]
    schedule = json.loads((root / "src/engine_core/guardian/schedule.json").read_text(encoding="utf-8"))
    events = [e for e in schedule["events"] if e.get("handler") == "td_sync_daily_kline"]
    assert [e["event_id"] for e in events] == ["td_sync_1515", "td_sync_1800", "td_sync_2015", "td_sync_2335"]
    for event in events:
        profile = schedule["profiles"][event["profile"]]
        assert profile["catchup"] == "run_in_window" and profile["retry_until_late"] is True
        assert event["cost"] == "light" and event["calendar"] == "trading_day" and event["late_until"] > event["deadline"]
    assert "td_sync_daily_kline" in HANDLERS
    assert '"td_sync_daily_kline"' in (root / "native/guardiand/main.go").read_text(encoding="utf-8")
    # the Go compiler test fixture is the checked-in schedule
    assert (root / "native/guardiand/testdata/schedule.json").read_text() == (
        root / "src/engine_core/guardian/schedule.json").read_text()


def test_guardian_handler_dispatches_the_event_and_syncs(tmp_path, monkeypatch):
    from examples import guardian_handler
    from engine_core.data_pipeline.td_sink import MemoryTdStore
    from engine_core.guardian import daily_kline_coverage

    commit_day(tmp_path, DAY, "baostock")
    store = MemoryTdStore()
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("CORE_TD_DATABASE", "core_dev_c567_dispatch")
    monkeypatch.setattr(daily_kline_coverage, "make_store", lambda settings: store)
    code, body = guardian_handler._run({"event": "td_sync_1515", "trade_date": DAY, "handler": "td_sync_daily_kline",
                                        "now": DAY + "T15:15:00+08:00", "args": {}})
    assert code == 0 and body["detail"]["outcomes"][0]["status"] == "synced"
    assert store.day_counts([DAY]) == {DAY: NROWS}
