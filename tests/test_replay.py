import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine_core import (
    DeterministicEngine,
    EngineSignal,
    MarketStateReducer,
    ProbeStrategy,
    Q2FrameReplaySource,
    SignalKind,
    TDEventTimeReplaySource,
    VirtualClock,
    WindowManager,
    WindowSpec,
    build_q2_projection,
    replay_q2frames,
    replay_td_event_time,
)
from engine_core.replay import Q2FrameV1, TDEventV1
from engine_core.windows import local_time_ms


FIXTURE = Path(__file__).parent / "fixtures/replay/q2frame_600519_20260903.jsonl"


def _frames():
    return [
        json.loads(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _source():
    clock = VirtualClock(
        datetime.fromtimestamp(1788398108000 / 1000, timezone.utc)
    )
    return Q2FrameReplaySource("2026-09-03", ("600519",), clock), clock


def test_q2frame_replay_is_virtual_clock_driven_and_repeatable():
    source, clock = _source()
    projections = [source.apply(frame) for frame in _frames()]
    repeat_source, _ = _source()
    repeat_projections = [repeat_source.apply(frame) for frame in _frames()]
    assert [projection.content_hash for projection in projections] == [
        projection.content_hash for projection in repeat_projections
    ]
    assert source.last_seq_no == 3
    assert source.last_logical_ts_ms == 1788398650000
    assert clock.now_ns() == (1788398650000 - 1788398108000) * 1_000_000
    assert all(projection.status.value == "READY" for projection in projections)


def test_q2frame_signal_construction_is_side_effect_free_until_consumption():
    source, clock = _source()
    initial = clock.now_utc()
    signal = source.signal_for(_frames()[0])

    assert signal.signal_id == "q2frame:1"
    assert signal.logical_time_ms == _frames()[0]["logical_ts_ms"]
    assert signal.signal_kind is SignalKind.MARKET_UPDATE
    assert clock.now_utc() == initial

    source.advance_before_consume(signal)
    assert clock.now_utc() == datetime.fromtimestamp(
        signal.logical_time_ms / 1000.0,
        timezone.utc,
    )


def test_q2frame_replay_feeds_the_same_engine_queue():
    source, _ = _source()
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ProbeStrategy(),
        session_id="2026-09-03",
        phase="REPLAY",
    )
    replay_q2frames(_frames(), source, engine)
    result = engine.run_until_empty()
    assert result.processed_signals == 3
    assert len(result.snapshots) == 0
    assert len(result.strategy_results) == 0
    assert engine._reducer.state.revision == 3
    assert engine._reducer.state.symbol_states["600519"]["price_milli"] == 1297540


def test_q2frame_replay_engine_output_matches_fixture_projection_engine():
    frames = _frames()

    def run_replay():
        source, _ = _source()
        engine = DeterministicEngine(
            MarketStateReducer(),
            WindowManager((WindowSpec("wide", 0, 10**15),)),
            ProbeStrategy(),
            session_id="2026-09-03",
            phase="REPLAY",
        )
        replay_q2frames(frames, source, engine)
        # A pulse is the same Engine trigger used by a fixture-driven run.
        engine.submit(
            EngineSignal(
                "replay-pulse",
                1788398650001,
                4,
                SignalKind.PULSE,
                {"trigger_id": "REPLAY_END"},
            )
        )
        return engine.run_until_empty()

    def run_fixture():
        engine = DeterministicEngine(
            MarketStateReducer(),
            WindowManager((WindowSpec("wide", 0, 10**15),)),
            ProbeStrategy(),
            session_id="2026-09-03",
            phase="REPLAY",
        )
        raw_hashes = {}
        for frame in frames:
            for update in frame["q2_updates"]:
                symbol = update["symbol"]
                raw_hashes.setdefault(symbol, {}).update(
                    {key: value for key, value in update.items() if key != "symbol"}
                )
            projection = build_q2_projection(
                "2026-09-03",
                datetime.fromtimestamp(frame["logical_ts_ms"] / 1000, timezone.utc),
                ("600519",),
                raw_hashes,
                source_id="q2frame_replay",
            )
            engine.submit(EngineSignal(
                "q2frame:%d" % frame["seq_no"],
                frame["logical_ts_ms"],
                frame["seq_no"],
                SignalKind.MARKET_UPDATE,
                projection,
            ))
        engine.submit(EngineSignal(
            "replay-pulse",
            1788398650001,
            4,
            SignalKind.PULSE,
            {"trigger_id": "REPLAY_END"},
        ))
        return engine.run_until_empty()

    left = run_replay()
    right = run_fixture()
    assert [item.content_hash for item in left.strategy_results] == [
        item.content_hash for item in right.strategy_results
    ]
    assert left.snapshots[-1].content_hash == right.snapshots[-1].content_hash


def test_q2frame_replay_rejects_bad_version_sequence_and_time():
    source, _ = _source()
    with pytest.raises(ValueError, match="unsupported"):
        source.apply({"version": "wrong", "seq_no": 1, "logical_ts_ms": 1, "q2_updates": []})
    source.apply(_frames()[0])
    with pytest.raises(ValueError, match="not continuous"):
        source.apply({**_frames()[2], "seq_no": 3})
    with pytest.raises(ValueError, match="moved backwards"):
        source.apply({**_frames()[1], "seq_no": 2, "logical_ts_ms": 1788398000000})


def test_q2frame_parser_rejects_qualified_symbol_and_non_list_updates():
    with pytest.raises(ValueError):
        Q2FrameV1.from_mapping({
            "version": "Q2FrameV1",
            "seq_no": 1,
            "logical_ts_ms": 1,
            "q2_updates": {"symbol": "600519"},
        })
    with pytest.raises(ValueError):
        Q2FrameV1.from_mapping({
            "version": "Q2FrameV1",
            "seq_no": 1,
            "logical_ts_ms": 1,
            "q2_updates": [{"symbol": "600519.SH"}],
        })


def _td_rows():
    return [
        {
            "ts": "2026-09-03 09:20:01",
            "px_milli": 1299600,
            "pc_milli": 1297500,
            "amt_yuan": 100,
            "vol_units": 2,
            "symbol": "600519",
        },
        {
            "ts": "2026-09-03 09:20:00",
            "px_milli": 1299500,
            "pc_milli": 1297500,
            "amt_yuan": 50,
            "vol_units": 1,
            "symbol": "600519",
        },
        {
            "ts": "2026-09-03 09:20:03",
            "px_milli": 1299700,
            "pc_milli": 1297500,
            "amt_yuan": 150,
            "vol_units": 3,
            "symbol": "600519",
        },
    ]


def _td_source():
    anchor = local_time_ms("2026-09-03", "09:20:00")
    clock = VirtualClock(
        datetime.fromtimestamp((anchor - 1000) / 1000, timezone.utc)
    )
    return TDEventTimeReplaySource(
        "2026-09-03",
        ("600519",),
        clock,
        slice_anchor_ms=anchor,
    ), clock, anchor


def test_td_event_replay_slices_preserve_events_and_stable_order():
    source, _, anchor = _td_source()
    left = source.event_slices(_td_rows())
    right_source, _, _ = _td_source()
    right = right_source.event_slices(list(reversed(_td_rows())))

    assert [item.content_hash for item in left] == [item.content_hash for item in right]
    assert [(item.start_ms, item.end_exclusive_ms) for item in left] == [
        (anchor, anchor + 3000),
        (anchor + 3000, anchor + 6000),
    ]
    assert [len(item.events) for item in left] == [2, 1]
    assert [event.event_time_ms for event in left[0].events] == [anchor, anchor + 1000]
    assert sum(len(item.events) for item in left) == 3


def test_td_event_replay_tie_break_includes_preserved_raw_fields():
    source, _, anchor = _td_source()
    common = {
        "ts": "2026-09-03 09:20:00",
        "px_milli": 1299500,
        "pc_milli": 1297500,
        "amt_yuan": 50,
        "symbol": "600519",
    }
    left = source.event_slices(
        [
            {**common, "bp1_milli": 1299400},
            {**common, "bp1_milli": 1299300},
        ]
    )
    right_source, _, _ = _td_source()
    right = right_source.event_slices(
        [
            {**common, "bp1_milli": 1299300},
            {**common, "bp1_milli": 1299400},
        ]
    )
    assert [event.content_hash for event in left[0].events] == [
        event.content_hash for event in right[0].events
    ]
    assert [
        event.raw_fields["bp1_milli"] for event in left[0].events
    ] == [
        event.raw_fields["bp1_milli"] for event in right[0].events
    ]
    assert set(event.raw_fields["bp1_milli"] for event in left[0].events) == {
        1299300,
        1299400,
    }
    assert left[0].start_ms == anchor


def test_td_event_public_projection_contract_keeps_units_and_slice_size():
    source, _, anchor = _td_source()
    event = TDEventV1.from_mapping(_td_rows()[0])

    assert source.slice_ms == 3_000
    assert event.to_q2_raw() == {
        "px": event.price_milli,
        "pc": event.pre_close_milli,
        "amt": event.amount_yuan,
        "ts": event.event_time_ms,
    }
    assert "vol" not in event.to_q2_raw()
    assert event.event_time_ms >= anchor


def test_td_event_replay_feeds_same_engine_without_preaggregation():
    source, clock, _ = _td_source()
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ProbeStrategy(),
        session_id="2026-09-03",
        phase="REPLAY",
    )
    replay_td_event_time(_td_rows(), source, engine)
    result = engine.run_until_empty()

    assert result.processed_signals == 3
    assert engine._reducer.state.revision == 3
    assert engine._reducer.state.symbol_states["600519"]["price_milli"] == 1299700
    assert clock.now_ns() == (1000 + 3000) * 1_000_000


