import json
from datetime import datetime


def _matrix(root, days):
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

    path = root / "trade_calendar" / "calendar.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"d": day, "t": int(trading), "src": "TEST", "u": "test"}
            for day, trading in days]
    path.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1",
                                "lit_from": rows[0]["d"], "lit_to": rows[-1]["d"],
                                "days": rows}))
    return TradeCalendarStore(root)


def test_startup_event_ids_route_with_go_contract_shape(tmp_path, monkeypatch):
    from examples import guardian_handler
    from engine_core.guardian import startup

    _matrix(tmp_path, [("2026-10-19", True), ("2026-10-20", True), ("2026-10-21", True)])
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(startup, "_health_probes", lambda *_: {"td": {"status": "ok"},
                                                                  "redis": {"status": "ok"}})
    for event_id in ("boot_trade_day", "boot_data_health", "boot_reconcile"):
        code, payload = guardian_handler._run({"contract": 1, "event": event_id,
            "trade_date": "2026-10-20", "phase": "PRE_MARKET",
            "now": "2026-10-20T08:30:00+08:00"})
        assert code == 0, (event_id, payload)
        assert payload["contract"] == 1 and payload["status"] in {"ok", "degraded"}


def test_trade_day_unknown_when_calendar_absent_or_uncovered(tmp_path):
    from engine_core.guardian.startup import boot_trade_day

    result = boot_trade_day("2026-10-20", tmp_path)
    assert result["status"] == "failed" and result["error_code"] == "CALENDAR_UNKNOWN"
    _matrix(tmp_path, [("2026-10-19", True)])
    result = boot_trade_day("2026-10-20", tmp_path)
    assert result["error_code"] == "CALENDAR_UNKNOWN"


def test_non_trading_day_skips_freshness_overdue_and_dry_run_does_not_write(tmp_path, monkeypatch):
    from engine_core.guardian.startup import boot_data_health

    _matrix(tmp_path, [("2026-10-19", True), ("2026-10-20", False), ("2026-10-21", True)])
    monkeypatch.setattr("engine_core.guardian.startup._health_probes",
                        lambda *_: {"td": {"status": "ok"}, "redis": {"status": "ok"}})
    result = boot_data_health("2026-10-20", "2026-10-19", tmp_path, dry_run=True)
    assert result["status"] == "ok"
    assert not (tmp_path / "guardian-native" / "health" / "2026-10-20.json").exists()


def test_health_catalog_statuses_and_reconcile_owner_events(tmp_path, monkeypatch):
    from engine_core.guardian.startup import boot_data_health, boot_reconcile

    _matrix(tmp_path, [("2026-10-19", True), ("2026-10-20", True), ("2026-10-21", True)])
    (tmp_path / "catalog.json").write_text(json.dumps({"datasets": {
        "symbol_universe": {"dates": {"2026-10-20": [{"row_count": 1,
            "available_at": "2026-10-20T08:00:00+08:00", "quality": {"status": "OK"}}]}},
        "daily_kline": {"dates": {}}, "stock_static": {"dates": {}}
    }}))
    monkeypatch.setattr("engine_core.guardian.startup._health_probes",
                        lambda *_: {"td": {"status": "failed"}, "redis": {"status": "ok"}})
    health = boot_data_health("2026-10-20", "2026-10-19", tmp_path,
                              now=datetime.fromisoformat("2026-10-20T20:00:00+08:00"), dry_run=True)
    assert health["status"] == "degraded"
    assert {item["status"] for item in health["detail"]["items"]} >= {"OK", "MISSING", "UNKNOWN"}
    rec = boot_reconcile("2026-10-20", "POST_MARKET", health["detail"], tmp_path,
                         now=datetime.fromisoformat("2026-10-20T20:00:00+08:00"))
    assert rec["status"] == "degraded"
    assert any(x.get("recommendation") == "NO_OWNER_EVENT" for x in rec["detail"]["gaps"])


