import json
import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine_core import FreshnessPolicy, build_q2_projection
from examples.run_task008_q2frame_auction_engine_shadow import (
    _inventory,
    run_q2frame_auction_engine_shadow,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _epoch_ms(clock: str) -> int:
    return int(
        datetime.fromisoformat("2026-09-23T" + clock)
        .replace(tzinfo=SHANGHAI)
        .timestamp()
        * 1000
    )


def _frame(seq_no: int, clock: str, *, a25: int = 0, empty: bool = False) -> dict:
    logical_ts_ms = _epoch_ms(clock)
    updates = [] if empty else [
        {
            "symbol": "600519",
            "mk": "sh",
            "px": 10_000 + seq_no,
            "pc": 9_900,
            "amt": 1_000_000 + seq_no,
            "vol": 100 + seq_no,
            "ts": logical_ts_ms,
            "ph": 1,
            "a20": 0,
            "a24": 0,
            "a25": a25,
        }
    ]
    return {
        "version": "Q2FrameV1",
        "seq_no": seq_no,
        "logical_ts_ms": logical_ts_ms,
        "phase": 1,
        "q2_updates": updates,
    }


def _slice_frame(
    seq_no: int,
    slice_start: str,
    *,
    logical_time: str | None = None,
    slice_end: str | None = None,
    a25: int = 0,
    empty: bool = False,
) -> dict:
    start_ms = _epoch_ms(slice_start)
    logical_ms = _epoch_ms(logical_time or slice_start)
    end_ms = _epoch_ms(slice_end) if slice_end else start_ms + 3_000
    frame = _frame(seq_no, logical_time or slice_start, a25=a25, empty=empty)
    frame["logical_ts_ms"] = logical_ms
    frame["slice_start_ms"] = start_ms
    frame["slice_end_ms"] = end_ms
    return frame


def _write_frames(path: Path, frames: tuple[dict, ...]) -> None:
    path.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("gap_seconds", "expected_action", "at_stale_boundary"),
    (
        (57, "CONTINUE", False),
        (60, "CONTINUE", True),
        (63, "STOP_REQUIRED", False),
    ),
)
def test_inventory_gap_policy_uses_strict_existing_freshness_boundary(
    tmp_path: Path, gap_seconds: int, expected_action: str, at_stale_boundary: bool
):
    start = _epoch_ms("09:31:00.000")
    first = _slice_frame(1, "09:31:00.000")
    gap_ms = gap_seconds * 1_000
    next_start_ms = start + 3_000 + gap_ms
    second = _slice_frame(
        2 + gap_seconds // 3,
        datetime.fromtimestamp(next_start_ms / 1000, SHANGHAI).strftime("%H:%M:%S.%f"),
        logical_time=datetime.fromtimestamp(
            (next_start_ms + 2_000) / 1000, SHANGHAI
        ).strftime("%H:%M:%S.%f"),
    )
    source = tmp_path / f"gap-{gap_seconds}s.jsonl"
    _write_frames(source, (first, second))

    inventory = _inventory(
        source,
        "2026-09-23",
        stale_after_ms=60_000,
        include_opening=True,
    )
    diagnostic = inventory["_frame_gap_diagnostics"]

    assert diagnostic["missing_slice_count"] == gap_seconds // 3
    assert diagnostic["gap_segments"][0]["gap_ms"] == gap_ms
    assert diagnostic["gap_segments"][0]["action"] == expected_action
    assert diagnostic["gap_segments"][0]["at_stale_boundary"] is at_stale_boundary
    assert diagnostic["gap_segments"][0]["next_frame_consumed_by_replay"] is True
    assert diagnostic["gap_segments"][0]["freshness_exposure_ms"] == gap_ms
    assert diagnostic["gap_segments"][0]["freshness_exposure_end_ms"] == (
        diagnostic["gap_segments"][0]["gap_end_ms"]
    )


def test_inventory_records_sequence_gap_without_slice_gap_and_does_not_stop(
    tmp_path: Path,
):
    source = tmp_path / "sequence-only-gap.jsonl"
    _write_frames(
        source,
        (
            _slice_frame(7, "09:15:00.000", logical_time="09:15:02.000"),
            _slice_frame(9, "09:15:03.000", logical_time="09:15:05.000"),
        ),
    )

    inventory = _inventory(source, "2026-09-23")
    diagnostic = inventory["_frame_gap_diagnostics"]

    assert diagnostic["source_sequence_gap_count"] == 1
    assert diagnostic["source_sequence_gap_segment_count"] == 1
    assert diagnostic["slice_gap_segment_count"] == 0
    assert diagnostic["missing_slice_count"] == 0
    assert diagnostic["continuation_status"] == "CONTINUE"


