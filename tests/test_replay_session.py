from datetime import datetime, timezone

import pytest

from engine_core import (
    CrossSectionReplaySource,
    ReplaySessionTimeline,
    TimerFiring,
    VirtualClock,
    local_datetime_ms,
)


TRADE_DATE = "2026-09-18"


def _row(ts: int, symbol: str = "600519") -> dict[str, object]:
    return {
        "ts": ts,
        "symbol": symbol,
        "px_milli": 100_000,
        "pc_milli": 99_000,
        "amt_yuan": 10,
        "vol_units": 1,
    }


def _timeline():
    start = local_datetime_ms(TRADE_DATE, "09:15:00")
    source = CrossSectionReplaySource(
        TRADE_DATE,
        ("600519", "000001"),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 9_000,
    )
    frames = source.frames([_row(start + 100)])
    return source, frames, ReplaySessionTimeline(source.manifest(frames))


def test_replay_session_records_empty_frames_and_final_checkpoint():
    source, frames, timeline = _timeline()
    for frame in frames:
        timeline.record_frame(frame)
    checkpoint = timeline.finalize()

    assert timeline.frame_count == 3
    assert checkpoint.node_id == "CHECKPOINT:0940"
    assert timeline.snapshot()["checkpoints"] == ("CHECKPOINT:0940",)
    assert timeline.snapshot()["node_ids"][:3] == ("FRAME:0000", "FRAME:0001", "FRAME:0002")
    assert frames[1].completeness == "EMPTY"
    assert source.frame_count == 3


def test_replay_session_requires_sequential_frames_including_empty_frames():
    _, frames, timeline = _timeline()
    timeline.record_frame(frames[0])
    with pytest.raises(ValueError, match="sequentially"):
        timeline.record_frame(frames[2])


def test_replay_session_keeps_optional_prior_anchors_unknown():
    _, frames, timeline = _timeline()
    for frame in frames:
        timeline.record_frame(frame)
    t0925 = local_datetime_ms(TRADE_DATE, "09:25:06")
    revision = timeline.observe_auction(
        "0925",
        [_row(t0925)],
        evaluation_time_ms=t0925,
        expected_symbols=("600519", "000001"),
        source_layers=("REDIS",),
    )

    assert revision.state == "PARTIAL"
    snapshot = timeline.snapshot()
    assert snapshot["anchors"]["0925"]["revision"] == 1
    assert timeline.auction_timeline.build_analysis_bundle("0925")["prior_deltas"] == {
        "0920": "UNKNOWN",
        "0924": "UNKNOWN",
    }


def test_replay_session_joins_timer_nodes_without_scheduling_them():
    _, frames, left = _timeline()
    _, _, right = _timeline()
    for frame in frames:
        left.record_frame(frame)
        right.record_frame(frame)
    scheduled = local_datetime_ms(TRADE_DATE, "09:32:00")
    firing = TimerFiring(
        timer_id="OPENING_0932",
        scheduled_time_ms=scheduled,
        fired_time_ms=scheduled + 10_000,
        trigger_basis="WALL_DEADLINE",
        origin="NORMAL",
        late_by_ms=10_000,
        session_plan_hash="session-plan-hash",
    )
    left.record_timer(firing)
    left.finalize()
    right.finalize()
    right.record_timer(firing)

    assert left.snapshot()["timers"] == ("TIMER:OPENING_0932",)
    assert left.content_hash == right.content_hash
    assert left.evidence_hash == right.evidence_hash
