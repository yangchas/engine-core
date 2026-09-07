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
    assert view.revision == 2
    assert view.finality == "FINAL"
    assert view.origin == "NORMAL"


def test_window_cannot_close_before_end():
    start = local_time_ms("2026-09-04", "09:15:00")
    end = local_time_ms("2026-09-04", "09:20:00")
    manager = WindowManager((WindowSpec("auction", start, end),))
    with pytest.raises(ValueError):
        manager.close("auction", end - 1)


def test_empty_window_is_missing_and_not_ready():
    start = local_time_ms("2026-09-04", "09:20:00")
    end = local_time_ms("2026-09-04", "09:24:00")
    manager = WindowManager((WindowSpec("auction", start, end),))
    view = manager.close("auction", end)
    assert view.observation_count == 0
    assert view.coverage == 0.0
    assert view.completeness == "MISSING"
    assert view.finality == "FINAL"
    assert view.revision == 1


def test_window_recovery_origin_is_separate_from_finality():
    start = local_time_ms("2026-09-04", "09:20:00")
    end = local_time_ms("2026-09-04", "09:24:00")
    manager = WindowManager((WindowSpec("auction", start, end),))
    manager.observe(start, start, 0.8, "PARTIAL", "a")
    view = manager.close("auction", end, origin="RECOVERY_CATCHUP")
    assert view.finality == "FINAL"
    assert view.origin == "RECOVERY_CATCHUP"
    assert view.completeness == "PARTIAL"


def test_window_observation_uses_first_coverage_and_minimum_afterwards():
    start = local_time_ms("2026-09-04", "09:20:00")
    end = local_time_ms("2026-09-04", "09:24:00")
    manager = WindowManager((WindowSpec("auction", start, end),))
    manager.observe(start, start, 0.9, "READY", "a")
    manager.observe(start + 1, start + 1, 1.0, "READY", "b")
    assert manager.views()["auction"].coverage == 0.9


def test_window_source_range_uses_true_minimum_and_maximum_for_out_of_order_observations():
    start = local_time_ms("2026-09-04", "09:20:00")
    end = local_time_ms("2026-09-04", "09:24:00")
    manager = WindowManager((WindowSpec("auction", start, end),))
    manager.observe(start, 300, 1.0, "READY", "a", oldest_source_time_ms=300, newest_source_time_ms=310)
    manager.observe(start + 1, 100, 1.0, "READY", "b", oldest_source_time_ms=100, newest_source_time_ms=120)
    view = manager.views()["auction"]
    assert view.oldest_source_time_ms == 100
    assert view.newest_source_time_ms == 310
    assert view.first_source_time_ms == 100
    assert view.last_source_time_ms == 310
