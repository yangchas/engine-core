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
REAL_Q2_UPDATE_FIXTURE = Path(__file__).parent / "fixtures/q2/q2frame_source_time_real_20260929.json"


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


def test_q2frame_isolates_bad_update_and_keeps_real_sibling_quote_running(
    tmp_path: Path,
):
    """A malformed member must degrade this projection, not discard its frame.

    The valid update is an unchanged, hash-pinned producer capture. The two
    invalid members are explicit fault injection, not claimed market data.
    """
    captured = json.loads(REAL_Q2_UPDATE_FIXTURE.read_text(encoding="utf-8"))
    update = captured["q2_update"]
    frame_time_ms = captured["source"]["frame_logical_ts_ms"]
    frame = {
        "version": "Q2FrameV1",
        "seq_no": 1,
        "logical_ts_ms": frame_time_ms,
        "q2_updates": [
            update,
            {"symbol": "not-a-market-symbol", "px": 1, "ts": frame_time_ms},
            None,
        ],
    }
    clock = VirtualClock(datetime.fromtimestamp(frame_time_ms / 1000, timezone.utc))
    reducer = MarketStateReducer()
    source = Q2FrameReplaySource(
        captured["trade_date"],
        (update["symbol"],),
        clock,
    )
    engine = DeterministicEngine(
        reducer,
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ProbeStrategy(),
        session_id=captured["trade_date"],
        phase="REPLAY",
    )

    signal = source.signal_for(frame)
    engine.submit(signal)
    result = engine.run_until_empty()

    assert result.processed_signals == 1
    assert source.last_seq_no == 1
    assert reducer.state.revision == 1
    assert reducer.state.symbol_states[update["symbol"]]["price_milli"] == update["px"]
    assert reducer.state.completeness == "PARTIAL"
    assert reducer.state.source_observation_metadata["source_anomaly_count"] == 2
    assert len(reducer.state.source_observation_metadata["source_anomaly_hashes"]) == 2

    from examples.run_task008_q2frame_auction_engine_shadow import _inventory

    q2frame_path = tmp_path / "fault-injected-real-q2frame.jsonl"
    q2frame_path.write_text(json.dumps(frame) + "\n", encoding="utf-8")
    inventory = _inventory(q2frame_path, captured["trade_date"])
    assert inventory["update_count"] == 1
    assert inventory["symbol_count"] == 1
    assert inventory["skipped_update_count"] == 2
    assert len(inventory["source_anomaly_hashes"]) == 2


def test_empty_q2frame_advances_timeline_without_inventing_universe():
    timestamp_ms = 1788398650000
    frame = {
        "version": "Q2FrameV1",
        "seq_no": 1,
        "logical_ts_ms": timestamp_ms,
        "q2_updates": [],
    }
    clock = VirtualClock(datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc))
    source = Q2FrameReplaySource("2026-09-03", (), clock)

    projection = source.apply(frame)

    assert source.last_seq_no == 1
    assert source.last_logical_ts_ms == timestamp_ms
    assert projection.quotes == {}
    assert projection.expected_symbols == ()
    assert projection.coverage == 0.0
    assert projection.status.value == "MISSING"
    assert projection.consistency_status == "EMPTY_UNIVERSE"
    assert clock.now_utc() == datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)


def test_q2frame_replay_does_not_apply_market_hours_gate():
    """Producer source time is data; freshness is a separate policy."""

    first = {
        "version": "Q2FrameV1",
        "seq_no": 1,
        "logical_ts_ms": 1789689600000,  # 2026-09-18 08:00 Asia/Shanghai
        "q2_updates": [{"symbol": "600519", "px": 100000, "pc": 99000, "ts": 1789689600000}],
    }
    second = {
        "version": "Q2FrameV1",
        "seq_no": 2,
        "logical_ts_ms": 1789745370000,  # 2026-09-18 23:29:30 Asia/Shanghai
        "q2_updates": [{"symbol": "600519", "px": 101000, "pc": 99000, "ts": 1789745370000}],
    }
    clock = VirtualClock(datetime.fromtimestamp(first["logical_ts_ms"] / 1000, timezone.utc))
    source = Q2FrameReplaySource("2026-09-18", ("600519",), clock)

    first_projection = source.apply(first)
    second_projection = source.apply(second)

    assert first_projection.quotes["600519"].price_milli == 100000
    assert second_projection.quotes["600519"].price_milli == 101000
    assert source.last_seq_no == 2


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


