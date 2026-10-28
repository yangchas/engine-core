"""C567 3b: the daily_kline self-check judges coverage from Core TD, not from catalog files (fake TD store)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from td_sink_support import commit_day, load_real

DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
NOW = datetime.fromisoformat("2026-10-23T20:00:00+08:00")
MINE = {"daily_kline", "daily_kline_td_sync"}


def _calendar(root):
    path = root / "trade_calendar" / "calendar.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"d": d, "t": 1, "src": "TEST", "u": "test"} for d in DAYS]
    path.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1", "lit_from": DAYS[0], "lit_to": DAYS[-1],
                                "days": rows}))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from engine_core.data_pipeline.td_sink import MemoryTdStore
    from engine_core.guardian import daily_kline_coverage

    store = MemoryTdStore()
    monkeypatch.setenv("TD_CONFIG", "/nonexistent/for-fake-store")
    monkeypatch.setenv("CORE_TD_DATABASE", "core_dev_c567_selfcheck")
    monkeypatch.setattr(daily_kline_coverage, "make_store", lambda settings: store)
    _calendar(tmp_path)
    (tmp_path / "catalog.json").write_text(json.dumps({"datasets": {}}))
    return tmp_path, store


def _plan(root):
    from engine_core.guardian.startup import boot_reconcile

    result = boot_reconcile("2026-10-23", "POST_MARKET", {"items": []}, root, now=NOW, reconcile_max_load5=9999)
    detail = result["detail"]
    return [row for row in detail["plan"] if row.get("id") in MINE], detail


def _sync(root, store, days):
    from engine_core.data_pipeline.td_sink import DailyKlineTdSink
    return DailyKlineTdSink(store, root).sync_dates(days)


def test_td_missing_and_catalog_missing_enqueues_fetch(env):
    root, store = env
    store.ensure_schema()
    plan, detail = _plan(root)
    assert [(r["id"], r["action"], r["partition"]) for r in plan] == [("daily_kline", "FETCH_NOW", "2026-10-19")]
    assert plan[0]["coverage_origin"] == "td" and detail["daily_kline_coverage"]["origin"] == "td"


def test_td_missing_but_catalog_has_formal_syncs_instead_of_refetching(env):
    root, store = env
    for day in DAYS:
        commit_day(root, day, "baostock")
    plan, _detail = _plan(root)
    assert [(r["id"], r["action"]) for r in plan] == [("daily_kline_td_sync", "SYNC_TD")]
    assert plan[0]["partitions"] == DAYS and plan[0]["reason"] == "catalog_version_not_in_td"


def test_td_complete_enqueues_nothing_even_if_catalog_file_is_gone(env):
    root, store = env
    for day in DAYS:
        commit_day(root, day, "baostock")
    _sync(root, store, DAYS)
    plan, detail = _plan(root)
    assert plan == [] and set(detail["daily_kline_coverage"]["states"].values()) == {"OK"}
    (root / "catalog.json").write_text(json.dumps({"datasets": {}}))        # TD, not the catalog, is the authority
    plan, _ = _plan(root)
    assert plan == []


def test_td_has_only_provisional_enqueues_upgrade(env):
    root, store = env
    _manifest, rows = load_real("formal")
    for day in DAYS:
        commit_day(root, day, "wencai", rows=rows[:250])
    _sync(root, store, DAYS)
    plan, detail = _plan(root)
    assert set(detail["daily_kline_coverage"]["states"].values()) == {"LOW_QUALITY"}
    assert len(plan) == 1 and plan[0]["action"] == "FETCH_NOW" and plan[0]["upgrade_daily_kline"] is True
    assert plan[0]["reason"] == "upgrade_degraded_daily_kline" and plan[0]["partition"] == "2026-10-19"


def test_formal_in_catalog_but_provisional_in_td_syncs_not_refetches(env):
    root, store = env
    _manifest, rows = load_real("formal")
    for day in DAYS:
        commit_day(root, day, "wencai", rows=rows[:250])
    _sync(root, store, DAYS)
    for day in DAYS:
        commit_day(root, day, "baostock")                                  # FORMAL lands in the catalog only
    plan, _ = _plan(root)
    assert [(r["id"], r["action"]) for r in plan] == [("daily_kline_td_sync", "SYNC_TD")]
    _sync(root, store, DAYS)
    plan, _ = _plan(root)
    assert plan == []


def test_one_missing_day_in_td_is_the_only_fetch(env):
    root, store = env
    for day in DAYS[:-1]:
        commit_day(root, day, "baostock")
    _sync(root, store, DAYS[:-1])
    plan, _ = _plan(root)
    assert [(r["id"], r["action"], r["partition"]) for r in plan] == [("daily_kline", "FETCH_NOW", "2026-10-23")]


def test_partial_rows_in_td_are_incomplete_and_resynced(env):
    root, store = env
    for day in DAYS:
        commit_day(root, day, "baostock")
    _sync(root, store, DAYS)
    store.days["2026-10-22"].pop("000001")                                  # a row lost after the version was recorded
    plan, detail = _plan(root)
    assert detail["daily_kline_coverage"]["states"]["2026-10-22"] == "INCOMPLETE"
    assert [(r["id"], r["action"], r["partitions"]) for r in plan] == [("daily_kline_td_sync", "SYNC_TD", ["2026-10-22"])]
    assert _sync(root, store, ["2026-10-22"])[0].status == "synced"
    assert _plan(root)[0] == []


def test_td_unreadable_is_unknown_falls_back_to_catalog_and_alerts_once(env):
    root, store = env
    for day in DAYS:
        commit_day(root, day, "baostock")
    store.fail = "unavailable"
    plan, detail = _plan(root)
    assert plan == []                                                       # no mass re-fetch, no sync attempt
    assert detail["daily_kline_coverage"]["origin"] == "catalog_fallback"
    assert detail["daily_kline_coverage"]["unknown_reason"].startswith("fake_td_down")
    assert detail["td_coverage_alert"]["event"] == "daily_kline_td_unreadable"
    _plan2, detail2 = _plan(root)
    assert "td_coverage_alert" not in detail2                               # one alert per outage
    store.fail = None
    _plan3, detail3 = _plan(root)
    assert detail3["daily_kline_coverage"]["origin"] == "td"
    store.fail = "unavailable"
    assert "td_coverage_alert" in _plan(root)[1]                            # re-armed after recovery


def test_catalog_fallback_does_not_turn_unknown_into_missing(env):
    """Empty TD answers MISSING (fetch); unreachable TD must not: with a complete catalog nothing is fetched."""
    root, store = env
    for day in DAYS:
        commit_day(root, day, "baostock")
    store.fail = "unavailable"
    plan_down, _ = _plan(root)
    store.fail = None
    plan_up, _ = _plan(root)
    assert plan_down == [] and [r["action"] for r in plan_up] == ["SYNC_TD"]


def test_sync_td_action_is_applied_by_boot_apply_plan(env):
    from engine_core.guardian.startup import boot_apply_plan

    root, store = env
    for day in DAYS:
        commit_day(root, day, "baostock")
    plan, _ = _plan(root)
    result = boot_apply_plan("2026-10-23", plan, root, now=NOW, phase="POST_MARKET", dry_run=False)
    row = result["detail"]["results"][0]
    assert row["action"] == "SYNC_TD" and row["status"] == "ok"
    assert {o["status"] for o in row["detail"]["outcomes"]} == {"synced"}
    assert _plan(root)[0] == []


def test_mutation_catalog_only_judgement_is_caught(env, monkeypatch):
    """Put the old behaviour back (catalog decides) and the TD-authority scenarios must fail."""
    from engine_core.guardian import reconcile_plan

    root, store = env
    for day in DAYS:
        commit_day(root, day, "baostock")
    _sync(root, store, DAYS)
    (root / "catalog.json").write_text(json.dumps({"datasets": {}}))
    assert _plan(root)[0] == []                                             # correct: TD complete -> nothing

    def catalog_only(catalog, dataset, partition, now, coverage=None):
        return {**reconcile_plan.daily_kline_catalog_state(catalog, dataset, partition, now),
                "sync_td": False, "origin": "catalog"}

    monkeypatch.setattr(reconcile_plan, "daily_kline_day_state", catalog_only)
    mutated, _ = _plan(root)
    assert mutated and mutated[0]["action"] == "FETCH_NOW"                  # the mutation re-fetches: the test above fails


def test_catalog_origin_when_td_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("TD_CONFIG", raising=False)
    _calendar(tmp_path)
    for day in DAYS:
        commit_day(tmp_path, day, "baostock")
    plan, detail = _plan(tmp_path)
    assert plan == [] and detail["daily_kline_coverage"]["origin"] == "catalog"