def test_boot_apply_plan_executes_jobs_defers_and_appends_gaps(tmp_path, monkeypatch):
    from engine_core.guardian.startup import boot_apply_plan

    registry = [
        {"id": "light", "artifact": {"catalog_dataset": "light"},
         "fetch_job": ["stub", "{date}", "{item}"]},
        {"id": "heavy", "artifact": {"catalog_dataset": "heavy"},
         "fetch_job": ["stub", "{date}", "{item}"]},
        {"id": "capture", "artifact": {"catalog_dataset": "capture"}},
    ]
    plan = [
        {"id": "light", "partition": "2026-10-20", "action": "FETCH_NOW", "lane": "parallel"},
        {"id": "heavy", "partition": "2026-10-19", "action": "DEFER", "run_at": "MIDDAY_BREAK"},
        {"id": "capture", "partition": "2026-10-20", "action": "GAP"},
    ]
    calls = []
    monkeypatch.setattr("engine_core.guardian.startup.load_registry", lambda: registry)
    monkeypatch.setattr("engine_core.guardian.startup._run_fetch_job",
                        lambda item, day, timeout, _root: calls.append((item["id"], day)) or {"status": "ok"})
    result = boot_apply_plan("2026-10-20", plan, tmp_path, now="2026-10-20T09:36:00+08:00", phase="OPEN")
    assert calls == [("light", "2026-10-20")]
    assert result["detail"]["deferred"] == [{"id": "heavy", "partition": "2026-10-19",
                                                  "fetch_job": ["stub", "{date}", "{item}"],
                                                  "run_in_phases": ["MIDDAY_BREAK"]}]
    gap_path = tmp_path / "guardian-native" / "gaps" / "2026-10-20.json"
    assert json.loads(gap_path.read_text())[0]["id"] == "capture"
    boot_apply_plan("2026-10-20", [{"id": "capture", "partition": "2026-10-20",
        "action": "GAP", "reason": "repeat"}], tmp_path,
        now="2026-10-20T09:37:00+08:00", phase="OPEN")
    gap_rows = json.loads(gap_path.read_text())
    assert len(gap_rows) == 1 and gap_rows[0]["reason"] == "repeat"
    assert gap_rows[0]["observed_at"] == "2026-10-20T09:37:00+08:00"
    assert {x["id"]: x["action"] for x in result["detail"]["results"]} == {
        "light": "FETCH_NOW", "heavy": "DEFER", "capture": "GAP"}


def test_boot_apply_plan_isolates_failed_fetch_and_keeps_later_item_running(tmp_path, monkeypatch):
    import sys
    from engine_core.guardian import startup

    registry = [
        {"id": "broken", "fetch_job": {"argv": [sys.executable, "-c",
            "import sys;sys.stderr.write('x'*450);sys.exit(3)"]}},
        {"id": "good", "fetch_job": {"argv": [sys.executable, "-c",
            "import json;print(json.dumps(dict(status='ok'))) "]}},
    ]
    monkeypatch.setattr(startup, "load_registry", lambda: registry)
    result = startup.boot_apply_plan("2026-10-20", [
        {"id": "broken", "partition": "2026-10-20", "action": "FETCH_NOW"},
        {"id": "good", "partition": "2026-10-20", "action": "FETCH_NOW"},
    ], tmp_path, now="2026-10-20T09:36:00+08:00", phase="OPEN")
    rows = {row["id"]: row for row in result["detail"]["results"]}
    assert result["status"] == "degraded" and result["retryable"] is True
    assert rows["broken"]["status"] == "failed"
    assert rows["broken"]["error_code"] == "FETCH_EXIT_3"
    assert len(rows["broken"]["stderr"]) == 300
    assert rows["broken"]["detail"]["item_id"] == "broken"
    assert rows["broken"]["detail"]["error_code"] == "FETCH_EXIT_3"
    assert rows["broken"]["detail"]["elapsed_ms"] == rows["broken"]["elapsed_ms"]
    assert rows["good"]["status"] == "ok"


def test_historical_hot_plates_is_reconciled_separately_from_daily_capture():
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider
    from engine_core.guardian.reconcile_plan import reconcile_plan
    from engine_core.guardian.registry import load_registry
    import pytest

    items = load_registry()
    catalog = {"datasets": {"hot_plates": {"dates": {"2026-10-21": [{"version": "today"}]}}}}
    plan = reconcile_plan(datetime.fromisoformat("2026-10-21T12:30:00+08:00"), catalog,
                          trade_date="2026-10-21", prev_trade_date="2026-10-20",
                          phase="MIDDAY_BREAK", registry=items)
    prior = [item for item in plan if item["id"] == "hot_plates_yesterday"]
    assert len(prior) == 1
    # Kaipan history cannot return a past day's plates (see Client below), so D-1 is a recorded gap, never a fetch.
    assert prior[0]["action"] == "GAP" and prior[0]["partition"] == "2026-10-20"

    class Client:
        def get_his_plates(self, *_args):
            # Captured from real Kaipan: request 2026-10-20 returned Day=2026-10-21.
            return {"Day": ["2026-10-21"], "Min": "0925", "Max": "1500",
                    "List": [{"code": "801159"}]}

    with pytest.raises(ValueError, match="kaipan_hot_plates_response_date_mismatch"):
        KaipanProvider(Client()).hot_plates("2026-10-20")


