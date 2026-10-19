from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine_core.calendar import CalendarCoverageError
from engine_core.data_pipeline.trade_calendar import CalendarSourceError, TradeCalendarStore

FIXTURE = Path(__file__).parent / "fixtures" / "trade_calendar"


def _calendar():
    payload = json.loads((FIXTURE / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    return {row["calendar_date"] for row in payload["rows"] if row["is_trading_day"] == "1"}


def _szse():
    result = set()
    for path in FIXTURE.glob("szse-2026-*.json"):
        result.update(row["jyrq"] for row in json.loads(path.read_bytes())["data"] if str(row["jybz"]) == "1")
    return result


def _source_fetchers(bao=None, sz=None):
    bao = _calendar() if bao is None else set(bao)
    sz = _szse() if sz is None else set(sz)

    def fetch_bao(start, end):
        selected = sorted(day for day in bao if start.isoformat() <= day <= end.isoformat())
        # Preserve the archived response shape, transformed only to the requested window.
        rows = [[(start + timedelta(days=i)).isoformat(),
                 "1" if (start + timedelta(days=i)).isoformat() in bao else "0"]
                for i in range((end - start).days + 1)]
        return selected, json.dumps({"fields": ["calendar_date", "is_trading_day"], "rows": rows}).encode(), "BS"

    def fetch_sz(year, month):
        selected = sorted(day for day in sz if day.startswith(f"{year}-{month:02d}-"))
        raw = (FIXTURE / f"szse-{year}-{month:02d}.json").read_bytes() if (FIXTURE / f"szse-{year}-{month:02d}.json").exists() else b"{}"
        return selected, raw
    return fetch_bao, fetch_sz


def test_c325_fixture_hashes_and_archived_shapes():
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    for name, expected in provenance["sha256"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == expected
    payload = json.loads((FIXTURE / "baostock-2026-01-01_2026-12-31.json").read_bytes())
    assert len(payload["rows"]) == 365
    assert len(json.loads((FIXTURE / "szse-2026-01.json").read_bytes())["data"]) == 31


def test_cold_start_lights_natural_day_matrix_and_keeps_reader_api(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = TradeCalendarStore(tmp_path)
    bao, sz = _source_fetchers()
    result = store.refresh(now=date(2026, 10, 20), baostock_fetch=bao, szse_fetch=sz)
    matrix = json.loads((store.directory / "calendar.json").read_text())
    assert result["status"] == "CONFIRMED"
    assert matrix["lit_from"] == "2026-01-01" and matrix["lit_to"] == "2026-11-19"
    assert len(matrix["days"]) == 323
    assert matrix["days"][0]["src"] == "BS+SZ"
    assert len(store.all_trading_dates()) == len(_calendar() & {d["d"] for d in matrix["days"]})
    assert store.is_trading_day("2026-10-19")
    assert store.previous_trade_date("2026-10-20") == date(2026, 10, 19)
    assert store.next_trade_date("2026-10-19") == date(2026, 10, 20)
    assert store.trading_dates("2026-10-19", "2026-10-21") == ["2026-10-19", "2026-10-20", "2026-10-21"]
    assert json.loads((tmp_path / "trade_dates.json").read_text()) == store.all_trading_dates()
    assert not (store.directory / "versions").exists() and not (store.directory / "latest").exists()


def test_wheel_preserves_past_revises_future_and_rolls_forward(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = TradeCalendarStore(tmp_path)
    old = _calendar()
    bao, sz = _source_fetchers(old, old)
    store.refresh(now=date(2026, 10, 20), baostock_fetch=bao, szse_fetch=sz)
    past_before = store.is_trading_day("2026-10-19")
    future = "2026-11-02"
    revised = old - {future}
    bao2, sz2 = _source_fetchers(revised, revised)
    store.refresh(now=date(2026, 10, 21), baostock_fetch=bao2, szse_fetch=sz2)
    assert store.is_trading_day("2026-10-19") is past_before
    assert not store.is_trading_day(future)
    assert store.is_trading_day("2026-11-18")


def test_single_source_is_lit_truthfully_and_conflicts_do_not_overwrite(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = TradeCalendarStore(tmp_path)
    bao, _ = _source_fetchers()
    def failed_sz(*_):
        raise CalendarSourceError("SZSE_TIMEOUT")
    result = store.refresh(now=date(2026, 10, 20), baostock_fetch=bao, szse_fetch=failed_sz)
    assert result["status"] == "PARTIAL"
    assert {item["src"] for item in store.load()["days"]} == {"BS"}
    prior = store.is_trading_day("2026-10-21")
    bset = _calendar()
    sset = _szse() - {"2026-10-21"}
    bfetch, sfetch = _source_fetchers(bset, sset)
    result = store.refresh(now=date(2026, 10, 20), baostock_fetch=bfetch, szse_fetch=sfetch)
    assert result["reason_code"] == "SOURCE_CONFLICT"
    assert store.is_trading_day("2026-10-21") is prior
    assert store.calendar_status(date(2026, 10, 20))["conflicts"] > 0


def test_total_source_failure_keeps_day_matrix_and_coverage_unknown(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = TradeCalendarStore(tmp_path)
    bao, sz = _source_fetchers()
    store.refresh(now=date(2026, 10, 20), baostock_fetch=bao, szse_fetch=sz)
    before = [dict(day) for day in store.load()["days"]]
    def fail(*_):
        raise CalendarSourceError("FIXTURE_TIMEOUT")
    result = store.refresh(now=date(2026, 10, 20), baostock_fetch=fail, szse_fetch=fail)
    assert result["status"] == "STALE"
    assert [dict(day) for day in store.load()["days"]] == before
    with pytest.raises(CalendarCoverageError):
        store.is_trading_day("2025-12-31")
    with pytest.raises(CalendarCoverageError):
        store.is_trading_day("2026-11-20")


def test_migrates_old_latest_in_place_without_touching_versions(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = TradeCalendarStore(tmp_path)
    dates = sorted(_calendar())
    raw = {p.name: p.read_bytes() for p in FIXTURE.glob("szse-2026-*.json")}
    raw["baostock.json"] = (FIXTURE / "baostock-2026-01-01_2026-12-31.json").read_bytes()
    legacy = store.directory / "versions" / "legacy-v1"
    legacy.mkdir(parents=True)
    snap = {"schema": "CoreTradeCalendarV1", "version": "legacy-v1", "coverage_start": "2026-01-01",
            "coverage_end": "2026-11-20", "trading_dates": [d for d in dates if d <= "2026-11-20"]}
    manifest = {"fetched_at": "2026-10-20T00:00:00+00:00", "raw_responses": {k: {} for k in raw}}
    (legacy / "snapshot.json").write_text(json.dumps(snap))
    (legacy / "manifest.json").write_text(json.dumps(manifest))
    (store.directory / "latest").write_text("legacy-v1\n")
    matrix = store.load()
    assert len([day for day in matrix["days"] if day["t"]]) == len(snap["trading_dates"])
    assert matrix["lit_to"] == "2026-11-20"
    assert (legacy / "snapshot.json").exists()
    assert (store.directory / "calendar.json").exists()


def _file_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _copy_real_legacy_latest(root):
    # Read-only capture: latest and its immutable version are the C342-reviewed real legacy fixture.
    source = Path("/home/exedev/core-data/trade_calendar")
    version = source.joinpath("latest").read_text().strip()
    destination = root / "trade_calendar"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "latest").write_bytes((source / "latest").read_bytes())
    import shutil
    shutil.copytree(source / "versions" / version, destination / "versions" / version)
    return TradeCalendarStore(root)


def test_all_conflicting_window_preserves_days_and_cold_start_stays_unlit(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = TradeCalendarStore(tmp_path)
    bao, sz = _source_fetchers()
    store.refresh(now=date(2026, 10, 20), baostock_fetch=bao, szse_fetch=sz)
    original = {row["d"]: row.copy() for row in store.load()["days"]}

    def contradictory(start, end):
        days = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
        bfetch, _ = _source_fetchers(days - _szse(), _szse())
        values, raw, source = bfetch(start, end)
        return values, raw, source

    result = store.refresh(now=date(2026, 10, 20), baostock_fetch=contradictory, szse_fetch=sz)
    assert result["reason_code"] == "SOURCE_CONFLICT"
    assert {row["d"]: row for row in store.load()["days"]} == original

    cold = TradeCalendarStore(tmp_path / "cold")
    result = cold.refresh(now=date(2026, 10, 20), baostock_fetch=contradictory, szse_fetch=sz)
    assert result["reason_code"] == "SOURCE_CONFLICT"
    assert cold.load() is None


def test_total_failure_preserves_all_file_bytes(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = TradeCalendarStore(tmp_path)
    bao, sz = _source_fetchers()
    store.refresh(now=date(2026, 10, 20), baostock_fetch=bao, szse_fetch=sz)
    before = _file_bytes(tmp_path)
    def fail(*_):
        raise CalendarSourceError("FIXTURE_TIMEOUT")
    result = store.refresh(now=date(2026, 10, 20), baostock_fetch=fail, szse_fetch=fail)
    assert result["status"] == "STALE"
    assert _file_bytes(tmp_path) == before


def test_read_migration_only_writes_calendar_matrix(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = _copy_real_legacy_latest(tmp_path)
    before = _file_bytes(tmp_path)
    matrix = store.load()
    after = _file_bytes(tmp_path)
    assert len(matrix["days"]) == 324
    assert sum(row["t"] for row in matrix["days"]) == 213
    assert matrix["lit_from"] == "2026-01-01" and matrix["lit_to"] == "2026-11-20"
    assert after.keys() - before.keys() == {"trade_calendar/calendar.json"}
    assert all(after[name] == content for name, content in before.items())


def test_guardian_reads_migrated_and_matrix_calendars_without_fetch(tmp_path, monkeypatch):
    from engine_core.data_pipeline import trade_calendar
    import examples.core_guardian as cli
    monkeypatch.setattr(trade_calendar, "_market_today", lambda: date(2026, 10, 20))
    store = _copy_real_legacy_latest(tmp_path)
    monkeypatch.setattr(trade_calendar, "fetch_baostock", lambda *a: pytest.fail("read fetched source"))
    monkeypatch.setattr(trade_calendar, "fetch_szse_month", lambda *a: pytest.fail("read fetched source"))
    migrated = cli._load_dates(tmp_path, None)
    assert "2026-10-19" in migrated
    assert migrated.coverage_start == "2026-01-01" and migrated.coverage_end == "2026-11-20"
    matrix = store.load()
    existing = cli._load_dates(tmp_path, None)
    assert set(existing) == set(store.all_trading_dates())
    with pytest.raises(CalendarCoverageError):
        store.is_trading_day("2027-01-01")


def test_alert_entrypoint_still_deduplicates_calendar_failure(tmp_path, monkeypatch):
    import examples.core_guardian as cli
    import engine_core.report_mailer as mail
    from datetime import datetime
    reasons = iter(("SOURCE_TIMEOUT", "SOURCE_CONFLICT", "SOURCE_TIMEOUT", "SOURCE_TIMEOUT"))
    monkeypatch.setattr(TradeCalendarStore, "refresh_if_needed", lambda *a, **kw: {
        "status": "STALE", "reason_code": next(reasons)})
    sent = []
    monkeypatch.setattr(mail, "load_smtp_config", lambda: SimpleNamespace(from_addr="reports@localhost"))
    monkeypatch.setattr(mail, "send_message", lambda *a, **kw:
                        sent.append((a, kw)) or SimpleNamespace(status="FAKE_SMTP_CAPTURED"))
    cli._ALERTED.clear()
    event = next(item for item in cli.default_events() if item.handler == "calendar_refresh")
    opts = SimpleNamespace(email_self=True, run=True, simulate_clock=None)
    now = datetime.fromisoformat("2026-10-20T08:00:00+08:00")
    for _ in range(4):
        assert cli._command_handler("calendar_refresh", opts, tmp_path)(event, now, {}, False).startswith("DEGRADED:")
        cli._ALERTED.clear()
    assert len(sent) == 2
