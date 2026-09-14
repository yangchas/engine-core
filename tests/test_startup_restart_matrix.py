"""Pure startup/restart matrix for the Core-owned consumption nodes.

The source freeze nodes (09:20/09:24/09:25) belong to t1-v2 and are not
represented here.  This matrix only proves the existing SessionPlan and
SessionTimer wheels can calculate the Core-owned 09:26 auction follow-up and
09:32 opening checkpoint without consulting a wall clock or performing I/O.
"""

from datetime import datetime, timezone

import pytest

from engine_core import (
    CalendarCoverageError,
    TimerSpec,
    build_a_share_session_plan,
    build_calendar_snapshot,
    due_timer_firings,
    local_datetime_ms,
)


TRADE_DATE = "2026-09-08"
TIMER_CONTRACT = "SessionTimerV1"


def _calendar():
    return build_calendar_snapshot(
        [TRADE_DATE],
        version="startup-matrix-v1",
        declared_valid_from=TRADE_DATE,
        declared_valid_to=TRADE_DATE,
        source_guard_valid_from=TRADE_DATE,
        source_guard_valid_to=TRADE_DATE,
    )


def _plan():
    return build_a_share_session_plan(TRADE_DATE, _calendar())


def _ms(clock_time: str) -> int:
    return local_datetime_ms(TRADE_DATE, clock_time)


def _specs():
    # These are Core consumption checkpoints.  t1-v2 source freeze gates at
    # 09:20/09:24/09:25 are intentionally not listed.
    return (
        TimerSpec("AUCTION_0926", "09:26:00"),
        TimerSpec("OPENING_0932", "09:32:00"),
    )


def _node_key(timer_id: str) -> str:
    """The caller-side stable identity contract, not a new runtime object."""

    return f"{TRADE_DATE}:{timer_id}:{TIMER_CONTRACT}"


def _timer_ids(completed_node_keys):
    """Project known session keys to the existing timer wheel's identities."""

    expected = {
        _node_key(spec.timer_id): spec.timer_id
        for spec in _specs()
    }
    keys = tuple(completed_node_keys)
    if any(key not in expected for key in keys):
        raise ValueError("completed node key is outside this session")
    if len(keys) != len(set(keys)):
        raise ValueError("completed node keys must be unique")
    return tuple(expected[key] for key in keys)


def test_before_core_nodes_nothing_is_due_at_0915():
    firings = due_timer_firings(
        _plan(),
        _specs(),
        previous_time_ms=None,
        current_time_ms=_ms("09:15:00"),
    )
    assert firings == ()


def test_normal_0926_fires_only_auction_followup():
    firings = due_timer_firings(
        _plan(),
        _specs(),
        previous_time_ms=_ms("09:15:00"),
        current_time_ms=_ms("09:26:00"),
    )
    assert tuple(item.timer_id for item in firings) == ("AUCTION_0926",)
    assert firings[0].origin == "NORMAL"
    assert firings[0].late_by_ms == 0
    assert _node_key(firings[0].timer_id) == "2026-09-08:AUCTION_0926:SessionTimerV1"


def test_late_start_at_0928_is_recovery_catchup_and_completed_node_is_not_repeated():
    specs = _specs()
    recovery = due_timer_firings(
        _plan(),
        specs,
        previous_time_ms=None,
        current_time_ms=_ms("09:28:00"),
        origin="RECOVERY_CATCHUP",
    )
    assert tuple(item.timer_id for item in recovery) == ("AUCTION_0926",)
    assert recovery[0].late_by_ms == 120_000
    assert recovery[0].origin == "RECOVERY_CATCHUP"

    completed = _timer_ids((_node_key("AUCTION_0926"),))
    assert due_timer_firings(
        _plan(),
        specs,
        previous_time_ms=None,
        current_time_ms=_ms("09:28:00"),
        already_fired=completed,
        origin="RECOVERY_CATCHUP",
    ) == ()


def test_0933_cold_recovery_returns_core_nodes_in_stable_schedule_order():
    firings = due_timer_firings(
        _plan(),
        _specs(),
        previous_time_ms=None,
        current_time_ms=_ms("09:33:00"),
        origin="RECOVERY_CATCHUP",
    )
    assert tuple(item.timer_id for item in firings) == (
        "AUCTION_0926",
        "OPENING_0932",
    )
    assert tuple(item.late_by_ms for item in firings) == (7 * 60_000, 60_000)
    assert all(item.origin == "RECOVERY_CATCHUP" for item in firings)


def test_completed_node_keys_are_session_scoped_and_unknown_keys_fail_closed():
    with pytest.raises(ValueError, match="outside this session"):
        _timer_ids(("2026-09-09:AUCTION_0926:SessionTimerV1",))

    with pytest.raises(ValueError, match="unique"):
        _timer_ids(
            (
                _node_key("AUCTION_0926"),
                _node_key("AUCTION_0926"),
            )
        )

    with pytest.raises(ValueError, match="unknown timer_id"):
        due_timer_firings(
            _plan(),
            _specs(),
            previous_time_ms=None,
            current_time_ms=_ms("09:33:00"),
            already_fired=("UNKNOWN_NODE",),
            origin="RECOVERY_CATCHUP",
        )


def test_non_trading_or_cross_date_startup_fails_closed():
    calendar = build_calendar_snapshot(
        [TRADE_DATE],
        version="startup-matrix-v1",
        declared_valid_from="2026-09-07",
        declared_valid_to="2026-09-09",
        source_guard_valid_from="2026-09-07",
        source_guard_valid_to="2026-09-09",
    )
    with pytest.raises(ValueError, match="requires a trading day"):
        build_a_share_session_plan("2026-09-09", calendar)

    wrong_date = int(
        datetime(2026, 9, 9, 1, 33, tzinfo=timezone.utc).timestamp() * 1000
    )
    with pytest.raises(ValueError, match="does not match"):
        due_timer_firings(
            _plan(),
            _specs(),
            previous_time_ms=None,
            current_time_ms=wrong_date,
            origin="RECOVERY_CATCHUP",
        )


def test_same_time_core_nodes_are_stably_ordered_by_timer_id():
    specs = (
        TimerSpec("OPENING_B", "09:32:00"),
        TimerSpec("OPENING_A", "09:32:00"),
    )
    firings = due_timer_firings(
        _plan(),
        specs,
        previous_time_ms=None,
        current_time_ms=_ms("09:32:00"),
        origin="RECOVERY_CATCHUP",
    )
    assert tuple(item.timer_id for item in firings) == ("OPENING_A", "OPENING_B")
