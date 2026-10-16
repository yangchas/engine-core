from datetime import date, datetime
from pathlib import Path

import pytest

from engine_core.guardian.scheduler import (
    EventSpec, FileInstanceLock, Guardian, default_events, phase_at,
)


def at(value):
    return datetime.fromisoformat(value).astimezone()


def test_event_order_and_phase_windows():
    events = default_events()
    assert [event.event_id for event in events if event.start == "09:20:03"] == [
        "auction_0920_check"
    ]
    assert phase_at("08:30") == "PREMARKET"
    assert phase_at("09:20") == "AUCTION"
    assert phase_at("10:00") == "INTRADAY"
    assert phase_at("17:40") == "POSTMARKET"
    assert phase_at("02:00") == "NIGHT"


def test_gate_waits_then_triggers_once(tmp_path):
    ready = {"value": False}
    calls = []
    event = EventSpec("x", "AUCTION", "09:20:00", "09:21:00", "g", "h")
    guardian = Guardian([event], calendar_dates={"2026-10-16"}, state_root=tmp_path,
                        gates={"g": lambda *_: ready["value"]},
                        handlers={"h": lambda *_: calls.append("ran")})
    guardian.tick(datetime.fromisoformat("2026-10-16T09:20:10+08:00"))
    assert calls == []
    assert guardian.status("2026-10-16", "x") == "WAITING"
    ready["value"] = True
    guardian.tick(datetime.fromisoformat("2026-10-16T09:20:20+08:00"))
    guardian.tick(datetime.fromisoformat("2026-10-16T09:20:21+08:00"))
    assert calls == ["ran"]


def test_deadline_degraded_and_missed(tmp_path):
    calls = []
    degraded = EventSpec("degraded", "AUCTION", "09:20:00", "09:21:00", "no", "h",
                         degraded_at_deadline=True)
    missed = EventSpec("missed", "AUCTION", "09:20:00", "09:21:00", "no", "h")
    guardian = Guardian([degraded, missed], calendar_dates={"2026-10-16"}, state_root=tmp_path,
                        gates={"no": lambda *_: False}, handlers={"h": lambda event, *_: calls.append(event.event_id)})
    guardian.tick(datetime.fromisoformat("2026-10-16T09:21:00+08:00"))
    assert calls == ["degraded"]
    assert guardian.status("2026-10-16", "degraded") == "SUCCEEDED_DEGRADED"
    assert guardian.status("2026-10-16", "missed") == "MISSED"


def test_late_start_catchup_and_restart_idempotency(tmp_path):
    calls = []
    event = EventSpec("x", "AUCTION", "09:20:00", "09:25:00", "always", "h")
    kwargs = dict(calendar_dates={"2026-10-16"}, state_root=tmp_path,
                  gates={"always": lambda *_: True}, handlers={"h": lambda *_: calls.append(1)})
    first = Guardian([event], **kwargs)
    first.tick(datetime.fromisoformat("2026-10-16T09:24:00+08:00"))
    second = Guardian([event], **kwargs)
    second.tick(datetime.fromisoformat("2026-10-16T09:24:01+08:00"))
    assert calls == [1]
    assert second.status("2026-10-16", "x") == "SUCCEEDED"


def test_retry_backoff_and_circuit_breaker(tmp_path):
    calls = []
    event = EventSpec("x", "AUCTION", "09:20:00", "09:30:00", "yes", "h",
                      max_attempts=2, retry_base_seconds=2)
    def fail(*_):
        calls.append(1)
        raise RuntimeError("boom")
    guardian = Guardian([event], calendar_dates={"2026-10-16"}, state_root=tmp_path,
                        gates={"yes": lambda *_: True}, handlers={"h": fail})
    guardian.tick(datetime.fromisoformat("2026-10-16T09:20:00+08:00"))
    guardian.tick(datetime.fromisoformat("2026-10-16T09:20:01+08:00"))
    assert len(calls) == 1
    guardian.tick(datetime.fromisoformat("2026-10-16T09:20:02+08:00"))
    assert len(calls) == 2
    assert guardian.status("2026-10-16", "x") == "CIRCUIT_OPEN"
    assert guardian.degraded


def test_timeout_opens_circuit_without_retrying_a_still_running_handler(tmp_path):
    import time
    event = EventSpec("x", "AUCTION", "09:20:00", "09:30:00", "yes", "slow",
                      max_attempts=3, timeout_seconds=1)
    guardian = Guardian([event], calendar_dates={"2026-10-16"}, state_root=tmp_path,
                        gates={"yes": lambda *_: True},
                        handlers={"slow": lambda *_: time.sleep(1.2)})
    guardian.tick(datetime.fromisoformat("2026-10-16T09:20:00+08:00"))
    guardian.tick(datetime.fromisoformat("2026-10-16T09:20:01+08:00"))
    assert guardian.status("2026-10-16", "x") == "CIRCUIT_OPEN"
    time.sleep(0.25)


def test_instance_lock_is_exclusive(tmp_path):
    one = FileInstanceLock(tmp_path / "guardian.lock")
    two = FileInstanceLock(tmp_path / "guardian.lock")
    one.acquire()
    with pytest.raises(RuntimeError, match="already_running"):
        two.acquire()
    one.release()
    two.acquire()
    two.release()


def test_calendar_missing_fails_closed_and_holiday_does_not_trigger(tmp_path):
    event = EventSpec("x", "INTRADAY", "09:30:00", "09:31:00", "yes", "h")
    calls = []
    unknown = Guardian([event], calendar_dates=None, state_root=tmp_path / "unknown",
                       handlers={"h": lambda *_: calls.append(1)})
    unknown.tick(datetime.fromisoformat("2026-10-16T09:30:00+08:00"))
    assert unknown.degraded
    holiday = Guardian([event], calendar_dates=set(), state_root=tmp_path / "holiday",
                       handlers={"h": lambda *_: calls.append(1)})
    holiday.tick(datetime.fromisoformat("2026-10-17T09:30:00+08:00"))
    assert calls == []
    assert holiday.phase("2026-10-17T09:30:00+08:00") == "HOLIDAY"


def test_dry_run_does_not_call_handlers_or_persist(tmp_path):
    event = EventSpec("x", "AUCTION", "09:20:00", "09:21:00", "yes", "h")
    calls = []
    guardian = Guardian([event], calendar_dates={"2026-10-16"}, state_root=tmp_path,
                        gates={"yes": lambda *_: True}, handlers={"h": lambda *_: calls.append(1)},
                        dry_run=True)
    planned = guardian.tick(datetime.fromisoformat("2026-10-16T09:20:00+08:00"))
    assert guardian.tick(datetime.fromisoformat("2026-10-16T09:20:01+08:00")) == []
    guardian.tick(datetime.fromisoformat("2026-10-16T09:22:00+08:00"))
    assert planned[0]["status"] == "WOULD_RUN"
    assert calls == []
    assert not (tmp_path / "2026-10-16" / "state.json").exists()


def test_default_event_table_has_full_day_sampling_and_daily_sequence():
    ids = {event.event_id for event in default_events()}
    assert {"startup_0830", "premarket_slice_0850", "auction_report_0926", "open_report_0932", "close_mark_1505",
            "baostock_probe_1530", "baostock_retry_1740", "baostock_retry_1830",
            "baostock_retry_2000", "night_recap"} <= ids
    hot = [event for event in default_events() if event.handler == "kaipan_hot_capture"]
    assert len(hot) == 18

