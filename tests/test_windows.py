import pytest

from engine_core.windows import WindowManager, WindowSpec, local_time_ms


def test_half_open_window_has_no_double_count_at_end():
    start = local_time_ms("2026-09-04", "09:15:00")
    end = local_time_ms("2026-09-04", "09:20:00")
    manager = WindowManager((WindowSpec("auction", start, end),))
    assert manager.observe(start, start, 1.0, "READY", "a") == ("auction",)
    assert manager.observe(end, end, 1.0, "READY", "b") == ()
    view = manager.close("auction", end)
    assert view.observation_count == 1
    assert view.revision == 1


def test_window_cannot_close_before_end():
    start = local_time_ms("2026-09-04", "09:15:00")
    end = local_time_ms("2026-09-04", "09:20:00")
    manager = WindowManager((WindowSpec("auction", start, end),))
    with pytest.raises(ValueError):
        manager.close("auction", end - 1)
