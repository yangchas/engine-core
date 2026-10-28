import json
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path


TZ = timezone(timedelta(hours=8))


def _calendar(root, days):
    path = root / "trade_calendar" / "calendar.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"d": day, "t": int(trading), "src": "TEST", "u": "test"}
            for day, trading in days]
    path.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1",
                                "lit_from": rows[0]["d"], "lit_to": rows[-1]["d"],
                                "days": rows}), encoding="utf-8")


def _catalog(root, dates):
    datasets = {"daily_kline": {"dates": {}}}
    for day, status in dates.items():
        datasets["daily_kline"]["dates"][day] = [{
            "version": "test-" + day,
            "row_count": 1,
            "business_status": "PROVISIONAL" if status != "FORMAL" else "FORMAL",
            "quality": {"status": "PROVISIONAL" if status != "FORMAL" else "OK"},
        }]
    (root / "catalog.json").write_text(json.dumps({"datasets": datasets}), encoding="utf-8")


def _daily_registry():
    return [{"id": "daily_kline", "artifact": {"catalog_dataset": "daily_kline"},
             "fetch_job": ["stub"], "cost": {"class": "heavy"},
             "late_policy": {"mode": "fetch_now", "trigger_at": "17:30:00",
                             "partition_before_trigger": "D-1", "depends_on": []}}]


def _fake_td_coverage(catalog, candidates):
    """Model the TD version table for deterministic guardian planning tests."""
    from engine_core.guardian.daily_kline_coverage import (
        CoverageResult, DayCoverage, LOW_QUALITY, MISSING, OK,
    )

    dates = catalog.get("datasets", {}).get("daily_kline", {}).get("dates", {})
    days = {}
    for day in candidates:
        versions = dates.get(day) or []
        if not versions:
            days[day] = DayCoverage(day, MISSING, "td")
            continue
        version = versions[-1]
        state = OK if version.get("business_status") == "FORMAL" else LOW_QUALITY
        days[day] = DayCoverage(
            day, state, "td", version=str(version.get("version") or ""),
            business_status=str(version.get("business_status") or ""),
            needs_upgrade=state == LOW_QUALITY, rows=int(version.get("row_count") or 0))
    return CoverageResult(days, "td", watermarks={}, gap_fill_plan=[])


def _install_fake_td_coverage(startup, monkeypatch):
    monkeypatch.setattr(startup, "daily_kline_coverage",
                        lambda _root, candidates, catalog, _now: _fake_td_coverage(catalog, candidates))


def test_sa_window_budget_throttles_eight_minutes_and_exhausts_at_three(tmp_path, monkeypatch):
    from engine_core.guardian import startup

    _calendar(tmp_path, [("2026-10-26", True), ("2026-10-27", True)])
    _catalog(tmp_path, {"2026-10-26": "PROVISIONAL"})
    monkeypatch.setattr(startup, "load_registry", _daily_registry)
    _install_fake_td_coverage(startup, monkeypatch)
    source_calls = []

    def fail_fetch(item, day, timeout, root):
        source_calls.append(day)
        raise startup.FetchJobError("temporary source failure", "FETCH_TIMEOUT")

    monkeypatch.setattr(startup, "_run_fetch_job", fail_fetch)
    times = ["2026-10-26T21:02:00+08:00", "2026-10-26T21:10:00+08:00",
             "2026-10-26T21:13:00+08:00", "2026-10-26T21:24:00+08:00"]
    results = [startup.fetch_item("daily_kline", "2026-10-26", tmp_path,
                                  now=datetime.fromisoformat(value), phase="POST_MARKET")
               for value in times]

    assert len(source_calls) == 3
    assert results[1]["detail"]["upgrade"]["throttled"] is True
    state = json.loads((tmp_path / "guardian-upgrade-attempts.json").read_text())["2026-10-26"]
    assert state["attempts"] == 3
    assert state["status"] == "GAP_WINDOW_EXHAUSTED"
    gap = json.loads((tmp_path / "guardian-native" / "gaps" / "2026-10-26.json").read_text())[0]
    assert gap["status"] == "GAP_WINDOW_EXHAUSTED"
    assert gap["next_retry_window"] == "2026-10-27:PRE_MARKET"


