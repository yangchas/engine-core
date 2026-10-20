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
        "symbol_universe": {"dates": {"2026-10-20": [{}]}},
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
