from datetime import datetime, timedelta, timezone

import pytest

from engine_core.clock import (
    MILLISECONDS_PER_DAY,
    VirtualClock,
    local_datetime_ms,
    parse_clock_time_ms,
    require_aware_utc,
)


def test_virtual_clock_advances_wall_and_monotonic_together():
    clock = VirtualClock(datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc))
    assert clock.now_ns() == 0
    clock.advance_by(timedelta(seconds=3))
    assert clock.now_utc() == datetime(2026, 9, 4, 1, 0, 3, tzinfo=timezone.utc)
    assert clock.now_ns() == 3_000_000_000


def test_virtual_clock_rejects_backward_time_and_naive_datetime():
    clock = VirtualClock(datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        clock.advance_by(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        clock.advance_to(datetime(2026, 9, 4, 0, 59))
    with pytest.raises(ValueError):
        require_aware_utc(datetime(2026, 9, 4, 1, 0))


def test_strict_clock_parser_has_one_explicit_day_end_exception():
    assert parse_clock_time_ms("09:20:03") == ((9 * 60 + 20) * 60 + 3) * 1000
    assert parse_clock_time_ms("24:00:00", allow_day_end=True) == MILLISECONDS_PER_DAY
    with pytest.raises(ValueError, match="outside one day"):
        parse_clock_time_ms("24:00:00")
    with pytest.raises(ValueError, match="strict HH:MM:SS"):
        parse_clock_time_ms("9:20:03")


def test_local_datetime_ms_uses_explicit_timezone_and_strict_date():
    expected = datetime(2026, 9, 4, 1, 20, 3, tzinfo=timezone.utc)
    assert local_datetime_ms("2026-09-04", "09:20:03") == int(
        expected.timestamp() * 1000
    )
    with pytest.raises(ValueError, match="strict valid YYYY-MM-DD"):
        local_datetime_ms("2026-9-4", "09:20:03")
    with pytest.raises(ValueError, match="unknown local timezone"):
        local_datetime_ms("2026-09-04", "09:20:03", timezone_name="Mars/Base")
