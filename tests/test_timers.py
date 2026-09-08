from datetime import datetime, timezone

import pytest

from engine_core import (
    TimerSpec,
    build_a_share_session_plan,
    build_calendar_snapshot,
    due_timer_firings,
    local_datetime_ms,
)


def _plan():
    calendar = build_calendar_snapshot(
        ["2026-09-08"],
        version="timer-fixture-v1",
        declared_valid_from="2026-09-08",
        declared_valid_to="2026-09-08",
        source_guard_valid_from="2026-09-08",
        source_guard_valid_to="2026-09-08",
    )
    return build_a_share_session_plan("2026-09-08", calendar)


def _ms(clock_time: str) -> int:
    return local_datetime_ms("2026-09-08", clock_time)


def test_timer_fires_at_exact_business_anchor_and_only_once_by_identity():
    plan = _plan()
    specs = (TimerSpec("AUCTION_0920", "09:20:00"),)
    firing = due_timer_firings(
        plan,
        specs,
        previous_time_ms=_ms("09:19:57"),
        current_time_ms=_ms("09:20:00"),
    )
    assert len(firing) == 1
    assert firing[0].timer_id == "AUCTION_0920"
    assert firing[0].late_by_ms == 0
    assert firing[0].origin == "NORMAL"
    assert due_timer_firings(
        plan,
        specs,
        previous_time_ms=_ms("09:20:00"),
        current_time_ms=_ms("09:20:03"),
        already_fired=("AUCTION_0920",),
    ) == ()


def test_inclusive_frontier_does_not_lose_unrecorded_exact_timer_after_crash():
    firing = due_timer_firings(
        _plan(),
        (TimerSpec("AUCTION_0920", "09:20:00"),),
        previous_time_ms=_ms("09:20:00"),
        current_time_ms=_ms("09:20:03"),
        origin="RECOVERY_CATCHUP",
    )
    assert len(firing) == 1
    assert firing[0].origin == "RECOVERY_CATCHUP"
    assert firing[0].late_by_ms == 3000


def test_cold_recovery_returns_all_due_timers_in_stable_order():
    specs = (
        TimerSpec("SECOND_AT_0920", "09:20:00"),
        TimerSpec("FIRST_AT_0920", "09:20:00"),
        TimerSpec("AUCTION_0924", "09:24:00"),
        TimerSpec("AUCTION_0925", "09:25:00"),
    )
    firings = due_timer_firings(
        _plan(),
        specs,
        previous_time_ms=None,
        current_time_ms=_ms("09:24:10"),
        origin="RECOVERY_CATCHUP",
    )
    assert tuple(item.timer_id for item in firings) == (
        "FIRST_AT_0920",
        "SECOND_AT_0920",
        "AUCTION_0924",
    )
    assert all(item.fired_time_ms == _ms("09:24:10") for item in firings)


def test_timer_does_not_fire_before_anchor_or_across_wrong_session_date():
    plan = _plan()
    specs = (TimerSpec("AUCTION_0925", "09:25:00"),)
    assert due_timer_firings(
        plan,
        specs,
        previous_time_ms=_ms("09:24:00"),
        current_time_ms=_ms("09:24:59"),
    ) == ()
    wrong_date = int(
        datetime(2026, 9, 7, 1, 25, tzinfo=timezone.utc).timestamp() * 1000
    )
    with pytest.raises(ValueError, match="does not match"):
        due_timer_firings(
            plan,
            specs,
            previous_time_ms=None,
            current_time_ms=wrong_date,
        )


def test_timer_inputs_fail_closed_on_conflicts_or_unknown_fired_identity():
    plan = _plan()
    with pytest.raises(TypeError, match="TimerSpec"):
        due_timer_firings(
            plan,
            (object(),),
            previous_time_ms=None,
            current_time_ms=_ms("09:25:00"),
        )
    with pytest.raises(ValueError, match="unique"):
        due_timer_firings(
            plan,
            (TimerSpec("X", "09:20:00"), TimerSpec("X", "09:24:00")),
            previous_time_ms=None,
            current_time_ms=_ms("09:25:00"),
        )
    with pytest.raises(ValueError, match="unknown timer_id"):
        due_timer_firings(
            plan,
            (TimerSpec("X", "09:20:00"),),
            previous_time_ms=None,
            current_time_ms=_ms("09:25:00"),
            already_fired=("Y",),
        )
    with pytest.raises(ValueError, match="cannot move backwards"):
        due_timer_firings(
            plan,
            (TimerSpec("X", "09:20:00"),),
            previous_time_ms=_ms("09:25:00"),
            current_time_ms=_ms("09:24:00"),
        )


def test_timer_hash_binds_plan_anchor_actual_fire_time_and_origin():
    plan = _plan()
    spec = (TimerSpec("AUCTION_0920", "09:20:00"),)
    normal = due_timer_firings(
        plan,
        spec,
        previous_time_ms=_ms("09:19:59"),
        current_time_ms=_ms("09:20:03"),
    )[0]
    recovery = due_timer_firings(
        plan,
        spec,
        previous_time_ms=_ms("09:19:59"),
        current_time_ms=_ms("09:20:03"),
        origin="RECOVERY_CATCHUP",
    )[0]
    assert normal.content_hash != recovery.content_hash
    assert normal.scheduled_time_ms == _ms("09:20:00")
    assert normal.fired_time_ms == _ms("09:20:03")