def test_inventory_records_large_gap_but_does_not_stop_without_freshness_policy(
    tmp_path: Path,
):
    start = _epoch_ms("09:31:00.000")
    next_start_ms = start + 3_000 + 90_000
    second_clock = datetime.fromtimestamp(next_start_ms / 1000, SHANGHAI).strftime(
        "%H:%M:%S.%f"
    )
    source = tmp_path / "large-gap-no-freshness-policy.jsonl"
    _write_frames(
        source,
        (
            _slice_frame(1, "09:31:00.000"),
            _slice_frame(32, second_clock),
        ),
    )

    inventory = _inventory(
        source,
        "2026-09-23",
        stale_after_ms=None,
        include_opening=True,
    )

    assert inventory["_frame_gap_diagnostics"]["missing_slice_count"] == 30
    assert inventory["_frame_gap_diagnostics"]["continuation_status"] == "CONTINUE"
    assert inventory["_frame_gap_diagnostics"]["freshness_stop_threshold_ms"] is None


def test_gap_stop_uses_only_the_portion_intersecting_replay_scope(tmp_path: Path):
    source = tmp_path / "gap-crosses-replay-scope-end.jsonl"
    _write_frames(
        source,
        (
            _slice_frame(1, "09:31:08.000", logical_time="09:31:09.000"),
            _slice_frame(23, "09:32:14.000"),
        ),
    )

    inventory = _inventory(
        source,
        "2026-09-23",
        stale_after_ms=60_000,
        include_opening=True,
    )
    diagnostic = inventory["_frame_gap_diagnostics"]
    gap = diagnostic["gap_segments"][0]

    assert gap["gap_ms"] == 63_000
    assert gap["replay_scope_overlap_ms"] == 60_000
    assert gap["freshness_exposure_ms"] == 59_000
    assert gap["at_stale_boundary"] is False
    assert gap["action"] == "CONTINUE"
    assert diagnostic["continuation_status"] == "CONTINUE"


def test_inventory_does_not_infer_temporal_gap_across_missing_slice_metadata(
    tmp_path: Path,
):
    middle = _frame(2, "09:15:04.000")
    source = tmp_path / "partial-slice-metadata.jsonl"
    _write_frames(
        source,
        (
            _slice_frame(1, "09:15:00.000", logical_time="09:15:02.000"),
            middle,
            _slice_frame(3, "09:15:06.000", logical_time="09:15:08.000"),
        ),
    )

    inventory = _inventory(source, "2026-09-23")

    assert inventory["_frame_gap_diagnostics"]["slice_metadata_status"] == (
        "PARTIAL_UNVERIFIED"
    )
    assert inventory["_frame_gap_diagnostics"]["slice_gap_segment_count"] == 0


def test_legacy_same_second_raw_timestamp_regression_fails_during_inventory(
    tmp_path: Path,
):
    source = tmp_path / "legacy-subsecond-regression.jsonl"
    _write_frames(
        source,
        (
            _frame(1, "09:15:00.500"),
            _frame(2, "09:15:00.000"),
        ),
    )

    with pytest.raises(
        ValueError,
        match=r"previous_seq_no=1.*current_seq_no=2.*legacy frame without slice metadata",
    ):
        _inventory(source, "2026-09-23")


@pytest.mark.parametrize(
    ("frames", "message"),
    (
        (
            (
                _slice_frame(1, "09:15:00.000"),
                _slice_frame(1, "09:15:03.000"),
            ),
            "seq_no must increase",
        ),
        (
            (
                _slice_frame(2, "09:15:00.000"),
                _slice_frame(1, "09:15:03.000"),
            ),
            "seq_no must increase",
        ),
        (
            (
                _slice_frame(1, "09:15:00.000"),
                _slice_frame(
                    2,
                    "09:15:02.000",
                    logical_time="09:15:04.000",
                    slice_end="09:15:05.000",
                ),
            ),
            "slice intervals overlap",
        ),
        (
            (
                _slice_frame(1, "09:15:00.000", slice_end="09:15:02.000"),
            ),
            "slice width must be 3000ms",
        ),
        (
            (
                _slice_frame(
                    1,
                    "09:15:00.000",
                    logical_time="09:15:03.000",
                ),
            ),
            "logical timestamp is outside its half-open slice",
        ),
    ),
)
def test_inventory_rejects_order_or_slice_contract_corruption(
    tmp_path: Path, frames: tuple[dict, ...], message: str
):
    source = tmp_path / "invalid-q2frame.jsonl"
    _write_frames(source, frames)

    with pytest.raises(ValueError, match=message):
        _inventory(source, "2026-09-23")