def test_q2frame_replay_streams_frames_with_only_one_frame_of_lookahead():
    source, _ = _source()
    first_time = 1788398108000
    engine = _CaptureEngine()

    def frame(seq_no, logical_ts_ms):
        return {
            "version": "Q2FrameV1",
            "seq_no": seq_no,
            "logical_ts_ms": logical_ts_ms,
            "q2_updates": [],
        }

    def frames():
        yield frame(1, first_time)
        yield frame(2, first_time)
        yield frame(3, first_time + 3_000)
        # The third frame reveals the end of the first same-time group. It is
        # the only frame of lookahead; the group must drain before the source
        # iterator is asked for a fourth frame.
        assert engine.processed_ids == [
            "q2frame:1",
            "q2frame:2",
        ]
        yield frame(4, first_time + 6_000)
        assert engine.processed_ids == [
            "q2frame:1",
            "q2frame:2",
            "q2frame:3",
        ]

    replay_q2frames(frames(), source, engine)

    assert engine.processed_ids == [
        "q2frame:1",
        "q2frame:2",
        "q2frame:3",
        "q2frame:4",
    ]
    assert engine.group_sizes == [2, 1, 1]


@pytest.mark.parametrize("timer_offset_ms", [1_000, 3_000, 6_000])
def test_q2frame_replay_does_not_drain_timer_ahead_of_market_frames(timer_offset_ms):
    source, clock = _source()
    first_time = 1788398108000
    observed_clock_ms = []

    class ClockProbeStrategy(ProbeStrategy):
        def evaluate(self, snapshot, bundle):
            observed_clock_ms.append(int(clock.now_utc().timestamp() * 1000))
            return super().evaluate(snapshot, bundle)

    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ClockProbeStrategy(),
        session_id="2026-09-03",
        phase="REPLAY",
    )
    engine.submit(
        EngineSignal(
            "auction-timer",
            first_time + timer_offset_ms,
            100,
            SignalKind.TIMER,
            {"trigger_id": "probe"},
        )
    )

    def frame(seq_no, logical_ts_ms, price):
        return {
            "version": "Q2FrameV1",
            "seq_no": seq_no,
            "logical_ts_ms": logical_ts_ms,
            "q2_updates": [
                {
                    "symbol": "600519",
                    "px": price,
                    "pc": 99000,
                    "ts": logical_ts_ms,
                }
            ],
        }

    replay_q2frames(
        [
            frame(1, first_time, 100000),
            frame(2, first_time + 3_000, 101000),
        ],
        source,
        engine,
    )

    # The frame at +3s must be reduced before a timer at +3s, while a timer at
    # +6s must stay queued until the replay reaches that logical time. A timer
    # between frame times must observe the clock at its own logical time.
    assert engine._reducer.state.revision == 2
    assert engine._reducer.state.symbol_states["600519"]["price_milli"] == 101000
    assert clock.now_utc() == datetime.fromtimestamp(
        (first_time + 3_000) / 1000,
        timezone.utc,
    )
    if timer_offset_ms == 1_000:
        assert len(engine._strategy_results) == 1
        assert engine._strategy_results[-1].trace["logical_time_ms"] == first_time + 1_000
        assert engine._strategy_results[-1].trace["market_state_revision"] == 1
        assert observed_clock_ms == [first_time + 1_000]
    elif timer_offset_ms == 3_000:
        assert len(engine._strategy_results) == 1
        assert engine._strategy_results[-1].trace["logical_time_ms"] == first_time + 3_000
        assert engine._strategy_results[-1].trace["market_state_revision"] == 2
        assert observed_clock_ms == [first_time + 3_000]
    else:
        assert len(engine._strategy_results) == 0
        engine.run_through(
            first_time + 6_000,
            before_consume=source.advance_before_consume,
        )
        assert len(engine._strategy_results) == 1
        assert engine._strategy_results[-1].trace["market_state_revision"] == 2
        assert observed_clock_ms == [first_time + 6_000]