def test_legacy_exhausted_without_window_does_not_block_next_quiet_window(tmp_path, monkeypatch):
    from engine_core.guardian import startup

    _calendar(tmp_path, [("2026-10-26", True)])
    _catalog(tmp_path, {"2026-10-26": "PROVISIONAL"})
    (tmp_path / "guardian-upgrade-attempts.json").write_text(json.dumps({
        "2026-10-26": {"status": "exhausted", "attempts": 3,
                       "reason": "upgrade_attempt_budget_exhausted"}}))
    monkeypatch.setattr(startup, "load_registry", _daily_registry)
    _install_fake_td_coverage(startup, monkeypatch)
    monkeypatch.setattr(startup, "items_for_phase", lambda _phase, items: items)
    monkeypatch.setattr(startup.os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(startup.os, "cpu_count", lambda: 4)

    result = startup.boot_reconcile(
        "2026-10-26", "POST_MARKET", {"items": []}, tmp_path,
        now=datetime.fromisoformat("2026-10-26T21:30:00+08:00"))
    action = next(row for row in result["detail"]["plan"] if row["id"] == "daily_kline")

    assert action["partition"] == "2026-10-26" and action["action"] == "FETCH_NOW"
    queued = startup.boot_apply_plan(
        "2026-10-26", result["detail"]["plan"], tmp_path,
        now="2026-10-26T21:30:00+08:00", phase="POST_MARKET", enqueue_only=True)
    deferred = next(row for row in queued["detail"]["deferred"] if row["id"] == "daily_kline")
    assert deferred["partition"] == "2026-10-26"


def test_sb_next_premarket_requeues_old_exhausted_partition_and_success_resolves(tmp_path, monkeypatch):
    from engine_core.guardian import startup
    from engine_core.guardian.reconcile_plan import reconcile_plan

    _calendar(tmp_path, [("2026-10-26", True), ("2026-10-27", True)])
    _catalog(tmp_path, {"2026-10-26": "PROVISIONAL"})
    (tmp_path / "guardian-upgrade-attempts.json").write_text(json.dumps({
        "2026-10-26": {"status": "GAP_WINDOW_EXHAUSTED", "attempts": 3,
                       "window_id": "2026-10-26:POST_MARKET", "last_attempt_at": "2026-10-26T21:24:00+08:00"}}))
    registry = _daily_registry()
    monkeypatch.setattr(startup, "load_registry", lambda: registry)
    plan = reconcile_plan(datetime.fromisoformat("2026-10-27T08:30:00+08:00"),
                          json.loads((tmp_path / "catalog.json").read_text()),
                          trade_date="2026-10-27", prev_trade_date="2026-10-26",
                          phase="PRE_MARKET", registry=registry,
                          upgrade_attempts=json.loads((tmp_path / "guardian-upgrade-attempts.json").read_text()),
                          daily_kline_candidates=["2026-10-26"],
                          daily_kline_coverage=_fake_td_coverage(
                              json.loads((tmp_path / "catalog.json").read_text()), ["2026-10-26"]).days,
                          daily_kline_coverage_origin="td")
    action = next(row for row in plan if row["id"] == "daily_kline")
    assert action["partition"] == "2026-10-26" and action["action"] == "FETCH_NOW"

    def formal_fetch(item, day, timeout, root):
        path = Path(root) / "catalog.json"
        catalog = json.loads(path.read_text())
        catalog["datasets"]["daily_kline"]["dates"][day] = [{
            "version": "formal-" + day, "row_count": 1,
            "business_status": "FORMAL", "quality": {"status": "OK"},
            "available_at": "2026-10-27T08:30:00+08:00"}]
        path.write_text(json.dumps(catalog))
        return {"status": "ok"}

    monkeypatch.setattr(startup, "_run_fetch_job", formal_fetch)
    result = startup.fetch_item("daily_kline", "2026-10-26", tmp_path,
                               now=datetime.fromisoformat("2026-10-27T08:30:00+08:00"),
                               phase="PRE_MARKET")
    assert result["status"] == "ok"
    state = json.loads((tmp_path / "guardian-upgrade-attempts.json").read_text())["2026-10-26"]
    assert state["status"] == "resolved"
    gap = json.loads((tmp_path / "guardian-native" / "gaps" / "2026-10-26.json").read_text())[0]
    assert gap["status"] == "RESOLVED"


def test_sc_three_failed_trading_day_windows_alert_once_and_keep_retrying(tmp_path, monkeypatch):
    from examples import guardian_handler
    from engine_core.guardian import startup

    trading_days = [(f"2026-10-{day}", True) for day in range(26, 30)]
    _calendar(tmp_path, trading_days)
    _catalog(tmp_path, {"2026-10-23": "PROVISIONAL"})
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(startup, "load_registry", _daily_registry)
    monkeypatch.setattr(guardian_handler, "_send_alert", lambda *_args, **_kwargs: {"status": "sent"})
    source_calls = []

    def fail_fetch(item, day, timeout, root):
        source_calls.append(day)
        raise startup.FetchJobError("temporary source failure", "FETCH_TIMEOUT")

    monkeypatch.setattr(startup, "_run_fetch_job", fail_fetch)
    alert_count = 0
    for date in ("2026-10-26", "2026-10-27", "2026-10-28"):
        for minute in (2, 13, 24):
            now = f"{date}T08:{minute:02d}:00+08:00"
            _code, result = guardian_handler._run({"handler": "fetch_item", "trade_date": date,
                "data_date": "2026-10-23", "item_id": "daily_kline", "phase": "PRE_MARKET", "now": now})
            if result.get("detail", {}).get("alert", {}).get("status") == "sent":
                alert_count += 1

    _code, later = guardian_handler._run({"handler": "fetch_item", "trade_date": "2026-10-29",
        "data_date": "2026-10-23", "item_id": "daily_kline", "phase": "PRE_MARKET",
        "now": "2026-10-29T08:30:00+08:00"})
    state = json.loads((tmp_path / "guardian-upgrade-attempts.json").read_text())["2026-10-23"]
    assert alert_count == 1
    assert len(source_calls) == 10
    assert later["status"] == "degraded" and state["attempts"] == 1
    assert state["status"] == "GAP_ESCALATED" and state["escalation_alerted"] is True


def test_sd_recent_missing_partitions_are_fetched_oldest_first_one_at_a_time():
    from engine_core.guardian.reconcile_plan import reconcile_plan

    registry = _daily_registry()
    catalog = {"datasets": {"daily_kline": {"dates": {
        "2026-10-22": [{"version": "v22", "row_count": 1,
                        "business_status": "FORMAL", "quality": {"status": "OK"}}],
        "2026-10-23": [{"version": "v23", "row_count": 1,
                        "business_status": "FORMAL", "quality": {"status": "OK"}}],
    }}}}
    candidates = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    now = datetime.fromisoformat("2026-10-27T08:30:00+08:00")
    first = reconcile_plan(now, catalog, trade_date="2026-10-27", prev_trade_date="2026-10-26",
                           phase="PRE_MARKET", registry=registry, daily_kline_candidates=candidates,
                           daily_kline_coverage=_fake_td_coverage(catalog, candidates).days,
                           daily_kline_coverage_origin="td")
    assert [(row["id"], row["partition"]) for row in first if row["id"] == "daily_kline"] == [
        ("daily_kline", "2026-10-20")]
    catalog["datasets"]["daily_kline"]["dates"]["2026-10-20"] = [{
        "version": "v20", "row_count": 1, "business_status": "FORMAL", "quality": {"status": "OK"},
        "available_at": "2026-10-27T08:30:00+08:00"}]
    second = reconcile_plan(now, catalog, trade_date="2026-10-27", prev_trade_date="2026-10-26",
                            phase="PRE_MARKET", registry=registry, daily_kline_candidates=candidates,
                            daily_kline_coverage=_fake_td_coverage(catalog, candidates).days,
                            daily_kline_coverage_origin="td")
    assert [(row["id"], row["partition"]) for row in second if row["id"] == "daily_kline"] == [
        ("daily_kline", "2026-10-21")]


def test_se_heavy_daily_kline_is_deferred_during_intraday(tmp_path, monkeypatch):
    from engine_core.guardian import startup

    marker = []
    monkeypatch.setattr(startup, "load_registry", _daily_registry)
    monkeypatch.setattr(startup, "_run_fetch_job", lambda *_args: marker.append("ran"))
    result = startup.boot_apply_plan("2026-10-27", [{"id": "daily_kline", "partition": "2026-10-26",
        "action": "FETCH_NOW", "lane": "serial", "cost": "heavy"}], tmp_path,
        now="2026-10-27T10:00:00+08:00", phase="INTRADAY_AM", enqueue_only=True)
    assert marker == []
    assert result["detail"]["deferred"][0]["run_in_phases"] == ["MIDDAY_BREAK"]


def test_sf_live_exhausted_state_is_requeued_next_premarket(tmp_path, monkeypatch):
    import pytest
    from engine_core.guardian import startup

    snapshot = Path("/home/exedev/validation/s1/C554/live_initial")
    if not (snapshot / "guardian-upgrade-attempts.json").exists():
        pytest.skip("C554 read-only live snapshot is not available")
    (tmp_path / "guardian-native" / "gaps").mkdir(parents=True)
    (tmp_path / "trade_calendar").mkdir(parents=True)
    shutil.copyfile(snapshot / "guardian-upgrade-attempts.json", tmp_path / "guardian-upgrade-attempts.json")
    shutil.copyfile(snapshot / "gaps" / "2026-10-26.json", tmp_path / "guardian-native" / "gaps" / "2026-10-26.json")
    shutil.copyfile(snapshot / "catalog.json", tmp_path / "catalog.json")
    shutil.copyfile(snapshot / "trade_calendar" / "calendar.json",
                    tmp_path / "trade_calendar" / "calendar.json")
    registry = startup.load_registry()
    daily_item = next(item for item in registry if item["id"] == "daily_kline")
    daily_item.setdefault("td_sink", {})["history_min_trading_days"] = 1
    monkeypatch.setattr(startup, "load_registry", lambda: registry)
    _install_fake_td_coverage(startup, monkeypatch)
    monkeypatch.setattr(startup.os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(startup.os, "cpu_count", lambda: 4)
    result = startup.boot_reconcile("2026-10-27", "PRE_MARKET", None, tmp_path,
                                    now=datetime.fromisoformat("2026-10-27T08:30:00+08:00"))
    plan = result["detail"]["plan"]
    action = next(row for row in plan if row["id"] == "daily_kline")
    assert action["partition"] == "2026-10-26" and action["action"] == "FETCH_NOW"
    queued = startup.boot_apply_plan("2026-10-27", plan, tmp_path,
                                     now="2026-10-27T08:30:00+08:00", phase="PRE_MARKET",
                                     enqueue_only=True)
    deferred = next(row for row in queued["detail"]["deferred"] if row["id"] == "daily_kline")
    assert deferred["partition"] == "2026-10-26"
    assert deferred["run_in_phases"] == ["PRE_MARKET"]


def test_sg_load_deferrals_alert_after_three_windows_then_queue_once_under_load(tmp_path, monkeypatch):
    from examples import guardian_handler
    from engine_core.guardian import startup
    from engine_core.guardian.scheduler import default_events

    trade_date = "2026-10-27"
    _calendar(tmp_path, [("2026-10-26", True), (trade_date, True)])
    _catalog(tmp_path, {})
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    _install_fake_td_coverage(startup, monkeypatch)
    daily_item = next(item for item in startup.load_registry() if item["id"] == "daily_kline")
    monkeypatch.setattr(startup, "load_registry", lambda: [daily_item])
    monkeypatch.setattr(startup.os, "getloadavg", lambda: (0.5, 3.0, 0.1))
    monkeypatch.setattr(startup.os, "cpu_count", lambda: 2)
    alerts = []
    monkeypatch.setattr(guardian_handler, "_send_alert",
                        lambda request, _dry: alerts.append(request) or {"status": "sent"})

    events = {event.event_id: event for event in default_events()}
    policy = dict(events["boot_reconcile"].args)
    for phase, now in (("NIGHT", f"{trade_date}T01:00:00+08:00"),
                       ("PRE_MARKET", f"{trade_date}T08:30:00+08:00"),
                       ("MIDDAY_BREAK", f"{trade_date}T12:00:00+08:00")):
        code, result = guardian_handler._run({"handler": "boot_reconcile", "trade_date": trade_date,
            "phase": phase, "now": now, **policy})
        assert code == 0
        action = next(row for row in result["detail"]["plan"] if row["id"] == "daily_kline")
        assert action["action"] == "DEFER" and action["deferred_by_load"] is True

    state = json.loads((tmp_path / "guardian-upgrade-attempts.json").read_text())["2026-10-26"]
    assert state["attempts"] == 0
    assert state["deferred_by_load"] is True
    assert state["consecutive_load_defer_windows"] == 3
    assert len(alerts) == 1

    code, queued = guardian_handler._run({"handler": "boot_apply_plan", "event": "runtime_reconcile_1540",
        "trade_date": trade_date, "phase": "CLOSE", "now": f"{trade_date}T15:40:00+08:00",
        "args": dict(events["runtime_reconcile_1540"].args)})
    assert code == 0
    row = next(row for row in queued["detail"]["results"] if row["id"] == "daily_kline")
    assert row["status"] == "queued" and row["load_override_once"] is True
    state = json.loads((tmp_path / "guardian-upgrade-attempts.json").read_text())["2026-10-26"]
    assert state["attempts"] == 0
    assert state["load_override_queued_window"] == f"{trade_date}:CLOSE"


def test_sh_low_load_reconcile_queues_daily_kline_normally(tmp_path, monkeypatch):
    from examples import guardian_handler
    from engine_core.guardian import startup
    from engine_core.guardian.scheduler import default_events

    trade_date = "2026-10-27"
    _calendar(tmp_path, [("2026-10-26", True), (trade_date, True)])
    _catalog(tmp_path, {})
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    _install_fake_td_coverage(startup, monkeypatch)
    daily_item = next(item for item in startup.load_registry() if item["id"] == "daily_kline")
    monkeypatch.setattr(startup, "load_registry", lambda: [daily_item])
    monkeypatch.setattr(startup.os, "getloadavg", lambda: (0.5, 1.0, 0.1))
    monkeypatch.setattr(startup.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(guardian_handler, "_send_alert",
                        lambda *_args, **_kwargs: {"status": "unexpected"})
    event = next(event for event in default_events() if event.event_id == "boot_reconcile")

    code, result = guardian_handler._run({"handler": "boot_reconcile", "trade_date": trade_date,
        "phase": "POST_MARKET", "now": f"{trade_date}T18:00:00+08:00",
        "args": dict(event.args)})

    assert code == 0
    action = next(row for row in result["detail"]["plan"] if row["id"] == "daily_kline")
    assert action["action"] == "FETCH_NOW"
    assert not (tmp_path / "guardian-upgrade-attempts.json").exists()
