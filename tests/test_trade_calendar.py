from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine_core.data_pipeline.trade_calendar import (
    CalendarConflict, CalendarSourceError, TradeCalendarStore,
)

FIXTURE = Path(__file__).parent / "fixtures" / "trade_calendar"

def _c325():
    payload = json.loads((FIXTURE / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    dates = [row["calendar_date"] for row in payload["rows"] if row["is_trading_day"] == "1"]
    raw = {p.name: p.read_bytes() for p in FIXTURE.glob("szse-*.json")}
    raw["baostock.json"] = (FIXTURE / "baostock-2026-01-01_2026-12-31.json").read_bytes()
    return dates, raw


def test_store_publishes_immutable_calendar_and_queries_authority(tmp_path):
    store = TradeCalendarStore(tmp_path)
    dates, raw = _c325()
    published = store.publish_confirmed(
        dates,
        source="C325 BaoStock+SZSE",
        fetched_at="2026-10-20T00:00:00+00:00",
        coverage_start="2026-01-01",
        coverage_end="2026-12-31",
        raw_responses=raw,
    )
    status = store.calendar_status(date(2026, 10, 20))
    assert published["window"] == {
        "start": published["coverage_start"], "end": published["coverage_end"]}
    assert status["window"] == published["window"]
    assert status["trading_dates"] == published["trading_dates"]
    assert store.is_trading_day("2026-10-19") is True
    assert store.is_trading_day("2026-10-18") is False
    assert store.previous_trade_date("2026-10-20") == date(2026, 10, 19)
    assert store.next_trade_date("2026-10-19") == date(2026, 10, 20)
    assert store.trading_dates("2026-10-19", "2026-10-21") == [d for d in dates if "2026-10-19" <= d <= "2026-10-21"]
    assert store.calendar_status(date(2026, 10, 20))["status"] == "CONFIRMED"
    assert (tmp_path / "trade_dates.json").exists()


def test_agreed_future_closure_replaces_old_future_value(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = TradeCalendarStore(tmp_path)
    dates, raw = _c325()
    store.publish_confirmed(dates,
        source="BaoStock+SZSE", fetched_at="2026-10-20T00:00:00+00:00",
        coverage_start="2026-01-01", coverage_end="2026-12-31", raw_responses=raw)
    store.publish_confirmed([d for d in dates if d != "2026-10-21"],
        source="BaoStock+SZSE", fetched_at="2026-10-20T00:01:00+00:00",
        coverage_start="2026-01-01", coverage_end="2026-12-31", raw_responses=raw)
    assert not store.is_trading_day("2026-10-21")


def test_c325_fixture_hashes_and_source_payloads_are_used():
    fixture = Path(__file__).parent / "fixtures" / "trade_calendar"
    provenance = json.loads((fixture / "provenance.json").read_text())
    for name, expected in provenance["sha256"].items():
        assert hashlib.sha256((fixture / name).read_bytes()).hexdigest() == expected
    payload = json.loads((fixture / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    assert len(payload["rows"]) == 365
    szse = json.loads((fixture / "szse-2026-01.json").read_bytes())
    assert len(szse["data"]) == 31


def test_failure_and_historical_conflict_keep_latest_unchanged(tmp_path):
    store = TradeCalendarStore(tmp_path)
    dates, raw = _c325()
    manifest = store.publish_confirmed(
        dates, source="C325 BaoStock+SZSE", fetched_at="2026-10-20T00:00:00+00:00",
        coverage_start="2026-01-01", coverage_end="2026-12-31", raw_responses=raw)
    latest = (store.directory / "latest").read_text()

    def broken_source(_start, _end):
        raise CalendarSourceError("FIXTURE_TIMEOUT")

    failed = store.refresh(now=date(2026, 10, 20), baostock_fetch=broken_source)
    assert failed["status"] == "STALE"
    assert (store.directory / "latest").read_text() == latest

    with pytest.raises(CalendarConflict):
        store.publish_confirmed(
            [d for d in dates if d != "2026-10-19"], source="C325 derived conflict",
            fetched_at="2026-10-20T01:00:00+00:00", coverage_start="2026-01-01",
            coverage_end="2026-12-31", raw_responses=raw)
    assert (store.directory / "latest").read_text() == latest
    assert manifest["status"] == "CONFIRMED"
    assert list((store.directory / "pending-review").iterdir())


def test_szse_api_parser_reads_archived_real_response(tmp_path, monkeypatch):
    import io
    import engine_core.data_pipeline.trade_calendar as module
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    body = (FIXTURE / "szse-2026-01.json").read_bytes()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(body))
    days, raw = module.fetch_szse_month(2026, 1)
    assert raw == body
    assert len(days) == 20


def test_refresh_wires_both_archived_sources_and_records_evidence(tmp_path):
    from datetime import date
    store = TradeCalendarStore(tmp_path)
    bao, szse = _c325()
    bao_payload = json.loads((FIXTURE / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    def fetch_bao(start, end):
        fields = ["calendar_date", "is_trading_day"]
        rows = [[row["calendar_date"], row["is_trading_day"]] for row in bao_payload["rows"]
                if str(start) <= row["calendar_date"] <= str(end)]
        raw = json.dumps({"fields": fields, "rows": rows}, separators=(",", ":")).encode()
        return [row[0] for row in rows if row[1] == "1"], raw, "BaoStock"
    def fetch_szse(year, month):
        body = (FIXTURE / f"szse-{year}-{month:02d}.json").read_bytes()
        payload = json.loads(body)
        return [row["jyrq"] for row in payload["data"] if str(row["jybz"]) == "1"], body
    result = store.refresh(now=date(2026, 10, 20), baostock_fetch=fetch_bao, szse_fetch=fetch_szse)
    assert result["status"] == "CONFIRMED"
    manifest = json.loads((store.directory / "versions" / result["version"] / "manifest.json").read_text())
    assert manifest["validation"]["confirmed_segments"][0]["sources"]


def test_calendar_alert_dedup_survives_reason_switch_and_restart(tmp_path, monkeypatch):
    import engine_core.report_mailer as mail
    import examples.core_guardian as cli
    from datetime import datetime

    reasons = iter(("SOURCE_TIMEOUT", "SOURCE_CONFLICT", "SOURCE_TIMEOUT", "SOURCE_TIMEOUT"))
    monkeypatch.setattr(TradeCalendarStore, "refresh_if_needed", lambda *a, **kw: {
        "status": "STALE", "reason_code": next(reasons), "version": "calendar-v1"})
    sent = []
    monkeypatch.setattr(mail, "load_smtp_config", lambda: SimpleNamespace(from_addr="reports@localhost"))
    monkeypatch.setattr(mail, "send_message", lambda *a, **kw:
                        sent.append((a, kw)) or SimpleNamespace(status="FAKE_SMTP_CAPTURED"))
    cli._ALERTED.clear()
    event = next(item for item in cli.default_events() if item.handler == "calendar_refresh")
    opts = SimpleNamespace(email_self=True, run=True, simulate_clock=None)
    now = datetime.fromisoformat("2026-10-20T08:00:00+08:00")
    for _ in range(4):
        handler = cli._command_handler("calendar_refresh", opts, tmp_path)
        assert handler(event, now, {}, False).startswith("DEGRADED:calendar_refresh:")
        cli._ALERTED.clear()  # model a process restart between refreshes
    assert len(sent) == 2
    assert json.loads((tmp_path / "guardian" / "calendar_alerts.json").read_text()) == {
        "2026-10-20": ["SOURCE_CONFLICT", "SOURCE_TIMEOUT"]}


def test_auction_baseline_reports_insufficient_calendar_history(tmp_path):
    from engine_core.auction_baseline import _expected_sessions
    from engine_core.calendar import CalendarCoverageError
    from engine_core.data_pipeline.storage import ArtifactCatalog

    store = TradeCalendarStore(tmp_path)
    dates, raw = _c325()
    store.publish_confirmed(
        ["2026-10-19"], source="C325 bounded history", fetched_at="2026-10-20T00:00:00+00:00",
        coverage_start="2026-01-01", coverage_end="2026-12-31", raw_responses=raw)
    with pytest.raises(CalendarCoverageError, match="calendar_history_insufficient:required=64"):
        _expected_sessions(ArtifactCatalog(tmp_path), "2026-10-20", dates)