@pytest.mark.parametrize("end_offset_ms", [6_000, 6_999])
def test_q2frame_replay_advances_explicit_end_after_last_market_frame(end_offset_ms):
    source, clock = _source()
    first_time = 1788398108000
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ProbeStrategy(),
        session_id="2026-09-03",
        phase="REPLAY",
    )
    engine.submit(
        EngineSignal(
            "auction-timer",
            first_time + 6_000,
            100,
            SignalKind.TIMER,
            {"trigger_id": "probe"},
        )
    )

    replay_q2frames(
        [
            {
                "version": "Q2FrameV1",
                "seq_no": 1,
                "logical_ts_ms": first_time,
                "q2_updates": [
                    {"symbol": "600519", "px": 100000, "pc": 99000, "ts": first_time}
                ],
            },
            {
                "version": "Q2FrameV1",
                "seq_no": 2,
                "logical_ts_ms": first_time + 3_000,
                "q2_updates": [
                    {
                        "symbol": "600519",
                        "px": 101000,
                        "pc": 99000,
                        "ts": first_time + 3_000,
                    }
                ],
            },
            {
                "version": "Q2FrameV1",
                "seq_no": 3,
                "logical_ts_ms": first_time + 9_000,
                "q2_updates": [
                    {
                        "symbol": "600519",
                        "px": 102000,
                        "pc": 99000,
                        "ts": first_time + 9_000,
                    }
                ],
            },
        ],
        source,
        engine,
        end_logical_time_ms=first_time + end_offset_ms,
    )

    assert source.last_seq_no == 2
    assert engine._reducer.state.revision == 2
    assert len(engine._strategy_results) == 1
    assert engine._strategy_results[0].trace["logical_time_ms"] == first_time + 6_000
    assert engine._strategy_results[0].trace["market_state_revision"] == 2
    assert clock.now_utc() == datetime.fromtimestamp(
        (first_time + 6_000) / 1000,
        timezone.utc,
    )


def test_q2frame_replay_truncates_subseconds_before_timer_grouping():
    source, clock = _source()
    first_time = 1788398108000
    observed_clock_ms = []

    class ClockProbeStrategy(ProbeStrategy):
        def evaluate(self, snapshot, bundle):
            observed_clock_ms.append(int(clock.now_utc().timestamp() * 1000))
            return super().evaluate(snapshot, bundle)

    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ClockProbeStrategy(),
        session_id="2026-09-03",
        phase="REPLAY",
    )
    engine.submit(
        EngineSignal(
            "auction-timer",
            first_time + 6_000,
            100,
            SignalKind.TIMER,
            {"trigger_id": "probe"},
        )
    )
    replay_q2frames(
        [
            {
                "version": "Q2FrameV1",
                "seq_no": 1,
                "logical_ts_ms": first_time + 6_197,
                "q2_updates": [
                    {
                        "symbol": "600519",
                        "px": 100000,
                        "pc": 99000,
                        "ts": first_time + 6_197,
                    }
                ],
            },
            {
                "version": "Q2FrameV1",
                "seq_no": 2,
                "logical_ts_ms": first_time + 6_999,
                "q2_updates": [
                    {
                        "symbol": "600519",
                        "px": 101000,
                        "pc": 99000,
                        "ts": first_time + 6_999,
                    }
                ],
            },
        ],
        source,
        engine,
    )

    assert engine._reducer.state.revision == 2
    assert engine._reducer.state.symbol_states["600519"]["price_milli"] == 101000
    assert source._raw_hashes["600519"]["ts"] == first_time + 6_999
    assert len(engine._strategy_results) == 1
    assert engine._strategy_results[0].trace["logical_time_ms"] == first_time + 6_000
    assert engine._strategy_results[0].trace["market_state_revision"] == 2
    assert observed_clock_ms == [first_time + 6_000]
    assert clock.now_utc() == datetime.fromtimestamp(
        (first_time + 6_000) / 1000,
        timezone.utc,
    )


