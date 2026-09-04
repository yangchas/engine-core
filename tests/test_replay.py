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
    VirtualClock,
    WindowManager,
    WindowSpec,
    build_q2_projection,
    replay_q2frames,
)
from engine_core.replay import Q2FrameV1


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
