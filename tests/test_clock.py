from datetime import datetime, timedelta, timezone

import pytest

from engine_core.clock import VirtualClock, require_aware_utc


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