@pytest.mark.parametrize("end_logical_time_ms", [0, -1, True, 1.5, "6000"])
def test_q2frame_replay_rejects_invalid_explicit_end_time(end_logical_time_ms):
    source, _ = _source()
    engine = _CaptureEngine()

    with pytest.raises(ValueError, match="end_logical_time_ms"):
        replay_q2frames(
            [],
            source,
            engine,
            end_logical_time_ms=end_logical_time_ms,
        )


class _CaptureEngine:
    def __init__(self):
        self.pending = []
        self.processed_ids = []
        self.group_sizes = []

    def submit(self, signal):
        self.pending.append(signal)

    def run_through(self, logical_time_ms, *, before_consume=None):
        if before_consume is not None:
            for signal in self.pending:
                before_consume(signal)
        self.processed_ids.extend(signal.signal_id for signal in self.pending)
        self.group_sizes.append(len(self.pending))
        self.pending.clear()


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


def test_q2frame_parser_rejects_bad_frame_shape_but_isolates_bad_member():
    with pytest.raises(ValueError):
        Q2FrameV1.from_mapping({
            "version": "Q2FrameV1",
            "seq_no": 1,
            "logical_ts_ms": 1,
            "q2_updates": {"symbol": "600519"},
        })
    parsed = Q2FrameV1.from_mapping({
        "version": "Q2FrameV1",
        "seq_no": 1,
        "logical_ts_ms": 1,
        "q2_updates": [{"symbol": "600519.SH"}],
    })
    assert parsed.q2_updates == ()
    assert len(parsed.source_anomaly_hashes) == 1


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


def test_td_event_replay_keeps_out_of_scope_tick_and_reports_partial_evidence():
    source, _, _ = _td_source()
    extra_row = {**_td_rows()[0], "symbol": "000001"}

    slices = source.event_slices([extra_row])
    assert len(slices) == 1
    assert len(slices[0].events) == 1
    assert slices[0].events[0].symbol == "000001"
    assert slices[0].out_of_scope_symbols == ("000001",)

    signal = source.signals_for([extra_row])[0]
    assert signal.payload.status.value == "PARTIAL"
    assert signal.payload.out_of_scope_symbols == ("000001",)
    assert signal.payload.out_of_scope_event_hashes == (slices[0].events[0].content_hash,)
    assert signal.payload.out_of_scope_events[0]["symbol"] == "000001"
    assert "000001" not in signal.payload.quotes

    replay_source, _, _ = _td_source()
    replay_rows = [
        {**_td_rows()[0], "ts": "2026-09-03 09:20:00", "symbol": "000001"},
        {**_td_rows()[1], "ts": "2026-09-03 09:20:02", "symbol": "600519"},
    ]
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("wide", 0, 10**15),)),
        ProbeStrategy(),
        session_id="2026-09-03-out-of-scope",
        phase="REPLAY",
    )
    replay_td_event_time(replay_rows, replay_source, engine)
    result = engine.run_until_empty()

    assert result.processed_signals == 2
    metadata = engine._reducer.state.source_observation_metadata
    assert metadata["out_of_scope_symbols"] == ("000001",)
    assert metadata["out_of_scope_event_hashes"]
    assert metadata["out_of_scope_events"][0]["symbol"] == "000001"
    assert metadata["out_of_scope_event_count"] == 1
    assert metadata["out_of_scope_evidence_hash"]
    assert engine._reducer.state.completeness == "READY"


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
