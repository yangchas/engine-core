from datetime import datetime, timezone

from engine_core import CrossSectionReplaySource, VirtualClock, local_datetime_ms
from examples.run_real_cross_sectional_replay import _performance_status, _run_pass


def test_performance_status_preserves_five_and_ten_minute_gates():
    assert _performance_status(299_999, frame_count=500) == "CROSS_SECTION_REPLAY_READY"
    assert _performance_status(300_000, frame_count=500) == "CROSS_SECTION_REPLAY_READY"
    assert _performance_status(300_001, frame_count=500) == "CROSS_SECTION_REPLAY_PASS_WITH_WARN"
    assert _performance_status(600_000, frame_count=500) == "CROSS_SECTION_REPLAY_PASS_WITH_WARN"
    assert _performance_status(600_001, frame_count=500) == "CROSS_SECTION_REPLAY_BLOCKED_BY_PERFORMANCE"
    assert _performance_status(999_999, frame_count=20) == "CROSS_SECTION_REPLAY_PROFILED"


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.done = False

    def execute(self, sql):
        assert "SELECT" in sql

    def fetchmany(self, size):
        if self.done:
            return []
        self.done = True
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


def test_real_runner_streams_rows_into_global_frames_without_full_capture():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519",),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 6_000,
    )
    rows = [
        ("2026-09-18 09:15:00.100", 100_000, 99_000, 10, 1, "600519"),
        ("2026-09-18 09:15:03.100", 101_000, 99_000, 11, 1, "600519"),
    ]
    result = _run_pass(_Connection(rows), source, ("600519",), shuffled=False)
    assert result["frame_count"] == 2
    assert result["processed_signals"] == 2
    assert result["reducer_revision"] == 2
    assert result["total_events"] == 2
    assert result["final_virtual_clock"].endswith("01:15:06+00:00")
    assert result["state_hash_verification"] == "FULL_PER_FRAME"
    assert result["final_cross_section_full_hash_parity"] == "PASS"

    frame_source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519",),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 6_000,
    )
    frame_result = _run_pass(
        _Connection(rows), frame_source, ("600519",), shuffled=False, verification_level="FRAME"
    )
    assert frame_result["state_hash_verification"] == "INCREMENTAL_IDENTITY_ONLY"
    assert frame_result["final_cross_section_full_hash_parity"] == "NOT_RUN"