def test_fetch_item_dry_run_does_not_call_job(tmp_path, monkeypatch):
    from engine_core.guardian.startup import fetch_item

    monkeypatch.setattr("engine_core.guardian.startup.load_registry", lambda: [
        {"id": "sample", "fetch_job": ["stub"]}])
    monkeypatch.setattr("engine_core.guardian.startup._run_fetch_job",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("executed")))
    result = fetch_item("sample", "2026-10-19", tmp_path, dry_run=True)
    assert result["status"] == "ok" and result["detail"]["dry_run"] is True


def test_boot_apply_plan_runs_stub_job_into_temporary_catalog(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.guardian import startup

    stub = tmp_path / "stub_fetch.py"
    stub.write_text(
        "import json, os, sys\n"
        "from engine_core.data_pipeline.storage import ArtifactCatalog\n"
        "day, item = sys.argv[1:]\n"
        "catalog = ArtifactCatalog(os.environ['CORE_DATA_ROOT'])\n"
        "manifest = catalog.commit(item, day, [{'trade_date': day, 'symbol': '000001'}],\n"
        "    {'trade_date': day, 'available_at': '2026-10-20T09:36:01+08:00'}, source='stub')\n"
        "print(json.dumps({'version':manifest['version'],'row_count':manifest['row_count']}))\n",
        encoding="utf-8")
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path / "catalog"))
    source_root = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setenv("PYTHONPATH", source_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    registry = [{"id": "stub_dataset", "artifact": {"catalog_dataset": "stub_dataset"},
                 "fetch_job": {"argv": [sys.executable, str(stub), "{date}", "{item}"],
                               "timeout_seconds": 10}}]
    monkeypatch.setattr(startup, "load_registry", lambda: registry)
    result = startup.boot_apply_plan("2026-10-20", [{"id": "stub_dataset", "partition": "2026-10-20",
        "action": "FETCH_NOW", "lane": "parallel"}], tmp_path,
        now="2026-10-20T09:36:00+08:00", phase="OPEN")
    assert result["status"] == "ok"
    manifest_path = ArtifactCatalog(tmp_path / "catalog").locate("stub_dataset", "2026-10-20")
    assert manifest_path is not None
    manifest = __import__("json").loads(manifest_path.read_text())
    assert manifest["available_at"] == "2026-10-20T09:36:01+08:00"


def test_boot_apply_plan_runs_dependency_layers_and_propagates_failure(tmp_path, monkeypatch):
    from engine_core.guardian import startup

    registry = [
        {"id": "plate_members", "fetch_job": {"argv": ["stub"]}},
        {"id": "stock_theme_assignment", "fetch_job": {"argv": ["stub"]}},
    ]
    monkeypatch.setattr(startup, "load_registry", lambda: registry)
    calls = []

    def run(item, _day, _timeout, _root):
        calls.append(item["id"])
        if item["id"] == "plate_members":
            raise RuntimeError("upstream failed")
        return {"status": "ok"}

    monkeypatch.setattr(startup, "_run_fetch_job", run)
    result = startup.boot_apply_plan("2026-10-20", [
        {"id": "stock_theme_assignment", "partition": "2026-10-20", "action": "FETCH_NOW",
         "lane": "parallel", "depends_on": ["plate_members"]},
        {"id": "plate_members", "partition": "2026-10-20", "action": "FETCH_NOW",
         "lane": "parallel", "depends_on": []},
    ], tmp_path, now="2026-10-20T09:36:00+08:00", phase="OPEN")
    rows = {row["id"]: row for row in result["detail"]["results"]}
    assert calls == ["plate_members"]
    assert rows["stock_theme_assignment"]["status"] == "DEPENDENCY_FAILED"
    assert rows["stock_theme_assignment"]["retryable"] is True
    calls.clear()
    monkeypatch.setattr(startup, "_run_fetch_job",
                        lambda item, _day, _timeout, _root: calls.append(item["id"]) or {"status": "ok"})
    startup.boot_apply_plan("2026-10-20", [
        {"id": "stock_theme_assignment", "partition": "2026-10-20", "action": "FETCH_NOW",
         "lane": "parallel", "depends_on": ["plate_members"]},
        {"id": "plate_members", "partition": "2026-10-20", "action": "FETCH_NOW",
         "lane": "parallel", "depends_on": []},
    ], tmp_path, now="2026-10-20T09:36:00+08:00", phase="OPEN")
    assert calls == ["plate_members", "stock_theme_assignment"]