def test_gap_crossing_0925_barrier_is_reported_without_suppressing_anchor_evidence(
    tmp_path: Path,
):
    source = tmp_path / "gap-crosses-0925.jsonl"
    _write_frames(
        source,
        (
            _slice_frame(
                1,
                "09:25:00.000",
                logical_time="09:25:02.000",
                a25=10_001,
            ),
            _slice_frame(
                4,
                "09:25:09.000",
                logical_time="09:25:11.000",
                a25=10_004,
            ),
        ),
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-23",
    )

    assert result["frame_gap_diagnostics"]["missing_slice_count"] == 2
    assert result["frame_gap_diagnostics"]["gap_segments"][0]["barriers_crossed"] == [
        "0925"
    ]
    assert "_frame_gap_diagnostics" not in result["inventory"]
    assert "source_sequence_start" not in result["inventory"]
    assert "source_sequence_end" not in result["inventory"]
    assert result["frame_gap_diagnostics"]["source_sequence_gap_count"] == 2
    assert result["ordered"]["input_frames_processed"] == 1
    assert result["ordered"]["anchor_evidence"]["0925"][
        "first_observable_ms"
    ] == _epoch_ms("09:25:06.000")
    assert result["ordered"]["anchor_evidence"]["0925"][
        "input_frames_included"
    ] == 1


def test_gap_beyond_opening_freshness_boundary_aborts_but_boundary_itself_does_not(
    tmp_path: Path,
):
    start_ms = _epoch_ms("09:31:07.000")
    boundary_next_start_ms = start_ms + 3_000 + 63_000
    boundary_clock = datetime.fromtimestamp(
        boundary_next_start_ms / 1000, SHANGHAI
    ).strftime("%H:%M:%S.%f")
    boundary_source = tmp_path / "gap-at-freshness-boundary.jsonl"
    _write_frames(
        boundary_source,
        (
            _slice_frame(1, "09:31:07.000", logical_time="09:31:08.000"),
            _slice_frame(23, boundary_clock),
        ),
    )
    at_boundary = run_q2frame_auction_engine_shadow(
        q2frame_path=boundary_source,
        trade_date="2026-09-23",
        include_opening=True,
    )
    assert at_boundary["frame_gap_diagnostics"]["continuation_status"] == "CONTINUE"
    assert at_boundary["frame_gap_diagnostics"]["gap_segments"][0][
        "at_stale_boundary"
    ] is True
    assert at_boundary["frame_gap_diagnostics"]["gap_segments"][0][
        "replay_scope_overlap_ms"
    ] == 61_000
    assert at_boundary["frame_gap_diagnostics"]["gap_segments"][0][
        "freshness_exposure_ms"
    ] == 60_000
    assert "OPENING_0932" in at_boundary["frame_gap_diagnostics"]["gap_segments"][0][
        "barriers_crossed"
    ]

    stop_start_ms = _epoch_ms("09:31:05.000")
    stop_next_start_ms = stop_start_ms + 3_000 + 63_000
    stop_clock = datetime.fromtimestamp(stop_next_start_ms / 1000, SHANGHAI).strftime(
        "%H:%M:%S.%f"
    )
    stop_source = tmp_path / "gap-over-freshness-boundary.jsonl"
    _write_frames(
        stop_source,
        (
            _slice_frame(1, "09:31:05.000", logical_time="09:31:06.000"),
            _slice_frame(23, stop_clock),
        ),
    )
    with pytest.raises(
        ValueError,
        match=(
            "gap_ms=63000, replay_scope_overlap_ms=63000, "
            "freshness_exposure_ms=62000, stale_after_ms=60000"
        ),
    ):
        run_q2frame_auction_engine_shadow(
            q2frame_path=stop_source,
            trade_date="2026-09-23",
            include_opening=True,
        )


