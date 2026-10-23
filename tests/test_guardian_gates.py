from datetime import date, datetime

import pytest

from engine_core.guardian.gates import auction_snapshot_ready, checkpoint_ready


def test_snapshot_gate_uses_universe_and_exact_threshold():
    event = type("Event", (), {"gate": "snapshot_0920_ready"})()
    now = datetime.fromisoformat("2026-10-19T09:20:00+08:00")
    assert auction_snapshot_ready(event, now, {"td": FakeTD(4500), "universe_count": 5000})
    later = now.replace(second=1)
    assert not auction_snapshot_ready(event, later, {"td": FakeTD(4499), "universe_count": 5000})


def test_checkpoint_cutoff_and_unavailable_fail_closed():
    event = type("Event", (), {"gate": "checkpoint_ready", "handler": "open_report",
                                  "start": "09:32:10"})()
    now = datetime.fromisoformat("2026-10-19T09:32:10+08:00")
    assert checkpoint_ready(event, now, {"checkpoint_count": 4000, "checkpoint_ts": "09:32:00"})
    assert not checkpoint_ready(event, now.replace(second=11), {"checkpoint_count": 3999, "checkpoint_ts": "09:32:00"})
    assert not checkpoint_ready(event, now.replace(second=12), {"td_unavailable": True})


def test_snapshot_gate_td_exception_fails_closed():
    event = type("Event", (), {"gate": "snapshot_0920_ready"})()
    now = datetime.fromisoformat("2026-10-19T09:20:03+08:00")
    class BrokenTD:
        def snapshot_count(self, *_):
            raise RuntimeError("backend details must not escape")
    assert not auction_snapshot_ready(event, now, {"td": BrokenTD(), "universe_count": 5000})


def test_snapshot_universe_query_failure_uses_logged_fallback(monkeypatch, caplog):
    import logging
    import engine_core.guardian.gates as gates
    caplog.set_level(logging.INFO)
    calls = []
    def query(sql):
        calls.append(sql)
        return [(str(i),) for i in range(4000)]
    monkeypatch.setattr(gates, "_query", query)
    now = datetime.fromisoformat("2026-10-19T09:20:06+08:00")
    stats = gates.auction_snapshot_stats(now, "0920")
    assert stats == {"row_count": 4000, "universe_count": 4500, "coverage": 4000 / 4500}
    assert "trusted universe unavailable; using fallback=4500" in caplog.text
    assert len(calls) == 1
    assert "private backend detail" not in caplog.text


class FakeTD:
    def __init__(self, count):
        self.count = count

    def snapshot_count(self, day, tag):
        return self.count


def test_intraday_0933_uses_093300_window_not_090000(monkeypatch):
    from engine_core.guardian.scheduler import default_events
    event = next(e for e in default_events() if e.event_id == "intraday_33")
    calls = []
    def query(sql):
        calls.append(sql)
        return [(str(i),) for i in range(4000)] if "09:33:00" in sql else []
    monkeypatch.setattr("engine_core.guardian.gates._query", query)
    now = datetime.fromisoformat("2026-10-19T09:33:05+08:00")
    assert checkpoint_ready(event, now, {})
    assert all("09:00:00" not in sql for sql in calls)


def test_intraday_deadlines_cover_their_three_minute_data_windows():
    from engine_core.guardian.scheduler import default_events
    events = {e.event_id: e for e in default_events()}
    assert {event_id: events[event_id].deadline
            for event_id in ("intraday_36", "intraday_39", "intraday_42", "intraday_45")} == {
                "intraday_36": "09:38:59",
                "intraday_39": "09:41:59",
                "intraday_42": "09:44:59",
                "intraday_45": "09:48:59",
            }


def test_same_tick_cache_does_not_confuse_two_event_cutoffs(monkeypatch):
    from engine_core.guardian.scheduler import default_events
    events = {e.event_id: e for e in default_events()}
    def query(sql):
        return [(str(i),) for i in range(4000)] if "09:45:18" in sql else []
    monkeypatch.setattr("engine_core.guardian.gates._query", query)
    now = datetime.fromisoformat("2026-10-19T09:45:30+08:00")
    assert not checkpoint_ready(events["intraday_45"], now, {})
    assert checkpoint_ready(events["open_report_0945"], now, {})


def test_snapshot_partial_observed_ticks_cannot_be_full_market_denominator(monkeypatch):
    from engine_core.guardian.scheduler import default_events
    event = next(e for e in default_events() if e.event_id == "auction_0925_check")
    monkeypatch.setattr("engine_core.guardian.gates._query", lambda sql: [(str(i),) for i in range(1000)])
    now = datetime.fromisoformat("2026-10-19T09:25:10+08:00")
    assert not auction_snapshot_ready(event, now, {})


def test_checkpoint_never_queries_future_window(monkeypatch):
    from engine_core.guardian.scheduler import default_events
    event = next(e for e in default_events() if e.event_id == "open_report_0945")
    calls = []
    monkeypatch.setattr("engine_core.guardian.gates._query", lambda sql: calls.append(sql) or [(str(i),) for i in range(4000)])
    now = datetime.fromisoformat("2026-10-19T09:45:10+08:00")
    assert not checkpoint_ready(event, now, {})
    assert calls == []


@pytest.mark.parametrize(("event_id", "start", "cutoff"), [
    ("open_report_0932", "09:32:10", "09:32:00"),
    ("open_report_0935", "09:35:10", "09:35:00"),
    ("open_report_0940", "09:40:10", "09:40:00"),
    ("open_report_0945", "09:45:30", "09:45:18"),
])
def test_report_gate_cutoff_matches_report_checkpoint(event_id, start, cutoff):
    from engine_core.guardian.gates import _checkpoint_cutoff
    from engine_core.guardian.scheduler import default_events

    event = next(e for e in default_events() if e.event_id == event_id)
    assert event.start == start
    assert _checkpoint_cutoff(event, date(2026, 10, 19)).strftime("%H:%M:%S") == cutoff


def test_checkpoint_timeout_returns_diagnostic_reason(monkeypatch):
    import time
    import engine_core.guardian.gates as gates
    from engine_core.guardian.scheduler import default_events

    event = next(e for e in default_events() if e.event_id == "intraday_33")
    monkeypatch.setenv("GUARDIAN_TD_QUERY_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr(gates, "_query", lambda _sql: (time.sleep(0.1), [])[1])
    gates._CACHE.clear()
    result = checkpoint_ready(event, datetime.fromisoformat("2026-10-19T09:33:05+08:00"), {})
    assert not result
    assert result["reason"] == "TD_QUERY_TIMEOUT"
    assert result["query_window"] == ["2026-10-19 09:33:00", "2026-10-19 09:33:03"]
    assert result["threshold"] == gates.MIN_CHECKPOINT_SYMBOLS
    assert result["elapsed_ms"] < 100


def test_handler_gate_emits_diagnostic_reason_json(monkeypatch, capsys):
    import json
    from examples import guardian_handler
    from engine_core.guardian import gates

    monkeypatch.setattr(gates, "default_gates", lambda: {"checkpoint_ready": lambda *_: {
        "status": "not_ready", "reason": "TD_QUERY_TIMEOUT", "row_count": 0,
        "query_window": ["09:33:00", "09:33:03"], "elapsed_ms": 15,
        "exception_type": "TimeoutError"}})
    code = guardian_handler._gate("checkpoint_ready", {"event": "intraday_33"})
    payload = json.loads(capsys.readouterr().out)
    assert code == 75 and payload["reason"] == "TD_QUERY_TIMEOUT"
    assert payload["query_window"] == ["09:33:00", "09:33:03"]
