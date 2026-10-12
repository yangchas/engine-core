"""Causal, explicitly hypothetical arrival-lag simulation for Q2Frame replay.

This module does not infer Rabbit delivery, historical availability, or live
arrival order. It only applies a named lag assumption to the logical time of
each captured Q2Frame (or, for stress-only runs, to each individual update).
Source update timestamps and payloads are preserved unchanged.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .contracts import canonical_hash, deep_freeze
from .replay import Q2FrameV1


ARRIVAL_MODEL_KIND = "HYPOTHETICAL_ARRIVAL_MODEL"
RUN_MODE = "HYPOTHETICAL_ARRIVAL_LAG_SENSITIVITY"
SLOT_CONVENTION = "CAUSAL_RIGHT_CLOSED"


@dataclass(frozen=True)
class ArrivalLagModelV1:
    """A transparent lag probe; it is never an empirical delay distribution."""

    mode: str
    max_delay_ms: int = 0
    seed: str = "arrival-lag-v1"

    def __post_init__(self) -> None:
        if self.mode not in {"NONE", "CONSTANT", "PER_UPDATE_HASH"}:
            raise ValueError("unsupported arrival lag mode")
        if type(self.max_delay_ms) is not int or self.max_delay_ms < 0:
            raise ValueError("max_delay_ms must be a non-negative integer")
        if self.mode == "NONE" and self.max_delay_ms != 0:
            raise ValueError("NONE mode requires max_delay_ms=0")
        if not isinstance(self.seed, str) or not self.seed:
            raise ValueError("seed must be a non-empty string")

    @classmethod
    def none(cls) -> "ArrivalLagModelV1":
        return cls("NONE")

    @classmethod
    def constant(cls, delay_ms: int) -> "ArrivalLagModelV1":
        return cls("CONSTANT", delay_ms)

    @classmethod
    def per_update_hash(
        cls, max_delay_ms: int, *, seed: str = "arrival-lag-v1"
    ) -> "ArrivalLagModelV1":
        return cls("PER_UPDATE_HASH", max_delay_ms, seed)

    @property
    def metadata(self) -> Mapping[str, Any]:
        realism = {
            "NONE": "NONE_SHIFT_PROBE",
            "CONSTANT": "NONE_SHIFT_PROBE",
            "PER_UPDATE_HASH": "NONE_STRESS_ONLY",
        }[self.mode]
        return {
            "kind": ARRIVAL_MODEL_KIND,
            "run_mode": RUN_MODE,
            "model_version": "ArrivalLagModelV1",
            "mode": self.mode,
            "assumed_delay_ms": self.max_delay_ms,
            "distribution_realism": realism,
            "rabbit_arrival": "UNKNOWN",
            "historical_available_at": "UNKNOWN_NOT_INFERRED",
            "claims": ["NOT_REAL_ARRIVAL", "NOT_LIVE_REPLAY_VALIDATION", "NOT_NORMAL_PARITY"],
            "source_time_basis": "Q2FRAME_LOGICAL_TIME_PLUS_ASSUMED_DELAY",
            "update_source_timestamps_modified": False,
        }

    def _hashed_delay(
        self,
        *,
        frame: Q2FrameV1,
        source_frame_index: int,
        update_index: Optional[int],
        update: Optional[Mapping[str, Any]],
    ) -> int:
        if self.max_delay_ms == 0:
            return 0
        digest = canonical_hash({
            "contract": "ArrivalLagModelV1",
            "seed": self.seed,
            "source_frame_index": source_frame_index,
            "source_seq_no": frame.seq_no,
            "source_logical_ts_ms": frame.logical_ts_ms,
            "source_update_index": update_index,
            "update": update,
        })
        return int(digest[:16], 16) % (self.max_delay_ms + 1)

    def arrival_time_ms(
        self,
        frame: Q2FrameV1,
        *,
        source_frame_index: int,
        update_index: Optional[int] = None,
        update: Optional[Mapping[str, Any]] = None,
    ) -> int:
        if self.mode == "NONE":
            delay_ms = 0
        elif self.mode == "CONSTANT":
            delay_ms = self.max_delay_ms
        else:
            delay_ms = self._hashed_delay(
                frame=frame,
                source_frame_index=source_frame_index,
                update_index=update_index,
                update=update,
            )
        return frame.logical_ts_ms + delay_ms


@dataclass(frozen=True)
class HypotheticalReadV1:
    """One read poll and the source Q2 updates newly visible at that poll."""

    read_time_ms: int
    frame: Optional[Q2FrameV1]
    max_arrival_ms: Optional[int]
    source_frame_seq_nos: tuple[int, ...]


def _validate_ms(value: int, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("%s must be a positive integer timestamp" % name)
    return value


def _ceil_to_cadence(timestamp_ms: int, cadence_ms: int) -> int:
    return ((timestamp_ms + cadence_ms - 1) // cadence_ms) * cadence_ms


def _source_frame(value: Mapping[str, Any] | Q2FrameV1) -> Q2FrameV1:
    if isinstance(value, Q2FrameV1):
        # Copy/freeze direct dataclass fixtures too; from_mapping already does so.
        return Q2FrameV1(
            seq_no=value.seq_no,
            logical_ts_ms=value.logical_ts_ms,
            q2_updates=tuple(deep_freeze(dict(update)) for update in value.q2_updates),
            phase=value.phase,
            source_anomaly_hashes=value.source_anomaly_hashes,
        )
    return Q2FrameV1.from_mapping(value)


def iter_hypothetical_reads(
    frames: Iterable[Mapping[str, Any] | Q2FrameV1],
    model: ArrivalLagModelV1,
    *,
    cadence_ms: int = 1_000,
    evaluation_times_ms: Sequence[int] = (),
    diagnostics: Optional[dict[str, Any]] = None,
) -> Iterator[HypotheticalReadV1]:
    """Stream captured Q2Frames through causal periodic reads.

    A read at ``R`` receives arrivals in ``(previous_read, R]``; the first
    read receives all arrivals at or before its explicitly aligned R0.  R0 is
    the first cadence boundary at or after the first source frame's logical
    timestamp. Exact evaluation reads are unioned with the cadence grid. Empty
    source frames are retained when they become visible; empty polling slots
    have ``frame=None`` and are not fabricated into market input.
    """

    if type(cadence_ms) is not int or cadence_ms <= 0 or cadence_ms % 1_000:
        raise ValueError("cadence_ms must be a positive whole-second multiple")
    eval_times = tuple(sorted({_validate_ms(value, "evaluation_time_ms") for value in evaluation_times_ms}))
    source_iter = iter(frames)
    try:
        first = _source_frame(next(source_iter))
    except StopIteration as exc:
        raise ValueError("at least one source Q2Frame is required") from exc
    _validate_ms(first.logical_ts_ms, "logical_ts_ms")

    initial_read_ms = _ceil_to_cadence(first.logical_ts_ms, cadence_ms)
    if eval_times and eval_times[0] < initial_read_ms:
        raise ValueError("evaluation times must be at or after the initial read")

    state = diagnostics if diagnostics is not None else {}
    counters: dict[str, Any] = {
        "input_frame_count": 0,
        "input_empty_frame_count": 0,
        "input_update_count": 0,
        "delivered_update_count": 0,
        "output_source_empty_frame_count": 0,
        "source_ts_regression_count": 0,
        "source_ts_after_frame_logical_count": 0,
        "source_ts_invalid_count": 0,
    }
    input_hash_sum = input_hash_xor = 0
    delivered_hash_sum = delivered_hash_xor = 0
    latest_source_ts_by_symbol: dict[str, int] = {}

    # Heap tuple orders events by assumed arrival, then source frame/update index.
    pending: list[tuple[int, int, int, str, int, Any, Any, Optional[int]]] = []
    next_frame: Optional[Q2FrameV1] = first
    next_source_index = 0
    previous_seq: Optional[int] = None
    previous_logical: Optional[int] = None
    last_read_ms: Optional[int] = None
    grid_read_ms = initial_read_ms
    eval_index = 0
    output_seq = 0

    def schedule_frame(frame: Q2FrameV1, source_index: int) -> None:
        nonlocal previous_seq, previous_logical, input_hash_sum, input_hash_xor
        _validate_ms(frame.logical_ts_ms, "logical_ts_ms")
        if previous_seq is not None and frame.seq_no != previous_seq + 1:
            raise ValueError("Q2Frame seq_no is not continuous")
        if previous_logical is not None and frame.logical_ts_ms < previous_logical:
            raise ValueError("Q2Frame logical_ts_ms moved backwards")
        previous_seq = frame.seq_no
        previous_logical = frame.logical_ts_ms
        counters["input_frame_count"] += 1
        if not frame.q2_updates:
            counters["input_empty_frame_count"] += 1
            arrival_ms = model.arrival_time_ms(frame, source_frame_index=source_index)
            heapq.heappush(
                pending,
                (arrival_ms, source_index, -1, "EMPTY", frame.seq_no, frame.phase, None, None),
            )
        for update_index, update in enumerate(frame.q2_updates):
            frozen = deep_freeze(dict(update))
            counters["input_update_count"] += 1
            update_hash = int(canonical_hash(frozen), 16)
            input_hash_sum = (input_hash_sum + update_hash) % (1 << 256)
            input_hash_xor ^= update_hash
            symbol = str(frozen.get("symbol", ""))
            source_ts = frozen.get("ts")
            try:
                source_ts_int = int(source_ts)
                if source_ts_int <= 0:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                counters["source_ts_invalid_count"] += 1
            else:
                previous_ts = latest_source_ts_by_symbol.get(symbol)
                if previous_ts is not None and source_ts_int < previous_ts:
                    counters["source_ts_regression_count"] += 1
                latest_source_ts_by_symbol[symbol] = source_ts_int
                if source_ts_int > frame.logical_ts_ms:
                    counters["source_ts_after_frame_logical_count"] += 1
            arrival_ms = model.arrival_time_ms(
                frame,
                source_frame_index=source_index,
                update_index=update_index,
                update=frozen,
            )
            heapq.heappush(
                pending,
                (
                    arrival_ms,
                    source_index,
                    update_index,
                    "UPDATE",
                    frame.seq_no,
                    frame.phase,
                    frozen,
                    update_hash,
                ),
            )

    def pull_due_source_frames(read_ms: int) -> None:
        nonlocal next_frame, next_source_index
        while next_frame is not None and next_frame.logical_ts_ms <= read_ms:
            frame = next_frame
            source_index = next_source_index
            next_source_index += 1
            schedule_frame(frame, source_index)
            try:
                next_frame = _source_frame(next(source_iter))
            except StopIteration:
                next_frame = None

    while True:
        next_eval = eval_times[eval_index] if eval_index < len(eval_times) else None
        if next_eval is None:
            read_ms = grid_read_ms
        else:
            read_ms = min(grid_read_ms, next_eval)
        if last_read_ms is not None and read_ms <= last_read_ms:
            if next_eval == read_ms:
                eval_index += 1
                continue
            grid_read_ms += cadence_ms
            continue

        pull_due_source_frames(read_ms)
        due: list[tuple[int, int, int, str, int, Any, Any, Optional[int]]] = []
        while pending and pending[0][0] <= read_ms:
            due.append(heapq.heappop(pending))
        due.sort(key=lambda item: (item[0], item[1], item[2]))

        updates: list[Mapping[str, Any]] = []
        source_seq_nos: list[int] = []
        phases: list[Any] = []
        due_arrivals: list[int] = []
        for item in due:
            arrival_ms, _, _, kind, source_seq_no, phase, payload, update_hash = item
            due_arrivals.append(arrival_ms)
            source_seq_nos.append(source_seq_no)
            phases.append(phase)
            if kind == "UPDATE":
                updates.append(payload)
                counters["delivered_update_count"] += 1
                delivered_hash_sum = (delivered_hash_sum + update_hash) % (1 << 256)
                delivered_hash_xor ^= update_hash

        read_frame: Optional[Q2FrameV1] = None
        if due:
            output_seq += 1
            phase = phases[0] if phases and all(value == phases[0] for value in phases) else None
            read_frame = Q2FrameV1(
                seq_no=output_seq,
                logical_ts_ms=read_ms,
                q2_updates=tuple(updates),
                phase=phase,
            )
            counters["output_source_empty_frame_count"] += sum(
                1 for item in due if item[3] == "EMPTY"
            )

        max_arrival = max(due_arrivals) if due_arrivals else None
        result = HypotheticalReadV1(
            read_time_ms=read_ms,
            frame=read_frame,
            max_arrival_ms=max_arrival,
            source_frame_seq_nos=tuple(sorted(set(source_seq_nos))),
        )
        last_read_ms = read_ms
        state.update(counters)
        state["input_identity_matches_delivered"] = (
            (counters["input_update_count"], input_hash_sum, input_hash_xor)
            == (counters["delivered_update_count"], delivered_hash_sum, delivered_hash_xor)
            if next_frame is None and not pending
            else None
        )
        state["slot_convention"] = SLOT_CONVENTION
        state["lookahead_max_ms"] = 0
        state["source_sequence_status"] = "UNKNOWN"
        state["rabbit_arrival"] = "UNKNOWN"
        state["historical_available_at"] = "UNKNOWN_NOT_INFERRED"
        yield result

        if next_eval == read_ms:
            eval_index += 1
        if read_ms == grid_read_ms:
            grid_read_ms += cadence_ms

        # No future input is required once source, due events, and requested
        # evaluation reads are exhausted. A cadence poll alone creates no frame.
        if next_frame is None and not pending and eval_index >= len(eval_times):
            break

    state.update(counters)
    state["input_identity_matches_delivered"] = (
        (counters["input_update_count"], input_hash_sum, input_hash_xor)
        == (counters["delivered_update_count"], delivered_hash_sum, delivered_hash_xor)
    )
    state["input_update_multiset_fingerprint"] = canonical_hash({
        "count": counters["input_update_count"],
        "sum_mod_2_256": input_hash_sum,
        "xor": input_hash_xor,
    })
    state["delivered_update_multiset_fingerprint"] = canonical_hash({
        "count": counters["delivered_update_count"],
        "sum_mod_2_256": delivered_hash_sum,
        "xor": delivered_hash_xor,
    })
    state["input_frame_count"] = counters["input_frame_count"]
    state["input_update_count"] = counters["input_update_count"]
    state["delivered_update_count"] = counters["delivered_update_count"]