def test_large_gap_after_opening_replay_scope_is_reported_but_does_not_abort(
    tmp_path: Path,
):
    source = tmp_path / "post-opening-large-gap.jsonl"
    _write_frames(
        source,
        (
            _slice_frame(
                1,
                "09:32:09.000",
                logical_time="09:32:10.500",
                a25=10_001,
            ),
            _slice_frame(
                23,
                "09:33:15.000",
                logical_time="09:33:16.000",
                a25=10_023,
            ),
        ),
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-23",
        include_opening=True,
    )
    diagnostics = result["frame_gap_diagnostics"]
    gap = diagnostics["gap_segments"][0]

    assert gap["gap_ms"] == 63_000
    assert gap["replay_scope_overlap_ms"] == 0
    assert gap["action"] == "OUTSIDE_REPLAY_SCOPE"
    assert diagnostics["out_of_scope_gap_segment_count"] == 1
    assert diagnostics["continuation_status"] == "CONTINUE"
    assert result["ordered"]["opening_evidence"]["OPENING_0932"][
        "evaluation_time_ms"
    ] == _epoch_ms("09:32:10.000")
    assert result["ordered"]["input_frames_processed"] == 1


def test_large_gap_is_in_scope_when_replay_continues_through_input(tmp_path: Path):
    source = tmp_path / "post-opening-gap-continue-through-input.jsonl"
    _write_frames(
        source,
        (
            _slice_frame(
                1,
                "09:32:09.000",
                logical_time="09:32:10.500",
                a25=10_001,
            ),
            _slice_frame(
                23,
                "09:33:15.000",
                logical_time="09:33:16.000",
                a25=10_023,
            ),
        ),
    )

    inventory = _inventory(
        source,
        "2026-09-23",
        stale_after_ms=60_000,
        include_opening=True,
        continue_through_input=True,
    )
    gap = inventory["_frame_gap_diagnostics"]["gap_segments"][0]
    assert gap["next_frame_consumed_by_replay"] is True
    assert gap["freshness_exposure_end_ms"] == gap["gap_end_ms"]
    assert gap["freshness_exposure_ms"] == 63_000

    with pytest.raises(
        ValueError,
        match=(
            "gap_ms=63000, replay_scope_overlap_ms=63000, "
            "freshness_exposure_ms=63000, stale_after_ms=60000"
        ),
    ):
        run_q2frame_auction_engine_shadow(
            q2frame_path=source,
            trade_date="2026-09-23",
            include_opening=True,
            continue_through_input=True,
        )


def test_gap_exposure_reaches_barrier_when_next_frame_is_not_consumed(
    tmp_path: Path,
):
    source = tmp_path / "gap-next-frame-after-opening-barrier.jsonl"
    _write_frames(
        source,
        (
            _slice_frame(1, "09:31:06.000", logical_time="09:31:07.000"),
            _slice_frame(
                22,
                "09:32:09.000",
                logical_time="09:32:11.500",
            ),
        ),
    )

    inventory = _inventory(
        source,
        "2026-09-23",
        stale_after_ms=60_000,
        include_opening=True,
    )
    diagnostic = inventory["_frame_gap_diagnostics"]
    gap = diagnostic["gap_segments"][0]
    assert gap["next_frame_consumed_by_replay"] is False
    assert gap["freshness_exposure_end_ms"] == diagnostic["replay_scope"][
        "final_barrier_ms"
    ]
    assert gap["freshness_exposure_ms"] == 61_000
    assert gap["action"] == "STOP_REQUIRED"

    with pytest.raises(
        ValueError,
        match=(
            "gap_ms=60000, replay_scope_overlap_ms=60000, "
            "freshness_exposure_ms=61000, stale_after_ms=60000"
        ),
    ):
        run_q2frame_auction_engine_shadow(
            q2frame_path=source,
            trade_date="2026-09-23",
            include_opening=True,
        )


def test_q2_freshness_equality_is_not_stale_and_matches_gap_boundary_rule():
    source_ms = _epoch_ms("09:31:00.000")
    observed_at = datetime.fromtimestamp(
        (source_ms + 60_000) / 1000, SHANGHAI
    )
    projection = build_q2_projection(
        "2026-09-23",
        observed_at,
        ("600519",),
        {
            "600519": {
                "mk": "sh",
                "px": 10_000,
                "pc": 9_900,
                "amt": 1_000_000,
                "vol": 100,
                "ts": source_ms,
            }
        },
        freshness_policy=FreshnessPolicy(stale_after_ms=60_000),
        source_id="q2-freshness-boundary-test",
    )

    assert observed_at.timestamp() * 1000 - source_ms == 60_000
    assert projection.stale_symbols == ()


