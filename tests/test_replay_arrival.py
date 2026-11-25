from __future__ import annotations

import pytest

from engine_core.replay import Q2FrameV1
from engine_core.replay_arrival import (
    ARRIVAL_MODEL_KIND,
    ArrivalLagModelV1,
    iter_hypothetical_reads,
)


def _frame(seq_no: int, logical_ms: int, *updates):
    return Q2FrameV1(
        seq_no=seq_no,
        logical_ts_ms=logical_ms,
        q2_updates=tuple(updates),
    )


def _collect(frames, model, *, cadence_ms=1_000, evaluation_times_ms=()):
    diagnostics = {}
    reads = tuple(
        iter_hypothetical_reads(
            frames,
            model,
            cadence_ms=cadence_ms,
            evaluation_times_ms=evaluation_times_ms,
            diagnostics=diagnostics,
        )
    )
    return reads, diagnostics


def test_none_model_delivers_left_boundary_once_and_preserves_source_order():
    frames = (
        _frame(1, 1_000, {"symbol": "000001", "ts": 1_000, "a25": 0}),
        _frame(2, 2_000, {"symbol": "000002", "ts": 2_000, "a25": 101}),
        _frame(3, 3_000, {"symbol": "000001", "ts": 3_000, "a25": 102}),
    )

    reads, diagnostics = _collect(frames, ArrivalLagModelV1.none())

    delivered = [
        (read.read_time_ms, dict(update))
        for read in reads
        if read.frame is not None
        for update in read.frame.q2_updates
    ]
    assert [read.read_time_ms for read in reads[:3]] == [1_000, 2_000, 3_000]
    assert [item[0] for item in delivered] == [1_000, 2_000, 3_000]
    assert [item[1]["symbol"] for item in delivered] == [
        "000001",
        "000002",
        "000001",
    ]
    assert [item[1]["ts"] for item in delivered] == [1_000, 2_000, 3_000]
    assert diagnostics["input_update_count"] == diagnostics["delivered_update_count"] == 3
    assert diagnostics["input_identity_matches_delivered"] is True
    assert all(
        read.frame is None or read.frame.logical_ts_ms >= read.max_arrival_ms
        for read in reads
    )


def test_constant_delay_is_causal_and_read_boundary_is_right_closed():
    frames = (
        _frame(1, 1_000, {"symbol": "000001", "ts": 900}),
        _frame(2, 2_000, {"symbol": "000002", "ts": 1_900}),
    )

    reads, _ = _collect(
        frames,
        ArrivalLagModelV1.constant(1_000),
        evaluation_times_ms=(2_000, 3_000),
    )

    by_time = {read.read_time_ms: read for read in reads}
    # The first source frame's assumed arrival is exactly 2,000ms. Because
    # read intervals are right-closed, it is visible at this read, not one
    # poll later.
    assert by_time[2_000].frame is not None
    assert [item["symbol"] for item in by_time[2_000].frame.q2_updates] == [
        "000001",
    ]
    assert by_time[3_000].frame is not None
    assert [item["symbol"] for item in by_time[3_000].frame.q2_updates] == [
        "000002",
    ]
    assert by_time[2_000].max_arrival_ms == 2_000
    assert by_time[3_000].max_arrival_ms == 3_000
    assert all(
        read.frame is None or read.frame.logical_ts_ms >= read.max_arrival_ms
        for read in reads
    )


def test_empty_source_frame_is_preserved_but_empty_poll_is_not_market_frame():
    frames = (
        _frame(1, 1_000, {"symbol": "000001", "ts": 1_000}),
        _frame(2, 2_000),
        _frame(3, 3_000, {"symbol": "000002", "ts": 3_000}),
    )

    reads, diagnostics = _collect(
        frames,
        ArrivalLagModelV1.none(),
        evaluation_times_ms=(2_500,),
    )

    by_time = {read.read_time_ms: read for read in reads}
    assert by_time[2_000].frame is not None
    assert by_time[2_000].frame.q2_updates == ()
    assert by_time[2_500].frame is None
    assert by_time[2_500].source_frame_seq_nos == ()
    assert by_time[3_000].frame is not None
    assert diagnostics["input_empty_frame_count"] == 1
    assert diagnostics["output_source_empty_frame_count"] == 1


def test_per_update_hash_is_deterministic_and_marked_stress_only():
    frames = (
        _frame(
            1,
            1_000,
            {"symbol": "000001", "ts": 1_000, "a25": 100},
            {"symbol": "000002", "ts": 1_000, "a25": 200},
        ),
        _frame(2, 2_000, {"symbol": "000003", "ts": 2_000, "a25": 300}),
    )
    model = ArrivalLagModelV1.per_update_hash(2_000)

    first, first_diagnostics = _collect(
        frames,
        model,
        cadence_ms=1_000,
        evaluation_times_ms=(3_000, 4_000),
    )
    second, second_diagnostics = _collect(
        frames,
        model,
        cadence_ms=1_000,
        evaluation_times_ms=(3_000, 4_000),
    )

    signature = lambda reads: [
        (
            read.read_time_ms,
            None
            if read.frame is None
            else tuple(dict(update) for update in read.frame.q2_updates),
        )
        for read in reads
    ]
    assert signature(first) == signature(second)
    assert first_diagnostics == second_diagnostics
    assert model.metadata["kind"] == ARRIVAL_MODEL_KIND
    assert model.metadata["distribution_realism"] == "NONE_STRESS_ONLY"
    assert model.metadata["rabbit_arrival"] == "UNKNOWN"
    assert first_diagnostics["input_identity_matches_delivered"] is True
    assert first_diagnostics["delivered_update_count"] == 3


def test_source_timestamp_regression_is_diagnostic_not_a_replay_failure():
    frames = (
        _frame(1, 1_000, {"symbol": "000001", "ts": 2_000}),
        _frame(2, 2_000, {"symbol": "000001", "ts": 1_500}),
    )

    reads, diagnostics = _collect(frames, ArrivalLagModelV1.none())

    assert len([read for read in reads if read.frame is not None]) == 2
    assert diagnostics["source_ts_regression_count"] == 1
    assert diagnostics["input_identity_matches_delivered"] is True


def test_nonmonotonic_or_noncontinuous_source_frames_are_rejected():
    with pytest.raises(ValueError, match="logical_ts_ms moved backwards"):
        _collect(
            (_frame(1, 2_000), _frame(2, 1_000)),
            ArrivalLagModelV1.none(),
        )

    with pytest.raises(ValueError, match="seq_no is not continuous"):
        _collect(
            (_frame(1, 1_000), _frame(3, 2_000)),
            ArrivalLagModelV1.none(),
        )


@pytest.mark.parametrize("cadence_ms", [500, 1_500, 0, -1])
def test_read_cadence_must_be_positive_whole_seconds(cadence_ms):
    with pytest.raises(ValueError, match="whole-second multiple"):
        _collect((_frame(1, 1_000),), ArrivalLagModelV1.none(), cadence_ms=cadence_ms)
