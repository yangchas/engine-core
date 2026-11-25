from datetime import datetime, timezone

from engine_core import (
    CrossSectionReplaySource,
    CrossSectionStateV1,
    IncrementalCrossSectionState,
    IncrementalQ2Projection,
    DeterministicEngine,
    MarketStateReducer,
    ProbeStrategy,
    TDEventV1,
    VirtualClock,
    WindowManager,
    WindowSpec,
    build_cross_section_facts,
    deep_freeze,
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
    assert source.frame_from_events(1, [TDEventV1.from_mapping(rows[0])], presorted=True).content_hash == one.content_hash


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


def test_incremental_state_uses_canonical_order_for_multiple_events_per_symbol():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519",),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 3_000,
    )
    rows = [_row(start + 100, price=100_000), _row(start + 200, price=101_000)]
    left_frame = source.frame_from_events(0, rows)
    right_frame = source.frame_from_events(0, list(reversed(rows)))
    left = IncrementalCrossSectionState(source.expected_symbols, trade_date="2026-09-18")
    right = IncrementalCrossSectionState(source.expected_symbols, trade_date="2026-09-18")
    left.apply(left_frame.events)
    right.apply(right_frame.events)
    kwargs = {
        "frame_no": 0,
        "logical_ts_ms": left_frame.logical_ts_ms,
        "updated_symbols": left_frame.updated_symbols,
        "missing_symbols": left_frame.missing_symbols,
        "completeness": left_frame.completeness,
        "coverage": left_frame.coverage,
    }
    assert left.identity_hash(**kwargs) == right.identity_hash(**kwargs)
    assert left.latest_raw["600519"]["px"] == 101_000


def test_incremental_state_final_full_hash_matches_public_state_hash():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519", "000001"),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 3_000,
    )
    frame = source.frame_from_events(0, [_row(start + 100, "600519")])
    accumulator = IncrementalCrossSectionState(source.expected_symbols, trade_date="2026-09-18")
    accumulator.apply(frame.events)
    signal = source.signal_for_frame(frame, accumulator.latest_raw)
    assert accumulator.full_state_hash(
        frame_no=frame.frame_no,
        logical_ts_ms=frame.logical_ts_ms,
        updated_symbols=frame.updated_symbols,
        missing_symbols=frame.missing_symbols,
        completeness=frame.completeness,
        coverage=frame.coverage,
        source_time_min_ms=frame.source_time_min_ms,
        source_time_max_ms=frame.source_time_max_ms,
    ) == signal.payload.cross_section.content_hash


def test_cross_section_projection_is_not_rebuilt_by_deep_freeze():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519",),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 3_000,
    )
    frame = source.frame_from_events(0, [_row(start + 100)])
    signal = source.signal_for_frame(
        frame,
        {},
        projection_builder=IncrementalQ2Projection("2026-09-18", source.expected_symbols),
    )
    assert deep_freeze(signal.payload) is signal.payload


def test_cross_section_facts_can_label_a_source_cohort_without_claiming_full_market():
    state = CrossSectionStateV1(
        trade_date="2026-09-29",
        frame_no=7,
        logical_ts_ms=1_790_650_000_000,
        expected_symbols=("000001", "000002", "000003"),
        updated_symbols=("000001", "000002"),
        missing_symbols=("000003",),
        symbol_states={
            "000001": {"price_milli": 10_100, "pre_close_milli": 10_000},
            "000002": {"price_milli": 9_900, "pre_close_milli": None},
        },
        frame_completeness="PARTIAL",
        coverage=2 / 3,
        source_time_min_ms=1_790_649_997_000,
        source_time_max_ms=1_790_650_000_000,
    )

    facts = build_cross_section_facts(
        state,
        scope="OBSERVED_COHORT",
        source_layers=("t1_v2_q2frame_event_time_replay",),
    )

    assert facts.scope == "OBSERVED_COHORT"
    assert facts.expected_count == 3
    assert facts.observed_count == 2
    assert facts.missing_count == 1
    assert facts.coverage == 2 / 3
    assert facts.field_denominators == {
        "price_milli": 2,
        "pre_close_milli": 1,
    }
    assert facts.market_breadth == {
        "up_count": 1,
        "down_count": 0,
        "flat_count": 0,
        "unknown_count": 1,
    }
    assert facts.fact_only is True


