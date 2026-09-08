from datetime import datetime, timezone

import pytest

from engine_core import (
    CalendarCoverageError,
    SessionInterval,
    SessionPlan,
    build_a_share_session_plan,
    build_calendar_snapshot,
)


def _calendar():
    return build_calendar_snapshot(
        ["2026-09-04", "2026-09-07", "2026-09-08"],
        version="fixture-v1",
        declared_valid_from="2026-09-04",
        declared_valid_to="2026-09-08",
        source_guard_valid_from="2026-09-04",
        source_guard_valid_to="2026-09-08",
    )


def _utc(hour: int, minute: int, second: int = 0, microsecond: int = 0):
    return datetime(2026, 9, 8, hour, minute, second, microsecond, tzinfo=timezone.utc)


def test_a_share_session_plan_uses_calendar_as_trading_day_authority():
    calendar = _calendar()
    plan = build_a_share_session_plan("2026-09-08", calendar)
    assert plan.trade_date == "2026-09-08"
    assert plan.calendar_semantic_hash == calendar.semantic_hash
    with pytest.raises(ValueError, match="requires a trading day"):
        build_a_share_session_plan("2026-09-06", calendar)


def test_session_plan_does_not_promote_calendar_guard_data_to_declared_support():
    calendar = build_calendar_snapshot(
        ["2026-09-03", "2026-09-04", "2026-09-07"],
        version="guard-v1",
        declared_valid_from="2026-09-04",
        declared_valid_to="2026-09-04",
        source_guard_valid_from="2026-09-03",
        source_guard_valid_to="2026-09-07",
    )
    assert calendar.is_trading_day("2026-09-03") is True
    with pytest.raises(CalendarCoverageError, match="declared coverage"):
        build_a_share_session_plan("2026-09-03", calendar)


def test_a_share_phase_boundaries_are_half_open_in_shanghai_time():
    plan = build_a_share_session_plan("2026-09-08", _calendar())
    # UTC + 8 hours = Asia/Shanghai.
    assert plan.phase_at(_utc(1, 14, 59, 999999)) == "PREMARKET"
    assert plan.phase_at(_utc(1, 15)) == "AUCTION"
    assert plan.phase_at(_utc(1, 30)) == "INTRADAY"
    assert plan.phase_at(_utc(3, 30)) == "LUNCH_BREAK"
    assert plan.phase_at(_utc(5, 0)) == "INTRADAY"
    assert plan.phase_at(_utc(7, 0)) == "POSTMARKET"


def test_session_plan_rejects_naive_or_cross_date_instants():
    plan = build_a_share_session_plan("2026-09-08", _calendar())
    with pytest.raises(ValueError, match="timezone-aware"):
        plan.phase_at(datetime(2026, 9, 8, 9, 20))
    with pytest.raises(ValueError, match="does not match"):
        plan.phase_at(datetime(2026, 9, 7, 1, 20, tzinfo=timezone.utc))


def test_phase_at_ms_matches_aware_datetime_and_rejects_bad_epoch():
    plan = build_a_share_session_plan("2026-09-08", _calendar())
    instant = _utc(1, 20, 3, 456000)
    assert plan.phase_at_ms(int(instant.timestamp() * 1000)) == "AUCTION"
    with pytest.raises(TypeError):
        plan.phase_at_ms(True)
    with pytest.raises(ValueError):
        plan.phase_at_ms(0)


def test_session_intervals_must_be_ordered_contiguous_and_full_day():
    calendar = _calendar()
    with pytest.raises(ValueError, match="without gaps or overlap"):
        SessionPlan(
            trade_date="2026-09-08",
            timezone_name="Asia/Shanghai",
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.version,
            calendar_semantic_hash=calendar.semantic_hash,
            intervals=(
                SessionInterval("PREMARKET", "00:00:00", "09:15:00"),
                SessionInterval("AUCTION", "09:16:00", "24:00:00"),
            ),
        )


def test_session_plan_hash_is_stable_and_binds_calendar_identity():
    calendar = _calendar()
    left = build_a_share_session_plan("2026-09-08", calendar)
    right = build_a_share_session_plan("2026-09-08", calendar)
    assert left.content_hash == right.content_hash

    other = build_calendar_snapshot(
        calendar.trading_dates,
        version="fixture-v2",
        declared_valid_from=calendar.declared_valid_from,
        declared_valid_to=calendar.declared_valid_to,
        source_guard_valid_from=calendar.source_guard_valid_from,
        source_guard_valid_to=calendar.source_guard_valid_to,
    )
    assert build_a_share_session_plan("2026-09-08", other).content_hash != left.content_hash


def test_clock_parser_rejects_loose_or_invalid_boundaries():
    with pytest.raises(ValueError, match="strict HH:MM:SS"):
        SessionInterval("AUCTION", "9:15:00", "09:30:00")
    with pytest.raises(ValueError, match="outside one day"):
        SessionInterval("AUCTION", "09:15:00", "25:00:00")
    with pytest.raises(ValueError, match="non-empty"):
        SessionInterval("AUCTION", "09:15:00", "09:15:00")
