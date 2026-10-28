"""TD-only daily-kline self-check and one-range-per-symbol gap planning."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from td_sink_support import commit_day

DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
NOW = datetime.fromisoformat("2026-10-23T20:00:00+08:00")


def _calendar(root):
    path = root / "trade_calendar" / "calendar.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"d": day, "t": 1, "src": "TEST", "u": "test"} for day in DAYS]
    path.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1", "lit_from": DAYS[0], "lit_to": DAYS[-1],
                                "days": rows}))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from engine_core.data_pipeline.td_sink import MemoryTdStore
    from engine_core.guardian import daily_kline_coverage

    store = MemoryTdStore()
    monkeypatch.setenv("TD_CONFIG", "/nonexistent/for-fake-store")
    monkeypatch.setenv("CORE_TD_DATABASE", "core_dev_c590_selfcheck")
    monkeypatch.setattr(daily_kline_coverage, "make_store", lambda settings: store)
    _calendar(tmp_path)
    (tmp_path / "catalog.json").write_text(json.dumps({"datasets": {}}))
    return tmp_path, store


def _plan(root):
    from engine_core.guardian.startup import boot_reconcile

    result = boot_reconcile("2026-10-23", "POST_MARKET", {"items": []}, root, now=NOW,
                            reconcile_max_load5=9999)
    return [row for row in result["detail"]["plan"] if row.get("id") == "daily_kline"], result["detail"]


def _seed_baostock_catalog_and_td(root, store, days=DAYS):
    from engine_core.data_pipeline.td_sink import DailyKlineTdSink

    for day in days:
        commit_day(root, day, "baostock")
    sink = DailyKlineTdSink(store, root)
    return [sink.sync_date(day) for day in days]


def test_missing_td_enqueues_baostock_gapfill_even_without_catalog(env):
    root, store = env
    _seed_baostock_catalog_and_td(root, store, DAYS[:-1])
    (root / "catalog.json").write_text(json.dumps({"datasets": {}}))
    plan, detail = _plan(root)
    assert [(row["id"], row["action"], row["partition"]) for row in plan] == [
        ("daily_kline", "FETCH_NOW", "2026-10-23")]
    assert detail["daily_kline_coverage"]["origin"] == "td"
    assert detail["daily_kline_coverage"]["gap_fill_symbols"] == len(store.latest_watermarks())


def test_catalog_formal_does_not_schedule_copy_into_td(env):
    root, store = env
    for day in DAYS:
        commit_day(root, day, "baostock")
    store.ensure_schema()
    plan, detail = _plan(root)
    assert [(row["id"], row["action"]) for row in plan] == [("daily_kline", "FETCH_NOW")]
    assert detail["daily_kline_coverage"]["origin"] == "td"


def test_td_complete_enqueues_nothing_even_if_catalog_is_gone(env):
    root, store = env
    _seed_baostock_catalog_and_td(root, store)
    plan, detail = _plan(root)
    assert plan == [] and set(detail["daily_kline_coverage"]["states"].values()) == {"OK"}
    (root / "catalog.json").write_text(json.dumps({"datasets": {}}))
    assert _plan(root)[0] == []


def test_partial_td_rows_enqueue_gapfill_without_sync_action(env):
    root, store = env
    _seed_baostock_catalog_and_td(root, store)
    store.days["2026-10-22"].pop("000001")
    plan, detail = _plan(root)
    assert detail["daily_kline_coverage"]["states"]["2026-10-22"] == "INCOMPLETE"
    assert [(row["id"], row["action"]) for row in plan] == [("daily_kline", "FETCH_NOW")]
    assert all(row["action"] != "SYNC_TD" for row in detail["plan"])


def test_selfcheck_reads_all_symbol_watermarks_in_one_aggregate(env):
    root, store = env
    _seed_baostock_catalog_and_td(root, store)
    store.calls.clear()
    plan, detail = _plan(root)
    assert plan == []
    assert store.calls.count("latest_watermarks") == 1
    assert detail["daily_kline_coverage"]["origin"] == "td"


def test_td_outage_is_unknown_and_never_falls_back_to_catalog(env):
    root, store = env
    for day in DAYS:
        commit_day(root, day, "baostock")
    store.ensure_schema()
    store.fail = "unavailable"
    plan, detail = _plan(root)
    assert plan == []
    assert detail["daily_kline_coverage"]["origin"] == "unknown"
    assert detail["daily_kline_coverage"]["unknown_reason"].startswith("fake_td_down")


def test_gap_plan_uses_next_trade_day_one_range_per_lagging_symbol_nearest_first():
    from engine_core.guardian.daily_kline_coverage import build_gap_fill_plan

    plan = build_gap_fill_plan({"000001": "2026-10-20", "000002": "2026-10-22",
                                "000003": "2026-10-23"}, "2026-10-23", DAYS, 5,
                               symbols=["000001", "000002", "000003"])
    assert [(row["symbol"], row["start_date"], row["end_date"]) for row in plan] == [
        ("000002", "2026-10-23", "2026-10-23"),
        ("000001", "2026-10-21", "2026-10-23")]


def test_unconfigured_td_stays_unknown_instead_of_using_catalog(tmp_path, monkeypatch):
    from engine_core.guardian.startup import boot_reconcile

    monkeypatch.delenv("TD_CONFIG", raising=False)
    _calendar(tmp_path)
    for day in DAYS:
        commit_day(tmp_path, day, "baostock")
    result = boot_reconcile("2026-10-23", "POST_MARKET", {"items": []}, tmp_path, now=NOW,
                            reconcile_max_load5=9999)
    detail = result["detail"]
    plan = [row for row in detail["plan"] if row.get("id") == "daily_kline"]
    assert plan == [] and detail["daily_kline_coverage"]["origin"] == "unknown"
