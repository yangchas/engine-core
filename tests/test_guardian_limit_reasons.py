from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from engine_core.guardian.scheduler import Guardian, default_events


TZ = timezone(timedelta(hours=8))


def _target_events():
    wanted = {"limit_pool_d1", "limit_reasons_d1"}
    return [event for event in default_events() if event.event_id in wanted]


def test_limit_reasons_waits_for_yesterday_pool_then_retries_and_catches_up(tmp_path):
    events = _target_events()
    assert [event.event_id for event in events] == ["limit_pool_d1", "limit_reasons_d1"]
    reasons = events[1]
    assert reasons.depends_on == ("limit_pool_d1",)
    assert reasons.retry_backoff == "5m"
    assert reasons.retry_until_late is True
    assert reasons.silent_until_last is False
    assert reasons.catchup_mode == "run_in_window"

    gate = {"pool": False}
    attempts_path = tmp_path / "reason-attempts.json"

    def handler(event, _now, _context, _degraded):
        if event.event_id == "limit_reasons_d1":
            attempts = int(attempts_path.read_text() if attempts_path.exists() else "0") + 1
            attempts_path.write_text(str(attempts))
            if attempts == 1:
                raise RuntimeError("temporary Kaipan failure")
        return event.event_id

    guardian = Guardian(
        events,
        calendar_dates={"2026-10-29"},
        state_root=tmp_path / "state",
        gates={"calendar_ready": lambda event, *_: gate["pool"] or event.event_id != "limit_pool_d1"},
        handlers={"kaipan_fetch": handler},
    )
    actions = guardian.tick(datetime(2026, 10, 29, 0, 45, tzinfo=TZ))
    assert actions == []
    assert guardian.status("2026-10-29", "limit_pool_d1") == "WAITING"
    assert guardian.status("2026-10-29", "limit_reasons_d1") == "PENDING"

    gate["pool"] = True
    actions = guardian.tick(datetime(2026, 10, 29, 0, 46, tzinfo=TZ))
    assert [item["event"] for item in actions] == ["limit_pool_d1", "limit_reasons_d1"]
    assert guardian.status("2026-10-29", "limit_pool_d1") == "SUCCEEDED"
    assert guardian.status("2026-10-29", "limit_reasons_d1") == "RETRY_WAIT"

    guardian.tick(datetime(2026, 10, 29, 0, 50, tzinfo=TZ))
    assert attempts_path.read_text() == "1"
    guardian.tick(datetime(2026, 10, 29, 0, 51, tzinfo=TZ))
    assert attempts_path.read_text() == "2"
    assert guardian.status("2026-10-29", "limit_reasons_d1") == "SUCCEEDED"

    late = Guardian(events, calendar_dates={"2026-10-29"}, state_root=tmp_path / "late-state",
                    gates={"calendar_ready": lambda *_: True},
                    handlers={"kaipan_fetch": lambda event, *_: event.event_id})
    late_actions = late.tick(datetime(2026, 10, 29, 2, 0, tzinfo=TZ))
    assert [item["event"] for item in late_actions] == ["limit_pool_d1", "limit_reasons_d1"]
    state = json.loads((tmp_path / "late-state/2026-10-29/state.json").read_text())
    assert state["events"]["limit_reasons_d1"]["status"] == "SUCCEEDED"