def test_td_event_replay_signals_are_repeatable_and_virtual_clock_driven():
    source, clock, _ = _td_source()
    initial = clock.now_utc()
    left = source.signals_for(_td_rows())
    assert clock.now_utc() == initial
    right_source, right_clock, _ = _td_source()
    right = right_source.signals_for(_td_rows())

    assert [signal.signal_id for signal in left] == [signal.signal_id for signal in right]
    assert [signal.logical_time_ms for signal in left] == [
        signal.logical_time_ms for signal in right
    ]
    assert [signal.payload.content_hash for signal in left] == [
        signal.payload.content_hash for signal in right
    ]
    assert clock.now_utc() == right_clock.now_utc()


def test_td_event_replay_rejects_missing_fields_and_pre_anchor_rows():
    source, _, anchor = _td_source()
    with pytest.raises(ValueError, match="px_milli"):
        source.event_slices(
            [
                {
                    "ts": "2026-09-03 09:20:00",
                    "pc_milli": 1297500,
                    "amt_yuan": 50,
                    "symbol": "600519",
                }
            ]
        )
    with pytest.raises(ValueError, match="precedes"):
        source.event_slices(
            [
                {
                    "ts": "2026-09-03 09:19:59",
                    "px_milli": 1299500,
                    "pc_milli": 1297500,
                    "amt_yuan": 50,
                    "symbol": "600519",
                }
            ]
        )
    with pytest.raises(ValueError, match="outside expected"):
        source.event_slices(
            [
                {
                    "ts": "2026-09-03 09:20:00",
                    "px_milli": 1299500,
                    "pc_milli": 1297500,
                    "amt_yuan": 50,
                    "symbol": "000001",
                }
            ]
        )
    with pytest.raises(ValueError, match="event date"):
        source.event_slices(
            [
                {
                    "ts": "2026-09-04 09:20:00",
                    "px_milli": 1299500,
                    "pc_milli": 1297500,
                    "amt_yuan": 50,
                    "symbol": "600519",
                }
            ]
        )
    assert anchor > 0