def test_out_of_scope_tick_is_retained_without_aborting_or_inflating_cohort():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519",),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 3_000,
    )
    frame = source.frame_from_events(
        0,
        [
            _row(start + 100, "600519", 100_000),
            _row(start + 200, "000001", 101_000),
        ],
    )

    assert {event.symbol for event in frame.events} == {"600519", "000001"}
    assert frame.updated_symbols == ("600519",)
    assert frame.missing_symbols == ()
    assert frame.out_of_scope_symbols == ("000001",)
    assert frame.coverage == 1.0
    assert frame.completeness == "PARTIAL"

    signal = source.signal_for_frame(
        frame,
        {},
        projection_builder=IncrementalQ2Projection("2026-09-18", source.expected_symbols),
    )
    projection = signal.payload
    assert projection.out_of_scope_symbols == ("000001",)
    assert set(projection.quotes) == {"600519"}
    assert projection.status.value == "PARTIAL"
    assert projection.cross_section.symbol_states["000001"]["px"] == 101_000

    facts = build_cross_section_facts(projection.cross_section)
    assert facts.expected_count == 1
    assert facts.observed_count == 1
    assert facts.out_of_scope_symbols == ("000001",)

    manifest = source.manifest((frame,))
    assert manifest.event_count == 2
    assert manifest.expected_symbol_count == 1
    assert manifest.out_of_scope_symbols == ("000001",)


def test_incremental_state_retains_out_of_scope_ticks_and_matches_full_hash():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519",),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 3_000,
    )
    frame = source.frame_from_events(
        0,
        [
            _row(start + 100, "600519", 100_000),
            _row(start + 200, "000001", 101_000),
        ],
    )
    left = IncrementalCrossSectionState(source.expected_symbols, trade_date="2026-09-18")
    right = IncrementalCrossSectionState(source.expected_symbols, trade_date="2026-09-18")
    left.apply(frame.events)
    right.apply(tuple(reversed(frame.events)))

    identity_args = {
        "frame_no": frame.frame_no,
        "logical_ts_ms": frame.logical_ts_ms,
        "updated_symbols": frame.updated_symbols,
        "missing_symbols": frame.missing_symbols,
        "completeness": frame.completeness,
        "coverage": frame.coverage,
        "out_of_scope_symbols": frame.out_of_scope_symbols,
    }
    assert left.latest_raw == right.latest_raw
    assert left.identity_hash(**identity_args) == right.identity_hash(**identity_args)

    signal = source.signal_for_frame(frame, left.latest_raw, latest_raw_already_updated=True)
    assert left.full_state_hash(
        frame_no=frame.frame_no,
        logical_ts_ms=frame.logical_ts_ms,
        updated_symbols=frame.updated_symbols,
        missing_symbols=frame.missing_symbols,
        completeness=frame.completeness,
        coverage=frame.coverage,
        out_of_scope_symbols=frame.out_of_scope_symbols,
        source_time_min_ms=frame.source_time_min_ms,
        source_time_max_ms=frame.source_time_max_ms,
    ) == signal.payload.cross_section.content_hash


def test_out_of_scope_symbol_remains_reported_after_its_frame():
    start = local_datetime_ms("2026-09-18", "09:15:00")
    source = CrossSectionReplaySource(
        "2026-09-18",
        ("600519",),
        VirtualClock(datetime.fromtimestamp(start / 1000, timezone.utc)),
        slice_anchor_ms=start,
        end_exclusive_ms=start + 6_000,
    )
    frames = source.frames([_row(start + 200, "000001", 101_000)])
    signals = list(source.iter_signals(frames))

    assert frames[0].out_of_scope_symbols == ("000001",)
    assert frames[1].out_of_scope_symbols == ()
    assert signals[1].payload.out_of_scope_symbols == ("000001",)
    assert signals[1].payload.cross_section.symbol_states["000001"]["px"] == 101_000
