from datetime import datetime, timezone

from engine_core import (
    CrossSectionReplaySource,
    IncrementalCrossSectionState,
    DeterministicEngine,
    MarketStateReducer,
    ProbeStrategy,
    TDEventV1,
    VirtualClock,
    WindowManager,
    WindowSpec,
    local_datetime_ms,
)


def _row(ts, symbol="600519", price=100_000):
    return {
        "ts": ts,
        "symbol": symbol,
        "px_milli": price,
        "pc_milli": 99_000,
        "amt_yuan": 10,
        "vol_units": 1,
    }


def test_cross_section_source_keeps_empty_frames_and_500_boundaries():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    end = local_datetime_ms("2026-09-18", "09:40:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519", "000001"),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=end,
    )
    frames = source.frames([_row(start + 1000)])
    assert len(frames) == 500
    assert frames[0].logical_ts_ms == start + 3_000
    assert frames[0].completeness == "PARTIAL"
    assert frames[1].completeness == "EMPTY"
    assert frames[-1].end_exclusive_ms == end
    manifest = source.manifest(frames)
    assert manifest.frame_count == 500
    assert manifest.event_count == 1


def test_cross_section_order_shuffle_has_same_frame_and_signal_hashes():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519", "000001"),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 9_000,
    )
    rows = [_row(start + 100, "600519"), _row(start + 100, "000001", 101_000)]
    left = source.signals_for(rows)
    right_source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519", "000001"),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 9_000,
    )
    right = right_source.signals_for(list(reversed(rows)))
    assert [item.payload.cross_section.content_hash for item in left] == [
        item.payload.cross_section.content_hash for item in right
    ]
    assert len(left) == 3


def test_cross_section_replay_uses_one_market_update_per_frame_on_one_engine():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519",),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 9_000,
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ProbeStrategy(),
        session_id="cross-section",
        phase="REPLAY",
    )
    source.replay([_row(start + 100)], engine)
    assert engine._processed == 3
    assert engine._reducer.state.revision == 3
    assert engine._reducer.state.source_observation_metadata["frame_no"] == 2
    assert engine._reducer.state.completeness == "EMPTY"


def test_streaming_single_frame_builder_matches_bounded_frame():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519", "000001"),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 6_000,
    )
    rows = [_row(start + 3_100, "600519")]
    whole = source.frames(rows)
    one = source.frame_from_events(1, [TDEventV1.from_mapping(rows[0])])
    assert whole[1].content_hash == one.content_hash


def test_incremental_state_identity_is_stable_for_reordered_frame_events():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519", "000001"),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 3_000,
    )
    rows = [_row(start + 100, "600519"), _row(start + 200, "000001", 101_000)]
    frame = source.frame_from_events(0, rows)
    left = IncrementalCrossSectionState(source.expected_symbols, trade_date="2026-09-18")
    right = IncrementalCrossSectionState(source.expected_symbols, trade_date="2026-09-18")
    left.apply(frame.events)
    right.apply(tuple(reversed(frame.events)))
    kwargs = {
        "frame_no": frame.frame_no,
        "logical_ts_ms": frame.logical_ts_ms,
        "updated_symbols": frame.updated_symbols,
        "missing_symbols": frame.missing_symbols,
        "completeness": frame.completeness,
        "coverage": frame.coverage,
    }
    assert left.identity_hash(**kwargs) == right.identity_hash(**kwargs)