def test_bounded_q2frame_start_skips_prior_auction_barriers_without_clock_rewind(
    tmp_path: Path,
):
    source = tmp_path / "bounded-q2frame.jsonl"
    frames = (
        _frame(1, "09:24:59.000"),
        _frame(2, "09:25:02.000", a25=10_002),
        _frame(3, "09:25:03.000", a25=10_003),
        _frame(4, "09:25:08.999", empty=True),
    )
    source.write_text(
        "".join(json.dumps(frame, separators=(",", ":")) + "\n" for frame in frames),
        encoding="utf-8",
    )

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-23",
    )

    assert result["deterministic"] is True
    anchors = result["ordered"]["anchor_evidence"]
    assert anchors["0920"]["status"] == "NOT_OBSERVED_IN_REPLAY_WINDOW"
    assert anchors["0924"]["status"] == "NOT_OBSERVED_IN_REPLAY_WINDOW"
    assert anchors["0920"]["reason_code"] == "FIRST_OBSERVABLE_PRECEDES_REPLAY_WINDOW"
    assert anchors["0924"]["reason_code"] == "FIRST_OBSERVABLE_PRECEDES_REPLAY_WINDOW"
    assert anchors["0925"]["auction_revision"]["state"] == "READY"
    assert result["ordered"]["virtual_clock_ms"] == _epoch_ms("09:25:06.000")
    assert result["ordered"]["first_excluded_frame"]["seq_no"] == 4


def test_barrier_sidecar_is_consumed_inside_the_real_slice_timeline(tmp_path: Path):
    source = tmp_path / "slice-q2frames.jsonl"
    frame = _frame(1, "09:24:11.000")
    frame["slice_start_ms"] = _epoch_ms("09:24:09.000")
    frame["slice_end_ms"] = _epoch_ms("09:24:12.000")
    frame["q2_updates"][0]["a24"] = 0
    source.write_text(
        json.dumps(frame, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    barrier_path = tmp_path / "barrier-q2frames.jsonl"
    barrier_update = dict(frame["q2_updates"][0])
    barrier_update["px"] = 10_010
    barrier_update["a24"] = 10_010
    barrier_update["ts"] = _epoch_ms("09:24:10.000")
    barrier_record = {
        "version": "Q2FrameV1",
        "record_kind": "barrier_snapshot",
        "barrier_tag": "0924",
        "seq_no": 1,
        "logical_ts_ms": _epoch_ms("09:24:10.000"),
        "phase": 1,
        "q2_updates": [barrier_update],
    }
    barrier_path.write_text(
        json.dumps(barrier_record, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    barrier_sha = hashlib.sha256(barrier_path.read_bytes()).hexdigest()

    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-23",
        expected_sha256=source_sha,
        barrier_q2frame_path=barrier_path,
        expected_barrier_q2frame_sha256=barrier_sha,
    )

    assert result["deterministic"] is True
    assert result["barrier_q2frame"]["sha256"] == barrier_sha
    applied = result["ordered"]["barrier_snapshot_applied_by_tag"]["0924"]
    assert applied["status"] == "APPLIED"
    assert applied["update_count"] == 1
    anchor = result["ordered"]["anchor_evidence"]["0924"]["auction_revision"]
    assert anchor["anchor_available_symbol_count"] == 1
    assert result["ordered"]["input_frames_processed"] == 1


def test_barrier_sidecar_at_exclusive_slice_end_is_not_consumed(tmp_path: Path):
    source = tmp_path / "slice-q2frames.jsonl"
    frame = _frame(1, "09:24:09.000")
    frame["slice_start_ms"] = _epoch_ms("09:24:07.000")
    frame["slice_end_ms"] = _epoch_ms("09:24:10.000")
    source.write_text(
        json.dumps(frame, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    barrier_path = tmp_path / "barrier-q2frames.jsonl"
    barrier_update = dict(frame["q2_updates"][0])
    barrier_update["a24"] = 10_010
    barrier_update["ts"] = _epoch_ms("09:24:10.000")
    barrier_record = {
        "version": "Q2FrameV1",
        "record_kind": "barrier_snapshot",
        "barrier_tag": "0924",
        "seq_no": 1,
        "logical_ts_ms": _epoch_ms("09:24:10.000"),
        "phase": 1,
        "q2_updates": [barrier_update],
    }
    barrier_path.write_text(
        json.dumps(barrier_record, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    barrier_sha = hashlib.sha256(barrier_path.read_bytes()).hexdigest()
    result = run_q2frame_auction_engine_shadow(
        q2frame_path=source,
        trade_date="2026-09-23",
        expected_sha256=source_sha,
        barrier_q2frame_path=barrier_path,
        expected_barrier_q2frame_sha256=barrier_sha,
    )

    applied = result["ordered"]["barrier_snapshot_applied_by_tag"]["0924"]
    assert applied["status"] == "OUTSIDE_REPLAY_WINDOW"
    assert result["ordered"]["barrier_snapshot_update_count"] == 0
