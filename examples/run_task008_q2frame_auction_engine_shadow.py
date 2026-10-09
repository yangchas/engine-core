"""Replay real t1-v2 Q2Frame data through one Core auction Engine session.

The runner reads a frozen Q2FrameV1 JSONL artifact sequentially.  It applies
the current AuctionTimingPolicyV1 first-observable times as event-time timer
signals, including an empty source gap at 09:25:06.  Source timestamps retain
their raw milliseconds in the Q2 payload; replay logical time is floored to
whole seconds so every ``06.xxx`` event is assigned to the 09:25:06 second.

This is replay development evidence, not Rabbit arrival or NORMAL acceptance.
The input is produced by t1-v2; Core does not recompute Q2 or access external
services.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    AuctionShadowStrategy,
    AuctionTimeline,
    AuctionTimingPolicyV1,
    CrossSectionStateV1,
    DeterministicEngine,
    EngineSignal,
    FreshnessPolicy,
    FrozenDataBundle,
    MarketStateReducer,
    OpeningShadowStrategy,
    Q2FrameReplaySource,
    Q2FrameV1,
    SignalKind,
    VirtualClock,
    WindowManager,
    WindowSpec,
    canonical_hash,
    canonical_json,
    build_cross_section_facts,
    build_opening_amount_summary,
    build_opening_limit_state_summary,
    build_opening_plate_amount_summary,
    build_opening_plate_price_summary,
    build_opening_transition_summary,
    semantic_hash,
    validate_opening_plate_amount_context,
    validate_opening_plate_auction_pressure_context,
    validate_opening_plate_field_delta_context,
    validate_opening_plate_price_reference_context,
)
from engine_core.contracts import StrategyResult  # noqa: E402
from engine_core.market_summary import derive_q2_auction_summary  # noqa: E402
from engine_core.q2 import normalize_q2  # noqa: E402


SHANGHAI = ZoneInfo("Asia/Shanghai")
AUCTION_TAGS = ("0920", "0924", "0925")
OPENING_TAG = "OPENING_0932"
OPENING_EVALUATION_LOCAL = "09:32:10"
OPENING_STALE_AFTER_MS = 60_000
Q2FRAME_SLICE_MS = 3_000
FRAME_GAP_DIAGNOSTICS_CONTRACT = "Q2FrameGapDiagnosticsV1"
AUCTION_PRICE_FIELDS = (
    "auction_anchor_0920_price_milli",
    "auction_anchor_0924_price_milli",
    "auction_anchor_0925_price_milli",
)
PARTIAL_REASON_DIAGNOSTICS_CONTRACT = "PartialReasonDiagnosticsV1"
_PARTIAL_REASON_PRIORITY = (
    "INVALID_FACT_QUALITY",
    "MISSING_FACT_QUALITY",
    "AUCTION_ANCHOR_ABSENT_COOCCURRENCE",
    "UNKNOWN_FACT_QUALITY",
    "STRUCTURAL_ONLY_COMPARABLE",
)


def _floor_second(timestamp_ms: int) -> int:
    return timestamp_ms - timestamp_ms % 1000


def _local_datetime(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(SHANGHAI)


def _status_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "UNKNOWN").upper()


def _build_partial_reason_diagnostics(
    facts_by_symbol: Mapping[str, Mapping[str, Any]],
    *,
    auction_anchor_facts_by_tag: Mapping[str, Mapping[str, Mapping[str, Any]]],
    volume_semantics_unknown: bool,
) -> dict[str, Any]:
    """Summarize observed PARTIAL co-occurrences without changing fact semantics.

    This intentionally consumes only fields already present in the report
    trace. It does not infer source-field missingness from comparison labels,
    and anchor status is reported as corroborating co-occurrence only.
    """

    primary_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in sorted(facts_by_symbol):
        fact = facts_by_symbol[symbol]
        status = _status_text(fact.get("status", fact.get("quality_status")))
        quality_status = _status_text(fact.get("quality_status"))
        coverage_status = _status_text(fact.get("coverage_status"))
        changes = fact.get("changes", {})
        if not isinstance(changes, Mapping):
            changes = {}
        is_partial = (
            status != "READY"
            or quality_status != "READY"
            or coverage_status != "READY"
        )
        flags: set[str] = set()
        auction_anchor_statuses: dict[str, str] = {}
        comparable_change_fields: list[str] = []

        if not is_partial:
            primary_reason = "NOT_PARTIAL"
        else:
            if quality_status == "INVALID":
                flags.add("INVALID_FACT_QUALITY")
            elif quality_status == "MISSING":
                flags.add("MISSING_FACT_QUALITY")
            elif quality_status in {"UNKNOWN", "UNAVAILABLE"}:
                flags.add("UNKNOWN_FACT_QUALITY")

            for tag in AUCTION_TAGS:
                tag_facts = auction_anchor_facts_by_tag.get(tag, {})
                anchor_fact = tag_facts.get(symbol)
                anchor_status = (
                    _status_text(anchor_fact.get("status"))
                    if isinstance(anchor_fact, Mapping)
                    else "UNKNOWN"
                )
                auction_anchor_statuses[tag] = anchor_status
                if anchor_status in {"MISSING", "INVALID", "UNAVAILABLE"}:
                    flags.add("AUCTION_ANCHOR_ABSENT_COOCCURRENCE")

            if coverage_status != "READY":
                flags.add("COVERAGE_NOT_READY_STRUCTURAL")
            if volume_semantics_unknown:
                # Run-level fact read from the constructed child strategies.
                # Do not infer it from a per-symbol VOLUME_UNKNOWN label.
                flags.add("VOLUME_SEMANTICS_UNKNOWN")
            if changes.get("breadth") == "BREADTH_UNAVAILABLE":
                flags.add("BREADTH_UNAVAILABLE_OUT_OF_SCOPE")
            if changes.get("theme") == "THEME_UNAVAILABLE":
                flags.add("THEME_UNAVAILABLE_OUT_OF_SCOPE")

            unknown_change_values = {
                "PRICE_UNKNOWN",
                "VOLUME_UNKNOWN",
                "PRESSURE_UNKNOWN",
                "UNKNOWN",
                "UNAVAILABLE",
            }
            for field_name in ("price", "amount", "order_book"):
                value = changes.get(field_name)
                if value is not None and _status_text(value) not in unknown_change_values:
                    comparable_change_fields.append(field_name)

            primary_reason = next(
                (reason for reason in _PARTIAL_REASON_PRIORITY if reason in flags),
                None,
            )
            if primary_reason is None:
                out_of_scope_flags = {
                    "BREADTH_UNAVAILABLE_OUT_OF_SCOPE",
                    "THEME_UNAVAILABLE_OUT_OF_SCOPE",
                }
                structural_flags = {
                    "COVERAGE_NOT_READY_STRUCTURAL",
                    "VOLUME_SEMANTICS_UNKNOWN",
                    *out_of_scope_flags,
                }
                all_anchors_available = all(
                    auction_anchor_statuses.get(tag) == "AVAILABLE"
                    for tag in AUCTION_TAGS
                )
                if (
                    all_anchors_available
                    and comparable_change_fields
                    and flags.intersection(structural_flags)
                    and flags.issubset(structural_flags)
                ):
                    primary_reason = "STRUCTURAL_ONLY_COMPARABLE"
                elif flags and flags.issubset(out_of_scope_flags):
                    primary_reason = "OUT_OF_SCOPE_ONLY"
                else:
                    primary_reason = "UNKNOWN_PARTIAL_REASON"

        ordered_flags = sorted(flags)
        primary_counts[primary_reason] += 1
        flag_counts.update(ordered_flags)
        by_symbol[symbol] = {
            "fact_status": status,
            "quality_status": quality_status,
            "coverage_status": coverage_status,
            "primary_reason": primary_reason,
            "reason_flags": ordered_flags,
            "auction_anchor_statuses": auction_anchor_statuses,
            "comparable_change_fields": comparable_change_fields,
        }

    return {
        "contract": PARTIAL_REASON_DIAGNOSTICS_CONTRACT,
        "interpretation": "OBSERVED_COOCCURRENCE_NOT_CAUSAL",
        "scope": "PARTIAL_ADJACENT_AUCTION_FACTS",
        "primary_reason_priority": list(_PARTIAL_REASON_PRIORITY)
        + ["OUT_OF_SCOPE_ONLY", "UNKNOWN_PARTIAL_REASON"],
        "run_level_reason_flags": (
            ["VOLUME_SEMANTICS_UNKNOWN"] if volume_semantics_unknown else []
        ),
        "symbol_count": len(facts_by_symbol),
        "partial_symbol_count": sum(
            row["fact_status"] != "READY"
            or row["quality_status"] != "READY"
            or row["coverage_status"] != "READY"
            for row in by_symbol.values()
        ),
        "primary_reason_counts": dict(sorted(primary_counts.items())),
        "reason_flag_counts": dict(sorted(flag_counts.items())),
        "limitations": {
            "field_level_missing_invalid": "NOT_EXPOSED_IN_REPORT_TRACE",
            "auction_anchor_status": "ALL_0920_0924_0925_ANCHORS_CHECKED; COOCCURRENCE_ONLY_NOT_CAUSAL",
            "coverage_status": "STRUCTURAL_FLAG; NOT A SYMBOL_LEVEL_CAUSAL_REASON",
            "volume_semantics": (
                "UNKNOWN_AT_RUN_LEVEL; not inferred per symbol from comparison labels"
                if volume_semantics_unknown
                else "NOT_MARKED_UNKNOWN_BY_RUNNER"
            ),
            "unknown_states": "PRESERVED_AS_UNKNOWN; not converted to numeric values",
        },
        "by_symbol": by_symbol,
    }


def _opening_evaluation_ms(trade_date: str) -> int:
    local = datetime.fromisoformat(f"{trade_date}T{OPENING_EVALUATION_LOCAL}")
    if local.tzinfo is None:
        local = local.replace(tzinfo=SHANGHAI)
    return int(local.astimezone(timezone.utc).timestamp() * 1000)


def _iter_raw(path: Path) -> Iterable[Mapping[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"Q2Frame line {line_no} must be an object")
            yield value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_barrier_q2frames(
    path: Path | None,
    *,
    trade_date: str,
) -> dict[str, Q2FrameV1]:
    if path is None:
        return {}
    snapshots: dict[str, Q2FrameV1] = {}
    expected_times = {
        tag: AuctionTimingPolicyV1.default(tag).at(trade_date)["first_observable_ms"]
        for tag in AUCTION_TAGS
    }
    for raw in _iter_raw(path):
        if raw.get("record_kind") != "barrier_snapshot":
            raise ValueError("barrier Q2Frame input must contain barrier_snapshot records")
        tag = str(raw.get("barrier_tag", ""))
        if not tag:
            raise ValueError("barrier Q2Frame tag must be non-empty")
        frame = Q2FrameV1.from_mapping(raw)
        if _local_datetime(frame.logical_ts_ms).date().isoformat() != trade_date:
            raise ValueError("barrier Q2Frame trade_date does not match requested trade_date")
        if tag in AUCTION_TAGS and _floor_second(frame.logical_ts_ms) != expected_times[tag]:
            raise ValueError("barrier Q2Frame logical time does not match its auction policy")
        if tag in snapshots:
            raise ValueError(f"duplicate barrier Q2Frame record for {tag}")
        snapshots[tag] = frame
    return snapshots


_OBSERVER_TIME_Q2_ERRORS = frozenset({"stale", "future_ts", "trade_date"})


def _canonical_q2_values_by_symbol(
    symbol_states: Mapping[str, Mapping[str, Any]],
    *,
    observer_time_error_counts: Counter[str],
) -> dict[str, dict[str, Any]]:
    """Keep normalized values and source errors, separating time diagnostics."""

    result = {}
    for symbol in sorted(symbol_states):
        values = dict(symbol_states[symbol])
        errors = tuple(values.get("field_errors", ()))
        observer_errors = tuple(error for error in errors if error in _OBSERVER_TIME_Q2_ERRORS)
        observer_time_error_counts.update(observer_errors)
        values["field_errors"] = tuple(
            error for error in errors if error not in _OBSERVER_TIME_Q2_ERRORS
        )
        result[symbol] = values
    return result


def _compare_core_q2_checkpoint(
    *,
    tag: str,
    checkpoint_ms: int,
    engine_state: Mapping[str, Mapping[str, Any]],
    sidecar: Q2FrameV1,
) -> dict[str, Any]:
    """Compare a passive Core reducer observation with a T1 snapshot.

    The T1 sidecar is an oracle only; its rows are never submitted to the Core
    Engine. Runtime freshness diagnostics are intentionally excluded because
    they depend on Core observation time and are absent from the raw sidecar.
    """

    sidecar_states: dict[str, Mapping[str, Any]] = {}
    duplicate_symbols = 0
    for update in sidecar.q2_updates:
        symbol = str(update["symbol"])
        if symbol in sidecar_states:
            duplicate_symbols += 1
        sidecar_states[symbol] = normalize_q2(symbol, update).to_mapping()

    core_observer_errors: Counter[str] = Counter()
    sidecar_observer_errors: Counter[str] = Counter()
    core_values = _canonical_q2_values_by_symbol(
        engine_state,
        observer_time_error_counts=core_observer_errors,
    )
    sidecar_values = _canonical_q2_values_by_symbol(
        sidecar_states,
        observer_time_error_counts=sidecar_observer_errors,
    )
    field_mismatch_counts: Counter[str] = Counter()
    mismatched_symbols: set[str] = set()
    all_symbols = sorted(set(core_values) | set(sidecar_values))
    for symbol in all_symbols:
        if symbol not in core_values or symbol not in sidecar_values:
            field_mismatch_counts["__symbol_presence__"] += 1
            mismatched_symbols.add(symbol)
            continue
        core_quote = core_values[symbol]
        sidecar_quote = sidecar_values[symbol]
        for field_name in sorted(set(core_quote) | set(sidecar_quote)):
            if core_quote.get(field_name) != sidecar_quote.get(field_name):
                field_mismatch_counts[field_name] += 1
                mismatched_symbols.add(symbol)

    core_hash = canonical_hash(core_values)
    sidecar_hash = canonical_hash(sidecar_values)
    status = (
        "VALUE_MISMATCH"
        if field_mismatch_counts
        else "NOT_COMPARABLE_DUPLICATE_SIDECAR_SYMBOLS"
        if duplicate_symbols
        else "CANONICAL_Q2_VALUES_EQUAL"
    )
    return {
        "contract": "CoreQ2CheckpointComparisonV1",
        "tag": tag,
        "status": status,
        "checkpoint_time_ms": checkpoint_ms,
        "sidecar_time_ms": sidecar.logical_ts_ms,
        "capture_policy": "PASSIVE_AFTER_EVENT_SECOND_NO_ENGINE_SIGNAL",
        "sidecar_injected_into_engine": False,
        "comparison_scope": (
            "Q2Quote.to_mapping excluding only observer-time field_errors"
        ),
        "field_errors_policy": (
            "source parse/validation errors compared; stale/future_ts/trade_date "
            "reported separately because they depend on Core observation policy"
        ),
        "core_observer_time_error_counts": dict(sorted(core_observer_errors.items())),
        "sidecar_observer_time_error_counts": dict(
            sorted(sidecar_observer_errors.items())
        ),
        "core_symbol_count": len(core_values),
        "sidecar_symbol_count": len(sidecar_values),
        "duplicate_sidecar_symbol_count": duplicate_symbols,
        "value_mismatch_count": sum(field_mismatch_counts.values()),
        "mismatched_symbol_count": len(mismatched_symbols),
        "field_mismatch_counts": dict(sorted(field_mismatch_counts.items())),
        "mismatched_symbols_sample": sorted(mismatched_symbols)[:20],
        "core_value_hash": core_hash,
        "sidecar_value_hash": sidecar_hash,
    }


def _inventory(
    path: Path,
    trade_date: str,
    *,
    stale_after_ms: int | None = None,
    include_opening: bool = False,
    continue_through_input: bool = False,
) -> dict[str, Any]:
    if stale_after_ms is not None and stale_after_ms < 0:
        raise ValueError("stale_after_ms must be non-negative")
    frame_count = update_count = empty_frame_count = 0
    missing_update_ts_count = subsecond_update_ts_count = 0
    frame_ts_mismatch_count = 0
    duplicate_symbol_occurrences = 0
    frame_minus_source_time = {
        "future": 0,
        "zero_to_under_3s": 0,
        "3s_to_under_60s": 0,
        "60s_or_more": 0,
        "min": None,
        "max": None,
    }
    symbols: set[str] = set()
    source_anomaly_hashes: list[str] = []
    first_raw_ms = last_raw_ms = None
    first_seq: int | None = None
    first_slice_start_ms = None
    last_slice_end_ms = None
    previous_seq: int | None = None
    previous_raw_ms: int | None = None
    previous_slice_end_ms: int | None = None
    previous_frame_had_slice = False
    source_sequence_gap_count = 0
    source_sequence_gaps: list[dict[str, int]] = []
    slice_gap_segments: list[dict[str, Any]] = []
    frames_with_slice_metadata = 0
    frames_without_complete_slice_metadata = 0
    raw_timestamp_regression_count = 0
    barriers = {
        tag: AuctionTimingPolicyV1.default(tag).at(trade_date)[
            "first_observable_ms"
        ]
        for tag in AUCTION_TAGS
    }
    if include_opening:
        barriers[OPENING_TAG] = _opening_evaluation_ms(trade_date)
    for raw in _iter_raw(path):
        frame = Q2FrameV1.from_mapping(raw)
        if previous_seq is not None and frame.seq_no <= previous_seq:
            raise ValueError(
                "Q2Frame seq_no must increase: "
                f"previous={previous_seq}, current={frame.seq_no}"
            )
        if first_seq is None:
            first_seq = frame.seq_no
        if previous_seq is not None and frame.seq_no > previous_seq + 1:
            missing_sequences = frame.seq_no - previous_seq - 1
            source_sequence_gap_count += missing_sequences
            source_sequence_gaps.append(
                {
                    "after_seq_no": previous_seq,
                    "before_seq_no": frame.seq_no,
                    "missing_sequence_count": missing_sequences,
                }
            )

        raw_slice_start_ms = raw.get("slice_start_ms")
        raw_slice_end_ms = raw.get("slice_end_ms")
        has_slice_keys = (
            "slice_start_ms" in raw or "slice_end_ms" in raw
        )
        has_complete_slice = (
            type(raw_slice_start_ms) is int
            and type(raw_slice_end_ms) is int
            and raw_slice_start_ms > 0
            and raw_slice_end_ms > 0
        )
        if has_complete_slice:
            frames_with_slice_metadata += 1
            slice_start_ms = raw_slice_start_ms
            slice_end_ms = raw_slice_end_ms
            if slice_end_ms - slice_start_ms != Q2FRAME_SLICE_MS:
                raise ValueError(
                    "Q2Frame slice width must be 3000ms: "
                    f"seq_no={frame.seq_no}, start_ms={slice_start_ms}, "
                    f"end_ms={slice_end_ms}"
                )
            if not slice_start_ms <= frame.logical_ts_ms < slice_end_ms:
                raise ValueError(
                    "Q2Frame logical timestamp is outside its half-open slice: "
                    f"seq_no={frame.seq_no}, logical_ts_ms={frame.logical_ts_ms}, "
                    f"slice=[{slice_start_ms},{slice_end_ms})"
                )
            if previous_frame_had_slice and previous_slice_end_ms is not None:
                if slice_start_ms < previous_slice_end_ms:
                    raise ValueError(
                        "Q2Frame slice intervals overlap: "
                        f"previous_end_ms={previous_slice_end_ms}, "
                        f"current_seq_no={frame.seq_no}, start_ms={slice_start_ms}"
                    )
                if slice_start_ms > previous_slice_end_ms:
                    gap_ms = slice_start_ms - previous_slice_end_ms
                    missing_slice_count = (
                        gap_ms // Q2FRAME_SLICE_MS
                        if gap_ms % Q2FRAME_SLICE_MS == 0
                        else None
                    )
                    crossed_barriers = [
                        tag
                        for tag, barrier_ms in barriers.items()
                        if previous_slice_end_ms <= barrier_ms < slice_start_ms
                    ]
                    slice_gap_segments.append(
                        {
                            "after_seq_no": previous_seq,
                            "before_seq_no": frame.seq_no,
                            "before_logical_ts_ms": frame.logical_ts_ms,
                            "before_replay_second_ms": _floor_second(
                                frame.logical_ts_ms
                            ),
                            "gap_start_ms": previous_slice_end_ms,
                            "gap_end_ms": slice_start_ms,
                            "gap_ms": gap_ms,
                            "missing_slice_count": missing_slice_count,
                            "gap_alignment_status": (
                                "WHOLE_SLICES"
                                if missing_slice_count is not None
                                else "NOT_WHOLE_SLICE_MULTIPLE"
                            ),
                            "barriers_crossed": crossed_barriers,
                            "freshness_threshold_ms": stale_after_ms,
                        }
                    )
            previous_slice_end_ms = slice_end_ms
        else:
            frames_without_complete_slice_metadata += 1

        if previous_raw_ms is not None and frame.logical_ts_ms < previous_raw_ms:
            raw_timestamp_regression_count += 1
            if previous_frame_had_slice or has_complete_slice or has_slice_keys:
                context = "slice-contract frame order"
            else:
                context = "legacy frame without slice metadata; cannot prove harmless jitter"
            raise ValueError(
                "Q2Frame logical timestamps moved backwards: "
                f"previous_seq_no={previous_seq}, previous_ms={previous_raw_ms}, "
                f"current_seq_no={frame.seq_no}, current_ms={frame.logical_ts_ms}; "
                f"{context}"
            )
        if _local_datetime(frame.logical_ts_ms).date().isoformat() != trade_date:
            raise ValueError("Q2Frame logical time is outside requested trade_date")
        previous_seq = frame.seq_no
        previous_raw_ms = frame.logical_ts_ms
        previous_frame_had_slice = has_complete_slice
        source_anomaly_hashes.extend(frame.source_anomaly_hashes)
        frame_count += 1
        update_count += len(frame.q2_updates)
        if not frame.q2_updates:
            empty_frame_count += 1
        if first_raw_ms is None:
            first_raw_ms = frame.logical_ts_ms
            raw_slice_start_ms = raw.get("slice_start_ms")
            if type(raw_slice_start_ms) is int and raw_slice_start_ms > 0:
                first_slice_start_ms = raw_slice_start_ms
        raw_slice_end_ms = raw.get("slice_end_ms")
        if type(raw_slice_end_ms) is int and raw_slice_end_ms > 0:
            last_slice_end_ms = raw_slice_end_ms
        last_raw_ms = frame.logical_ts_ms
        frame_second = _floor_second(frame.logical_ts_ms)
        symbols_in_frame: set[str] = set()
        for update in frame.q2_updates:
            symbol = str(update["symbol"])
            symbols.add(symbol)
            if symbol in symbols_in_frame:
                duplicate_symbol_occurrences += 1
            symbols_in_frame.add(symbol)
            source_ms = update.get("ts")
            if isinstance(source_ms, bool) or not isinstance(source_ms, int):
                missing_update_ts_count += 1
                continue
            if source_ms % 1000:
                subsecond_update_ts_count += 1
            if _floor_second(source_ms) != frame_second:
                frame_ts_mismatch_count += 1
            delta_ms = frame.logical_ts_ms - source_ms
            if delta_ms < 0:
                frame_minus_source_time["future"] += 1
            elif delta_ms < 3_000:
                frame_minus_source_time["zero_to_under_3s"] += 1
            elif delta_ms < 60_000:
                frame_minus_source_time["3s_to_under_60s"] += 1
            else:
                frame_minus_source_time["60s_or_more"] += 1
            min_delta = frame_minus_source_time["min"]
            max_delta = frame_minus_source_time["max"]
            frame_minus_source_time["min"] = (
                delta_ms if min_delta is None else min(min_delta, delta_ms)
            )
            frame_minus_source_time["max"] = (
                delta_ms if max_delta is None else max(max_delta, delta_ms)
            )
    if frame_count == 0:
        raise ValueError("Q2Frame artifact is empty")
    inventory = {
        "frame_count": frame_count,
        "update_count": update_count,
        "empty_frame_count": empty_frame_count,
        "non_empty_frame_count": frame_count - empty_frame_count,
        "symbol_count": len(symbols),
        "symbols": tuple(sorted(symbols)),
        "first_logical_ts_ms": first_raw_ms,
        "first_slice_start_ms": first_slice_start_ms,
        "replay_window_start_ms": first_slice_start_ms or first_raw_ms,
        "last_logical_ts_ms": last_raw_ms,
        "last_slice_end_ms": last_slice_end_ms,
        "replay_window_end_ms": last_slice_end_ms or last_raw_ms,
        "missing_update_ts_count": missing_update_ts_count,
        "subsecond_update_ts_count": subsecond_update_ts_count,
        "frame_second_mismatch_count": frame_ts_mismatch_count,
        "duplicate_symbol_occurrences_within_frame": duplicate_symbol_occurrences,
        "frame_minus_source_time_ms": {
            **frame_minus_source_time,
            "meaning": (
                "frame logical timestamp minus per-symbol source timestamp; "
                "not arrival latency"
            ),
        },
        "universe_basis": "UNIQUE_SYMBOLS_IN_FROZEN_Q2FRAME_ONLY",
    }
    if source_anomaly_hashes:
        inventory["skipped_update_count"] = len(source_anomaly_hashes)
        inventory["source_anomaly_hashes"] = tuple(sorted(source_anomaly_hashes))
    known_missing_slice_counts = [
        segment["missing_slice_count"]
        for segment in slice_gap_segments
        if segment["missing_slice_count"] is not None
    ]
    replay_scope_start_ms = _floor_second(
        first_slice_start_ms or int(first_raw_ms)
    )
    replay_scope_final_ms = max(barriers.values())
    if continue_through_input:
        replay_scope_final_ms = max(
            replay_scope_final_ms,
            _floor_second(int(last_raw_ms)),
        )
    replay_scope_end_exclusive_ms = replay_scope_final_ms + 1_000
    for segment in slice_gap_segments:
        input_overlap_start_ms = max(
            segment["gap_start_ms"], replay_scope_start_ms
        )
        input_overlap_end_ms = min(
            segment["gap_end_ms"], replay_scope_end_exclusive_ms
        )
        input_scope_overlap_ms = max(
            0, input_overlap_end_ms - input_overlap_start_ms
        )
        next_frame_consumed = (
            segment["before_replay_second_ms"] <= replay_scope_final_ms
        )
        freshness_exposure_end_ms = (
            segment["gap_end_ms"]
            if next_frame_consumed
            else replay_scope_final_ms
        )
        freshness_overlap_start_ms = max(segment["gap_start_ms"], replay_scope_start_ms)
        freshness_overlap_end_ms = freshness_exposure_end_ms
        freshness_exposure_ms = max(
            0, freshness_overlap_end_ms - freshness_overlap_start_ms
        )
        outside_scope_ms = max(
            0, segment["gap_ms"] - input_scope_overlap_ms
        )
        threshold = segment["freshness_threshold_ms"]
        segment["replay_scope_overlap_ms"] = input_scope_overlap_ms
        segment["freshness_exposure_ms"] = freshness_exposure_ms
        segment["next_frame_consumed_by_replay"] = next_frame_consumed
        segment["freshness_exposure_end_ms"] = freshness_exposure_end_ms
        segment["outside_replay_scope_ms"] = outside_scope_ms
        segment["at_stale_boundary"] = (
            threshold is not None
            and freshness_exposure_ms > 0
            and freshness_exposure_ms == threshold
        )
        if threshold is not None and freshness_exposure_ms > threshold:
            segment["action"] = "STOP_REQUIRED"
        elif input_scope_overlap_ms > 0:
            segment["action"] = "CONTINUE"
        else:
            segment["action"] = "OUTSIDE_REPLAY_SCOPE"
    stop_required = any(
        segment["action"] == "STOP_REQUIRED" for segment in slice_gap_segments
    )
    inventory["_frame_gap_diagnostics"] = {
        "contract": FRAME_GAP_DIAGNOSTICS_CONTRACT,
        "slice_width_ms": Q2FRAME_SLICE_MS,
        "slice_semantics": "[slice_start_ms,slice_end_ms)",
        "slice_metadata_status": (
            "ABSENT_LEGACY"
            if frames_with_slice_metadata == 0
            else "COMPLETE"
            if frames_without_complete_slice_metadata == 0
            else "PARTIAL_UNVERIFIED"
        ),
        "frames_with_slice_metadata": frames_with_slice_metadata,
        "frames_without_complete_slice_metadata": frames_without_complete_slice_metadata,
        "source_sequence_start": first_seq,
        "source_sequence_end": previous_seq,
        "source_sequence_gap_count": source_sequence_gap_count,
        "source_sequence_gap_segment_count": len(source_sequence_gaps),
        "source_sequence_gaps": source_sequence_gaps,
        "slice_gap_segment_count": len(slice_gap_segments),
        "missing_slice_count": sum(known_missing_slice_counts),
        "unclassified_slice_gap_segment_count": sum(
            segment["missing_slice_count"] is None for segment in slice_gap_segments
        ),
        "gap_segments": slice_gap_segments,
        "replay_scope": {
            "start_ms": replay_scope_start_ms,
            "input_end_exclusive_ms": replay_scope_end_exclusive_ms,
            "final_barrier_ms": replay_scope_final_ms,
            "freshness_evaluation_ms": replay_scope_final_ms,
            "include_opening": include_opening,
            "continue_through_input": continue_through_input,
        },
        "in_scope_gap_segment_count": sum(
            segment["replay_scope_overlap_ms"] > 0
            for segment in slice_gap_segments
        ),
        "out_of_scope_gap_segment_count": sum(
            segment["replay_scope_overlap_ms"] == 0
            for segment in slice_gap_segments
        ),
        "partially_out_of_scope_gap_segment_count": sum(
            segment["replay_scope_overlap_ms"] > 0
            and segment["outside_replay_scope_ms"] > 0
            for segment in slice_gap_segments
        ),
        "freshness_stop_threshold_ms": stale_after_ms,
        "freshness_exposure_policy": (
            "GAP_START_TO_NEXT_SLICE_START_IF_NEXT_FRAME_CONSUMED; "
            "OTHERWISE_TO_FINAL_BARRIER; SECOND_TRUNCATED_LOWER_BOUND_NOT_PER_SYMBOL_QUOTE_AGE"
        ),
        "continuation_status": "STOP_REQUIRED" if stop_required else "CONTINUE",
        "legacy_raw_timestamp_policy": "RAW_NONDECREASING_REQUIRED",
        "raw_timestamp_regression_count": raw_timestamp_regression_count,
        "source_sequence_gap_note": (
            "source sequence gaps are recorded; slice bounds determine temporal gaps"
        ),
    }
    return inventory


def _groups_by_replay_second(path: Path) -> Iterable[tuple[int, tuple[Q2FrameV1, ...]]]:
    group_second: int | None = None
    group: list[Q2FrameV1] = []
    for raw in _iter_raw(path):
        frame = Q2FrameV1.from_mapping(raw)
        replay_second = _floor_second(frame.logical_ts_ms)
        if group_second is not None and replay_second != group_second:
            yield group_second, tuple(group)
            group = []
        group_second = replay_second
        group.append(frame)
    if group_second is not None:
        yield group_second, tuple(group)


class _AllSymbolAuctionShadow:
    """Compose auction and optional opening facts under one Engine."""

    strategy_id = "q2frame-all-symbol-auction-shadow-v1"

    def __init__(self, symbols: tuple[str, ...], *, include_opening: bool = False) -> None:
        self._strategies = {
            symbol: AuctionShadowStrategy(
                scope_id=symbol,
                start_trigger_id="AUCTION_0920",
                middle_trigger_id="AUCTION_0924",
                end_trigger_id="AUCTION_0925",
                previous_segment_id=f"q2frame_{symbol}_0920_to_0924",
                current_segment_id=f"q2frame_{symbol}_0924_to_0925",
                price_fields=AUCTION_PRICE_FIELDS,
            )
            for symbol in symbols
        }
        self._opening_strategies = (
            {symbol: OpeningShadowStrategy(scope_id=symbol) for symbol in symbols}
            if include_opening
            else {}
        )

    @property
    def volume_semantics_unknown(self) -> bool:
        """Reflect the semantics configured on the actual auction strategies."""

        return any(
            _status_text(strategy.volume_semantics) == "UNKNOWN"
            for strategy in self._strategies.values()
        )

    def evaluate(self, snapshot: Any, bundle: FrozenDataBundle) -> StrategyResult:
        is_opening = snapshot.trigger_id == OPENING_TAG
        strategies = self._opening_strategies if is_opening else self._strategies
        if is_opening and not strategies:
            raise ValueError("OPENING_0932 was not enabled for this replay")
        child_results = {
            symbol: strategy.evaluate(snapshot, bundle)
            for symbol, strategy in strategies.items()
        }
        status_counts: Counter[str] = Counter()
        facts_by_symbol: dict[str, Any] = {}
        anchor_facts_by_symbol: dict[str, Any] = {}
        anchor_fact_status_counts: Counter[str] = Counter()
        opening_field_status_by_symbol: dict[str, Any] = {}
        opening_field_status_counts: dict[str, Counter[str]] = {}
        child_hashes: dict[str, str] = {}
        evidence_refs: set[str] = set()
        for symbol, result in child_results.items():
            child_hashes[symbol] = result.content_hash
            evidence_refs.update(result.evidence_refs)
            status = result.trace.get("fact_status", "UNKNOWN")
            status_counts[getattr(status, "value", str(status))] += 1
            anchor_fact = result.trace.get("auction_anchor_fact")
            if isinstance(anchor_fact, Mapping):
                anchor_facts_by_symbol[symbol] = anchor_fact
                anchor_fact_status_counts[str(anchor_fact.get("status", "UNKNOWN"))] += 1
            fact = (
                result.trace.get("opening_fact")
                if is_opening
                else result.trace.get("auction_fact_shadow")
            )
            if fact is not None:
                facts_by_symbol[symbol] = fact
            if is_opening:
                field_status = result.trace.get("opening_fact_field_status")
                if field_status is not None:
                    opening_field_status_by_symbol[symbol] = field_status
                    for field_name, field_status_value in field_status.items():
                        opening_field_status_counts.setdefault(
                            field_name, Counter()
                        )[str(field_status_value)] += 1

        trigger_id = snapshot.trigger_id
        trace: dict[str, Any] = {
            "state": "OBSERVE",
            "decision_status": "FACT_ONLY",
            "strategy_id": (
                "q2frame-all-symbol-opening-shadow-v1"
                if is_opening
                else self.strategy_id
            ),
            "trigger_id": trigger_id,
            "logical_time_ms": snapshot.logical_time_ms,
            "snapshot_hash": snapshot.content_hash,
            "fact_status_counts": dict(sorted(status_counts.items())),
            "fact_status_scope": (
                "change_pct_and_source_time"
                if is_opening
                else "adjacent_auction_comparison"
            ),
            "expected_q2frame_symbol_count": len(strategies),
            "observed_symbol_count": len(snapshot.symbol_states),
            "coverage": snapshot.coverage,
            "completeness": snapshot.completeness,
            "source_observation_metadata": snapshot.source_observation_metadata,
            "per_symbol_result_hashes": child_hashes,
        }
        if trigger_id == "AUCTION_0925":
            trace["facts_by_symbol"] = facts_by_symbol
            trace["facts_by_symbol_hash"] = semantic_hash(facts_by_symbol)
        if anchor_facts_by_symbol:
            trace["anchor_fact_status_scope"] = "standalone_current_anchor"
            trace["auction_anchor_facts_by_symbol"] = anchor_facts_by_symbol
            trace["auction_anchor_facts_by_symbol_hash"] = semantic_hash(
                anchor_facts_by_symbol
            )
            trace["auction_anchor_fact_status_counts"] = dict(
                sorted(anchor_fact_status_counts.items())
            )
        if is_opening:
            trace["fact_status_scope"] = "change_pct_and_source_time"
            trace["facts_by_symbol"] = facts_by_symbol
            trace["facts_by_symbol_hash"] = semantic_hash(facts_by_symbol)
            trace["opening_fact_field_status_by_symbol"] = (
                opening_field_status_by_symbol
            )
            trace["opening_fact_field_status_counts"] = {
                field_name: dict(sorted(counts.items()))
                for field_name, counts in sorted(opening_field_status_counts.items())
            }
            trace["opening_fact_field_status_hash"] = semantic_hash(
                opening_field_status_by_symbol
            )
            trace["stale_symbol_count"] = len(
                snapshot.source_observation_metadata.get("stale_symbols", ())
            )
            trace["missing_symbol_count"] = len(
                snapshot.source_observation_metadata.get("missing_symbols", ())
            )
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=trace,
            evidence_refs=tuple(sorted(evidence_refs)),
            content_hash=semantic_hash(trace),
        )


def _new_engine(
    trade_date: str,
    symbols: tuple[str, ...],
    first_ms: int,
    last_ms: int,
    *,
    include_opening: bool = False,
) -> tuple[DeterministicEngine, _AllSymbolAuctionShadow]:
    strategy = _AllSymbolAuctionShadow(symbols, include_opening=include_opening)
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("q2frame_replay", first_ms, last_ms + 1),)),
        strategy,
        session_id=trade_date,
        phase="REPLAY",
    )
    return engine, strategy


def _run_once(
    path: Path,
    *,
    trade_date: str,
    inventory: Mapping[str, Any],
    input_sha256: str,
    barrier_snapshots: Mapping[str, Q2FrameV1] | None = None,
    include_opening: bool = False,
    continue_through_input: bool = False,
    include_partial_reason_diagnostics: bool = False,
    plate_amount_context: Mapping[str, Any] | None = None,
    plate_auction_pressure_context: Mapping[str, Any] | None = None,
    plate_field_delta_context: Mapping[str, Any] | None = None,
    plate_price_reference_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    symbols = tuple(inventory["symbols"])
    policies = {tag: AuctionTimingPolicyV1.default(tag) for tag in AUCTION_TAGS}
    barriers = {
        tag: policies[tag].at(trade_date)["first_observable_ms"]
        for tag in AUCTION_TAGS
    }
    if include_opening:
        barriers[OPENING_TAG] = _opening_evaluation_ms(trade_date)
    ordered_barriers = tuple(sorted(barriers.items(), key=lambda item: (item[1], item[0])))
    replay_window_start_ms = int(
        inventory.get("replay_window_start_ms") or inventory["first_logical_ts_ms"]
    )
    first_ms = _floor_second(replay_window_start_ms)
    final_ms = ordered_barriers[-1][1]
    if continue_through_input:
        final_ms = max(final_ms, _floor_second(int(inventory["last_logical_ts_ms"])))
    initial_clock = datetime.fromtimestamp(first_ms / 1000, timezone.utc)
    clock = VirtualClock(initial_clock)
    freshness_policy = (
        FreshnessPolicy(
            stale_after_ms=OPENING_STALE_AFTER_MS,
            max_future_skew_ms=999,
        )
        if include_opening
        else FreshnessPolicy()
    )
    source = Q2FrameReplaySource(
        trade_date,
        symbols,
        clock,
        freshness_policy=freshness_policy,
        source_id="t1_v2_q2frame",
    )
    engine, auction_strategy = _new_engine(
        trade_date,
        symbols,
        first_ms,
        final_ms,
        include_opening=include_opening,
    )
    auction_timeline = AuctionTimeline(trade_date)
    anchor_evidence: dict[str, dict[str, Any]] = {}
    opening_evidence: dict[str, dict[str, Any]] = {}
    frame_count = update_count = 0
    latest_q2_update_by_symbol: dict[str, Mapping[str, Any]] = {}
    last_raw_frame_ms = None
    last_raw_update_ms = None
    first_excluded_frame: dict[str, Any] | None = None
    barrier_index = 0
    barrier_snapshot_update_count = 0
    barrier_snapshot_applied_by_tag: dict[str, dict[str, Any]] = {}
    barrier_snapshot_comparison_by_tag: dict[str, dict[str, Any]] = {}
    auxiliary_barrier_snapshots = {
        tag: snapshot
        for tag, snapshot in (barrier_snapshots or {}).items()
        if tag not in AUCTION_TAGS
    }

    # A bounded Q2Frame may begin after one or more auction observation times.
    # Do not move VirtualClock backwards to manufacture those earlier Engine
    # barriers. Preserve the missing evidence explicitly and continue with any
    # later anchor that is inside the replay window.
    while (
        barrier_index < len(ordered_barriers)
        and ordered_barriers[barrier_index][1] < first_ms
    ):
        tag, logical_ms = ordered_barriers[barrier_index]
        not_observed = {
            "status": "NOT_OBSERVED_IN_REPLAY_WINDOW",
            "reason_code": "FIRST_OBSERVABLE_PRECEDES_REPLAY_WINDOW",
            "first_observable_ms": logical_ms,
            "first_observable_local": _local_datetime(logical_ms).isoformat(),
            "replay_window_start_ms": replay_window_start_ms,
            "replay_window_start_local": _local_datetime(first_ms).isoformat(),
            "expected_q2frame_symbol_count": len(symbols),
            "universe_basis": inventory["universe_basis"],
            "fact_status_counts": {},
            "fact_status_scope": "NO_FACTS_NOT_OBSERVED_IN_REPLAY_WINDOW",
            "historical_available_at": "UNKNOWN_NOT_INFERRED",
            "decision_status": "FACT_ONLY",
        }
        if tag == OPENING_TAG:
            opening_evidence[tag] = not_observed
        else:
            anchor_evidence[tag] = not_observed
        barrier_snapshot_applied_by_tag[tag] = {
            "status": "NOT_OBSERVED_IN_REPLAY_WINDOW",
            "snapshot_equivalence": "UNPROVEN",
        }
        barrier_index += 1

    def capture_barrier(tag: str, logical_ms: int, result: Any) -> None:
        snapshot = result.snapshots[-1]
        strategy_result = result.strategy_results[-1]
        base = {
            "first_observable_ms": logical_ms,
            "first_observable_local": _local_datetime(logical_ms).isoformat(),
            "engine_snapshot_hash": snapshot.content_hash,
            "engine_revision": snapshot.market_state_revision,
            "engine_completeness": snapshot.completeness,
            "coverage": snapshot.coverage,
            "observed_symbol_count": len(snapshot.symbol_states),
            "expected_q2frame_symbol_count": len(symbols),
            "oldest_source_time_ms": snapshot.source_observation_metadata.get(
                "oldest_source_time_ms"
            ),
            "newest_source_time_ms": snapshot.source_observation_metadata.get(
                "newest_source_time_ms"
            ),
            "input_frames_included": frame_count,
            "input_updates_included": update_count,
            "last_raw_frame_time_ms": last_raw_frame_ms,
            "last_raw_update_time_ms": last_raw_update_ms,
            "strategy_result_hash": strategy_result.content_hash,
            "fact_status_counts": strategy_result.trace.get("fact_status_counts", {}),
            "fact_status_scope": strategy_result.trace.get("fact_status_scope"),
            "anchor_fact_status_scope": strategy_result.trace.get(
                "anchor_fact_status_scope"
            ),
            "facts_by_symbol_hash": strategy_result.trace.get("facts_by_symbol_hash"),
            "facts_by_symbol": strategy_result.trace.get("facts_by_symbol", {}),
            "auction_anchor_facts_by_symbol_hash": strategy_result.trace.get(
                "auction_anchor_facts_by_symbol_hash"
            ),
            "auction_anchor_fact_status_counts": strategy_result.trace.get(
                "auction_anchor_fact_status_counts", {}
            ),
            "auction_anchor_facts_by_symbol": strategy_result.trace.get(
                "auction_anchor_facts_by_symbol", {}
            ),
            "processed_signals": result.processed_signals,
            "reducer_revision": engine._reducer.state.revision,
            "virtual_clock_ms": int(clock.now_utc().timestamp() * 1000),
        }
        source_anomaly_hashes = tuple(
            snapshot.source_observation_metadata.get("source_anomaly_hashes", ())
        )
        if source_anomaly_hashes:
            base["source_anomaly_count"] = len(source_anomaly_hashes)
            base["source_anomaly_hashes"] = source_anomaly_hashes
        if tag == "0925" and include_partial_reason_diagnostics:
            auction_anchor_facts_by_tag = {
                "0920": anchor_evidence.get("0920", {}).get(
                    "auction_anchor_facts_by_symbol", {}
                ),
                "0924": anchor_evidence.get("0924", {}).get(
                    "auction_anchor_facts_by_symbol", {}
                ),
                "0925": base.get("auction_anchor_facts_by_symbol", {}),
            }
            base["partial_reason_diagnostics"] = _build_partial_reason_diagnostics(
                base.get("facts_by_symbol", {}),
                auction_anchor_facts_by_tag=auction_anchor_facts_by_tag,
                volume_semantics_unknown=auction_strategy.volume_semantics_unknown,
            )
        if tag == OPENING_TAG:
            expected_symbols = tuple(sorted(symbols))
            observed_symbols = tuple(
                sorted(set(snapshot.symbol_states).intersection(expected_symbols))
            )
            missing_symbols = tuple(
                symbol
                for symbol in expected_symbols
                if symbol not in set(observed_symbols)
            )
            frame_completeness = (
                "EMPTY"
                if not observed_symbols
                else "PARTIAL"
                if missing_symbols
                else "COMPLETE"
            )
            source_metadata = snapshot.source_observation_metadata
            cohort_state = CrossSectionStateV1(
                trade_date=trade_date,
                frame_no=max(0, frame_count - 1),
                logical_ts_ms=logical_ms,
                expected_symbols=expected_symbols,
                updated_symbols=observed_symbols,
                missing_symbols=missing_symbols,
                symbol_states={
                    symbol: snapshot.symbol_states[symbol]
                    for symbol in observed_symbols
                },
                frame_completeness=frame_completeness,
                coverage=(
                    len(observed_symbols) / float(len(expected_symbols))
                    if expected_symbols
                    else 0.0
                ),
                source_time_min_ms=source_metadata.get("oldest_source_time_ms"),
                source_time_max_ms=source_metadata.get("newest_source_time_ms"),
            )
            cross_section = build_cross_section_facts(
                cohort_state,
                scope="OBSERVED_COHORT",
                source_layers=("t1_v2_q2frame_event_time_replay",),
            )
            cross_section_evidence = {
                "contract": "CrossSectionFactsV1",
                "frame_no": cross_section.frame_no,
                "logical_ts_ms": cross_section.logical_ts_ms,
                "scope": cross_section.scope,
                "scope_authority": "Q2FRAME_INPUT_COHORT_ONLY_NOT_FULL_MARKET",
                "expected_count": cross_section.expected_count,
                "observed_count": cross_section.observed_count,
                "missing_count": cross_section.missing_count,
                "coverage": cross_section.coverage,
                "field_denominators": dict(cross_section.field_denominators),
                "market_breadth": dict(cross_section.market_breadth),
                "source_layers": list(cross_section.source_layers),
                "stale_symbol_count": len(source_metadata.get("stale_symbols", ())),
                "breadth_includes_stale_observed_quotes": True,
                "content_hash": cross_section.content_hash,
                "fact_only": cross_section.fact_only,
            }
            base.update(
                {
                    "contract": "OpeningFactV1",
                    "cross_section_facts": cross_section_evidence,
                    "fact_status_scope": strategy_result.trace.get(
                        "fact_status_scope"
                    ),
                    "evaluation_time_ms": logical_ms,
                    "expected_symbol_count": len(symbols),
                    "stale_symbol_count": strategy_result.trace.get(
                        "stale_symbol_count", 0
                    ),
                    "missing_symbol_count": strategy_result.trace.get(
                        "missing_symbol_count", 0
                    ),
                    "opening_fact_field_status_by_symbol": strategy_result.trace.get(
                        "opening_fact_field_status_by_symbol", {}
                    ),
                    "opening_fact_field_status_counts": strategy_result.trace.get(
                        "opening_fact_field_status_counts", {}
                    ),
                    "opening_fact_field_status_hash": strategy_result.trace.get(
                        "opening_fact_field_status_hash"
                    ),
                    "amount_2m_summary": build_opening_amount_summary(
                        strategy_result.trace.get("facts_by_symbol", {}),
                        expected_symbols=expected_symbols,
                        scope="OBSERVED_COHORT",
                    ),
                    "limit_state_summary": build_opening_limit_state_summary(
                        strategy_result.trace.get("facts_by_symbol", {}),
                        expected_symbols=expected_symbols,
                        scope="OBSERVED_COHORT",
                    ),
                    "freshness_policy": {
                        "stale_after_ms": OPENING_STALE_AFTER_MS,
                        "max_future_skew_ms": 999,
                    },
                    "universe_basis": inventory["universe_basis"],
                    "availability_status": "UNKNOWN_NOT_INFERRED",
                }
            )
            q2_0925_evidence = anchor_evidence.get("0925", {})
            opening_rows_by_symbol: dict[str, dict[str, Any]] = {}
            for symbol, raw_update in latest_q2_update_by_symbol.items():
                quote = normalize_q2(symbol, raw_update)
                opening_rows_by_symbol[symbol] = {
                    "symbol": symbol,
                    "timestamp_ms": quote.source_record_time_ms,
                    "price_milli": quote.price_milli,
                    "previous_close_milli": quote.pre_close_milli,
                }
            transition_summary = build_opening_transition_summary(
                q2_0925_evidence.get("auction_anchor_facts_by_symbol", {}),
                opening_rows_by_symbol,
                expected_symbols=expected_symbols,
                scope="OBSERVED_COHORT",
            )
            base["opening_transition_summary"] = {
                "contract": "OpeningTransitionSummaryV1",
                "baseline": {
                    "tag": "0925",
                    "business_anchor_ms": q2_0925_evidence.get(
                        "auction_revision", {}
                    ).get("business_anchor_ms"),
                    "evaluation_time_ms": q2_0925_evidence.get(
                        "auction_revision", {}
                    ).get("evaluation_time_ms"),
                    "freeze_time_ms": q2_0925_evidence.get(
                        "auction_revision", {}
                    ).get("freeze_time_ms"),
                    "source_layer": "t1_v2_q2frame_event_time_replay",
                },
                "opening_evaluation_time_ms": logical_ms,
                "opening_source_layer": "t1_v2_q2frame_event_time_replay",
                "stale_opening_symbol_count": base.get("stale_symbol_count", 0),
                "historical_available_at": "UNKNOWN_NOT_INFERRED",
                "rabbit_arrival_order": "UNKNOWN_NOT_INFERRED",
                "decision_status": "FACT_ONLY",
                "facts": transition_summary,
            }
            if plate_amount_context is not None:
                base["plate_amount_context"] = {
                    "contract": plate_amount_context["contract"],
                    "trade_date": plate_amount_context["trade_date"],
                    "content_hash": plate_amount_context["content_hash"],
                    "source_provenance": plate_amount_context["source_provenance"],
                    "selected_plates": plate_amount_context["selected_plates"],
                }
                base["plate_amount_summary"] = build_opening_plate_amount_summary(
                    strategy_result.trace.get("facts_by_symbol", {}),
                    mapped_symbols_by_plate=plate_amount_context[
                        "mapped_symbols_by_plate"
                    ],
                    auction_symbols_by_plate=plate_amount_context[
                        "auction_symbols_by_plate"
                    ],
                    auction_top1_amount_ratio_by_plate=plate_amount_context[
                        "auction_top1_amount_ratio_by_plate"
                    ],
                    selected_plates=plate_amount_context["selected_plates"],
                )
                base["plate_price_summary"] = build_opening_plate_price_summary(
                    strategy_result.trace.get("facts_by_symbol", {}),
                    mapped_symbols_by_plate=plate_amount_context[
                        "mapped_symbols_by_plate"
                    ],
                    auction_symbols_by_plate=plate_amount_context[
                        "auction_symbols_by_plate"
                    ],
                    selected_plates=plate_amount_context["selected_plates"],
                    auction_price_reference_by_plate=(
                        plate_price_reference_context["auction_price_stats_by_plate"]
                        if plate_price_reference_context is not None
                        else None
                    ),
                )
                if plate_price_reference_context is not None:
                    base["plate_price_reference_context"] = {
                        "contract": plate_price_reference_context["contract"],
                        "trade_date": plate_price_reference_context["trade_date"],
                        "content_hash": plate_price_reference_context["content_hash"],
                        "source_provenance": plate_price_reference_context[
                            "source_provenance"
                        ],
                        "selected_plates": plate_price_reference_context[
                            "selected_plates"
                        ],
                    }
            if plate_auction_pressure_context is not None:
                base["plate_auction_pressure_context"] = {
                    "contract": plate_auction_pressure_context["contract"],
                    "trade_date": plate_auction_pressure_context["trade_date"],
                    "content_hash": plate_auction_pressure_context["content_hash"],
                    "source_provenance": plate_auction_pressure_context[
                        "source_provenance"
                    ],
                    "selected_plates": plate_auction_pressure_context[
                        "selected_plates"
                    ],
                    "historical_available_at": "UNKNOWN_NOT_INFERRED",
                    "source_layer": "CAPTURED_TD_AUCTION_0924_TO_0925_SIDECAR",
                }
                base["plate_auction_pressure_summary"] = (
                    plate_auction_pressure_context["auction_pressure_summary"]
                )
            if plate_field_delta_context is not None:
                base["plate_field_delta_context"] = {
                    "contract": plate_field_delta_context["contract"],
                    "trade_date": plate_field_delta_context["trade_date"],
                    "content_hash": plate_field_delta_context["content_hash"],
                    "source_provenance": plate_field_delta_context[
                        "source_provenance"
                    ],
                    "selected_plates": plate_field_delta_context["selected_plates"],
                    "historical_available_at": "UNKNOWN_NOT_INFERRED",
                    "source_layer": "CAPTURED_TD_AUCTION_0924_TO_0925_FIELD_FACTS",
                }
                base["plate_field_delta_summary"] = plate_field_delta_context[
                    "field_delta_summary"
                ]
            opening_evidence[tag] = base
        else:
            base["auction_revision"] = auction_revision_summary(
                tag,
                snapshot,
                logical_ms,
            )
            if tag == "0925":
                q2_quotes = {
                    symbol: normalize_q2(symbol, update)
                    for symbol, update in latest_q2_update_by_symbol.items()
                }
                q2_summary = derive_q2_auction_summary(
                    q2_quotes,
                    trade_date=trade_date,
                    source_id=f"q2frame-sha256:{input_sha256}",
                    source_table="Q2FrameV1:event_time_replay",
                    # The frozen artifact's observed symbols are not an
                    # authoritative full-market universe at this barrier.
                    expected_symbols=None,
                    observation_time_ms=snapshot.source_observation_metadata.get(
                        "observation_time_ms"
                    ),
                    input_content_hash=input_sha256,
                    evidence_refs=(input_sha256,),
                )
                base["q2_auction_summary"] = q2_summary.as_mapping()
            anchor_evidence[tag] = base

    def auction_revision_summary(
        tag: str,
        snapshot: Any,
        evaluation_time_ms: int,
    ) -> dict[str, Any]:
        rows = {
            symbol: {
                **dict(values),
                "source_time_ms": values.get("source_record_time_ms"),
            }
            for symbol, values in snapshot.symbol_states.items()
        }
        revision = auction_timeline.observe(
            tag,
            rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=symbols,
            observed_at_ms=snapshot.source_observation_metadata.get(
                "observation_time_ms"
            ),
            source_layers=("t1_v2_q2frame_event_time_replay",),
        )
        analysis_bundle = auction_timeline.build_analysis_bundle(tag)
        recovery_plan = analysis_bundle["recovery_plan"]
        recovery_plan_summary = (
            None
            if recovery_plan is None
            else {
                "contract": "RecoveryPlanV1",
                "plan_id": recovery_plan.plan_id,
                "requested_symbols": list(recovery_plan.requested_symbols),
                "missing_fields": list(recovery_plan.missing_fields),
                "current_revision": recovery_plan.current_revision,
                "requested_at_ms": recovery_plan.requested_at_ms,
                "preferred_sources": list(recovery_plan.preferred_sources),
                "idempotency_key": recovery_plan.idempotency_key,
                "soft_deadline_ms": recovery_plan.soft_deadline_ms,
                "content_hash": recovery_plan.content_hash,
                "recovery_state": recovery_plan.recovery_state,
            }
        )
        return {
            "contract": "AuctionAnchorRevisionV3",
            "revision": revision.revision,
            "state": revision.state,
            "business_anchor_ms": revision.business_anchor_ms,
            "first_observable_ms": revision.first_observable_ms,
            "preferred_finalize_ms": revision.preferred_finalize_ms,
            "soft_deadline_ms": revision.soft_deadline_ms,
            "expected_symbol_count": len(revision.expected_symbols),
            "prior_deltas": analysis_bundle["prior_deltas"],
            "source_observed_symbol_count": len(revision.source_observed_symbols),
            "source_missing_symbol_count": len(revision.source_missing_symbols),
            "source_coverage": revision.source_coverage,
            "anchor_available_symbol_count": len(revision.available_anchor_symbols),
            "missing_anchor_symbol_count": len(revision.missing_anchor_symbols),
            "anchor_coverage": revision.anchor_coverage,
            # Compatibility field retains the V1 name, now explicitly scoped
            # to required anchor-field coverage by the V2 contract.
            "coverage": revision.anchor_coverage,
            "recovery_required": analysis_bundle["recovery_required"],
            "recovery_plan": recovery_plan_summary,
            "recovery_execution": (
                "NOT_RUN_BY_CORE"
                if recovery_plan is not None
                else "NOT_REQUIRED"
            ),
            "source_layers": revision.source_layers,
            "observed_at_ms": revision.observed_at_ms,
            "evaluation_time_ms": revision.evaluation_time_ms,
            "freeze_time_ms": revision.freeze_time_ms,
            "source_time_min_ms": revision.source_time_min_ms,
            "source_time_max_ms": revision.source_time_max_ms,
            "late_execution": revision.late_execution,
            "supersedes_revision": revision.supersedes_revision,
            "recovery_state": revision.recovery_state,
            "observations_hash": revision.observations_hash,
            "content_hash": revision.content_hash,
            "evidence_hash": revision.evidence_hash,
        }

    def enqueue_barrier_snapshot(tag: str, logical_ms: int) -> None:
        nonlocal barrier_snapshot_update_count, last_raw_update_ms
        snapshot = (barrier_snapshots or {}).get(tag)
        if snapshot is None:
            barrier_snapshot_applied_by_tag[tag] = {
                "status": "NOT_PROVIDED",
                "snapshot_equivalence": "UNPROVEN",
            }
            return

        window_start_ms = int(
            inventory.get("replay_window_start_ms")
            or inventory["first_logical_ts_ms"]
        )
        window_end_ms = int(
            inventory.get("replay_window_end_ms")
            or inventory["last_logical_ts_ms"]
        )
        # Source slice bounds are half-open: a snapshot at the final right
        # edge belongs to the next replay window, not this one.
        if not window_start_ms <= logical_ms < window_end_ms:
            barrier_snapshot_applied_by_tag[tag] = {
                "status": "OUTSIDE_REPLAY_WINDOW",
                "snapshot_equivalence": "UNPROVEN",
                "replay_window_start_ms": window_start_ms,
                "replay_window_end_ms": window_end_ms,
            }
            return
        if _floor_second(source.last_logical_ts_ms) > logical_ms:
            raise ValueError(
                "barrier Q2Frame snapshot is older than already-consumed replay data"
            )

        # The sidecar's sequence is local to its own file. Re-number this
        # event in the merged in-memory replay stream so it cannot collide
        # with the regular per-slice Q2Frame sequence.
        source_logical_ms = max(snapshot.logical_ts_ms, source.last_logical_ts_ms)
        replay_snapshot = Q2FrameV1(
            seq_no=source.last_seq_no + 1,
            logical_ts_ms=source_logical_ms,
            q2_updates=snapshot.q2_updates,
            phase=snapshot.phase,
        )
        raw_signal = source.signal_for(
            replay_snapshot,
            signal_prefix=f"t1-v2-barrier-q2frame-{tag}",
        )
        replay_signal = EngineSignal(
            signal_id=raw_signal.signal_id,
            logical_time_ms=logical_ms,
            signal_seq=raw_signal.signal_seq,
            signal_kind=raw_signal.signal_kind,
            payload=raw_signal.payload,
        )
        source.advance_before_consume(replay_signal)
        engine.submit(replay_signal)
        barrier_snapshot_update_count += len(snapshot.q2_updates)
        for update in snapshot.q2_updates:
            symbol = str(update.get("symbol", "")).strip()
            if symbol:
                latest_q2_update_by_symbol[symbol] = update
            source_ms = update.get("ts")
            if isinstance(source_ms, int) and not isinstance(source_ms, bool):
                last_raw_update_ms = (
                    source_ms
                    if last_raw_update_ms is None
                    else max(last_raw_update_ms, source_ms)
                )
        barrier_snapshot_applied_by_tag[tag] = {
            "status": "APPLIED",
            "record_kind": "barrier_snapshot",
            "logical_ts_ms": snapshot.logical_ts_ms,
            "update_count": len(snapshot.q2_updates),
            "replay_seq_no": replay_snapshot.seq_no,
        }

    def fire_barrier(tag: str, logical_ms: int) -> None:
        nonlocal barrier_index
        clock.advance_to(datetime.fromtimestamp(logical_ms / 1000, timezone.utc))
        enqueue_barrier_snapshot(tag, logical_ms)
        trigger_id = OPENING_TAG if tag == OPENING_TAG else f"AUCTION_{tag}"
        engine.submit(
            EngineSignal(
                signal_id=f"q2frame-timer:{tag}",
                logical_time_ms=logical_ms,
                signal_seq=1_000_000_000 + barrier_index,
                signal_kind=SignalKind.TIMER,
                payload={"trigger_id": trigger_id},
            )
        )
        result = engine.run_until_empty()
        capture_barrier(tag, logical_ms, result)
        barrier_index += 1

    for replay_second, group in _groups_by_replay_second(path):
        if replay_second > final_ms:
            first = group[0]
            first_excluded_frame = {
                "seq_no": first.seq_no,
                "raw_logical_ts_ms": first.logical_ts_ms,
                "raw_logical_time_local": _local_datetime(first.logical_ts_ms).isoformat(),
                "replay_second_ms": replay_second,
                "update_count": len(first.q2_updates),
                "excluded_reason": (
                    "AFTER_OPENING_0932_EVALUATION_SECOND"
                    if include_opening
                    else "AFTER_0925_FIRST_OBSERVABLE_SECOND"
                ),
            }
            break

        while (
            barrier_index < len(ordered_barriers)
            and ordered_barriers[barrier_index][1] < replay_second
        ):
            tag, logical_ms = ordered_barriers[barrier_index]
            fire_barrier(tag, logical_ms)

        for frame in group:
            # Q2Frame seq_no identifies frames in its source artifact. Barrier
            # snapshots are additional events in the merged replay stream, so
            # assign an internal continuous sequence without rewriting source
            # evidence or depending on the artifact's local seq_no.
            replay_frame = Q2FrameV1(
                seq_no=source.last_seq_no + 1,
                logical_ts_ms=frame.logical_ts_ms,
                q2_updates=frame.q2_updates,
                phase=frame.phase,
            )
            raw_signal = source.signal_for(
                replay_frame,
                signal_prefix=f"t1-v2-q2frame-source-{frame.seq_no}",
            )
            replay_signal = EngineSignal(
                signal_id=raw_signal.signal_id,
                logical_time_ms=replay_second,
                signal_seq=raw_signal.signal_seq,
                signal_kind=raw_signal.signal_kind,
                payload=raw_signal.payload,
            )
            source.advance_before_consume(replay_signal)
            engine.submit(replay_signal)
            frame_count += 1
            update_count += len(frame.q2_updates)
            for update in frame.q2_updates:
                symbol = str(update.get("symbol", "")).strip()
                if symbol:
                    latest_q2_update_by_symbol[symbol] = update
            last_raw_frame_ms = frame.logical_ts_ms
            update_times = [
                int(update["ts"])
                for update in frame.q2_updates
                if isinstance(update.get("ts"), int)
                and not isinstance(update.get("ts"), bool)
            ]
            if update_times:
                frame_last_update_ms = max(update_times)
                last_raw_update_ms = (
                    frame_last_update_ms
                    if last_raw_update_ms is None
                    else max(last_raw_update_ms, frame_last_update_ms)
                )

        matching_barrier = (
            ordered_barriers[barrier_index]
            if barrier_index < len(ordered_barriers)
            else None
        )
        if matching_barrier is not None and matching_barrier[1] == replay_second:
            tag, logical_ms = matching_barrier
            # Engine signal priority applies every Q2Frame update in this whole
            # second before the same-time auction timer.
            clock.advance_to(datetime.fromtimestamp(logical_ms / 1000, timezone.utc))
            enqueue_barrier_snapshot(tag, logical_ms)
            trigger_id = OPENING_TAG if tag == OPENING_TAG else f"AUCTION_{tag}"
            engine.submit(
                EngineSignal(
                    signal_id=f"q2frame-timer:{tag}",
                    logical_time_ms=logical_ms,
                    signal_seq=1_000_000_000 + barrier_index,
                    signal_kind=SignalKind.TIMER,
                    payload={"trigger_id": trigger_id},
                )
            )
        result = engine.run_until_empty()
        if matching_barrier is not None and matching_barrier[1] == replay_second:
            tag, logical_ms = matching_barrier
            capture_barrier(tag, logical_ms, result)
            barrier_index += 1
        for tag, sidecar in auxiliary_barrier_snapshots.items():
            checkpoint_ms = _floor_second(sidecar.logical_ts_ms)
            if checkpoint_ms == replay_second and tag not in barrier_snapshot_comparison_by_tag:
                barrier_snapshot_comparison_by_tag[tag] = _compare_core_q2_checkpoint(
                    tag=tag,
                    checkpoint_ms=checkpoint_ms,
                    engine_state=engine._reducer.state.symbol_states,
                    sidecar=sidecar,
                )

    while barrier_index < len(ordered_barriers):
        tag, logical_ms = ordered_barriers[barrier_index]
        fire_barrier(tag, logical_ms)

    window_start_second = _floor_second(replay_window_start_ms)
    window_end_second = _floor_second(int(inventory["last_logical_ts_ms"]))
    for tag, sidecar in auxiliary_barrier_snapshots.items():
        if tag in barrier_snapshot_comparison_by_tag:
            continue
        checkpoint_ms = _floor_second(sidecar.logical_ts_ms)
        status = (
            "OUTSIDE_REPLAY_WINDOW"
            if checkpoint_ms < window_start_second or checkpoint_ms > window_end_second
            else "NOT_OBSERVED_IN_REPLAY_STREAM"
        )
        barrier_snapshot_comparison_by_tag[tag] = {
            "contract": "CoreQ2CheckpointComparisonV1",
            "tag": tag,
            "status": status,
            "checkpoint_time_ms": checkpoint_ms,
            "sidecar_time_ms": sidecar.logical_ts_ms,
            "capture_policy": "PASSIVE_AFTER_EVENT_SECOND_NO_ENGINE_SIGNAL",
            "sidecar_injected_into_engine": False,
            "comparison_scope": (
                "Q2Quote.to_mapping excluding only observer-time field_errors"
            ),
            "field_errors_policy": (
                "source parse/validation errors compared; stale/future_ts/trade_date "
                "reported separately because they depend on Core observation policy"
            ),
            "core_observer_time_error_counts": {},
            "sidecar_observer_time_error_counts": {},
            "core_symbol_count": 0,
            "sidecar_symbol_count": len({str(row["symbol"]) for row in sidecar.q2_updates}),
            "duplicate_sidecar_symbol_count": len(sidecar.q2_updates) - len(
                {str(row["symbol"]) for row in sidecar.q2_updates}
            ),
            "value_mismatch_count": None,
            "mismatched_symbol_count": None,
            "field_mismatch_counts": {},
            "mismatched_symbols_sample": [],
            "core_value_hash": None,
            "sidecar_value_hash": None,
        }

    if tuple(anchor_evidence) != AUCTION_TAGS:
        raise RuntimeError("not all three auction barriers produced Engine snapshots")
    if include_opening and tuple(opening_evidence) != (OPENING_TAG,):
        raise RuntimeError("OPENING_0932 did not produce an Engine snapshot")
    final_state = engine._reducer.state
    result = {
        "anchor_evidence": anchor_evidence,
        "opening_evidence": opening_evidence,
        "first_excluded_frame": first_excluded_frame,
        "input_frames_processed": frame_count,
        "input_updates_processed": update_count,
        "barrier_snapshot_update_count": barrier_snapshot_update_count,
        "barrier_snapshot_applied_by_tag": barrier_snapshot_applied_by_tag,
        "barrier_snapshot_comparison_by_tag": barrier_snapshot_comparison_by_tag,
        "processed_signals": engine.run_until_empty().processed_signals,
        "reducer_revision": final_state.revision,
        "final_state_hash": canonical_hash(
            {
                "revision": final_state.revision,
                "logical_time_ms": final_state.logical_time_ms,
                "symbols": final_state.symbol_states,
                "source": final_state.source_observation_metadata,
                "coverage": final_state.coverage,
                "completeness": final_state.completeness,
            }
        ),
        "engine_instances": 1,
        "virtual_clock_ms": int(clock.now_utc().timestamp() * 1000),
    }
    if continue_through_input:
        result["replay_termination_policy"] = "THROUGH_FINAL_INPUT_FRAME"
        result["replay_end_ms"] = final_ms
    return result


def run_q2frame_auction_engine_shadow(
    *,
    q2frame_path: Path,
    trade_date: str,
    expected_sha256: str | None = None,
    barrier_q2frame_path: Path | None = None,
    expected_barrier_q2frame_sha256: str | None = None,
    include_opening: bool = False,
    continue_through_input: bool = False,
    include_partial_reason_diagnostics: bool = False,
    plate_amount_context: Mapping[str, Any] | None = None,
    plate_auction_pressure_context: Mapping[str, Any] | None = None,
    plate_field_delta_context: Mapping[str, Any] | None = None,
    plate_price_reference_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a pinned Q2Frame auction replay, optionally through opening, twice."""

    if plate_amount_context is not None:
        if not include_opening:
            raise ValueError("plate amount context requires include_opening=True")
        plate_amount_context = validate_opening_plate_amount_context(
            plate_amount_context,
            trade_date=trade_date,
        )
    if plate_price_reference_context is not None:
        if not include_opening:
            raise ValueError("plate price reference context requires include_opening=True")
        if plate_amount_context is None:
            raise ValueError("plate price reference context requires plate amount context")
        plate_price_reference_context = validate_opening_plate_price_reference_context(
            plate_price_reference_context,
            trade_date=trade_date,
            selected_plates=plate_amount_context["selected_plates"],
        )
    if plate_auction_pressure_context is not None:
        if not include_opening:
            raise ValueError("plate auction pressure context requires include_opening=True")
        plate_auction_pressure_context = validate_opening_plate_auction_pressure_context(
            plate_auction_pressure_context,
            trade_date=trade_date,
            selected_plates=(
                plate_amount_context["selected_plates"]
                if plate_amount_context is not None
                else None
            ),
        )
    if plate_field_delta_context is not None:
        if not include_opening:
            raise ValueError("plate field delta context requires include_opening=True")
        plate_field_delta_context = validate_opening_plate_field_delta_context(
            plate_field_delta_context,
            trade_date=trade_date,
            selected_plates=(
                plate_amount_context["selected_plates"]
                if plate_amount_context is not None
                else None
            ),
        )

    initial_sha = _file_sha256(q2frame_path)
    if expected_sha256 is not None and initial_sha != expected_sha256:
        raise ValueError("Q2Frame SHA-256 does not match the pinned artifact")
    barrier_initial_sha = (
        _file_sha256(barrier_q2frame_path)
        if barrier_q2frame_path is not None
        else None
    )
    if (
        expected_barrier_q2frame_sha256 is not None
        and barrier_initial_sha != expected_barrier_q2frame_sha256
    ):
        raise ValueError("barrier Q2Frame SHA-256 does not match the pinned artifact")
    barrier_snapshots = _read_barrier_q2frames(
        barrier_q2frame_path,
        trade_date=trade_date,
    )
    inventory = _inventory(
        q2frame_path,
        trade_date,
        stale_after_ms=(OPENING_STALE_AFTER_MS if include_opening else None),
        include_opening=include_opening,
        continue_through_input=continue_through_input,
    )
    frame_gap_diagnostics = inventory["_frame_gap_diagnostics"]
    if frame_gap_diagnostics["continuation_status"] == "STOP_REQUIRED":
        blocking_gap = next(
            segment
            for segment in frame_gap_diagnostics["gap_segments"]
            if segment["action"] == "STOP_REQUIRED"
        )
        raise ValueError(
            "Q2Frame gap exceeds the configured freshness stop boundary: "
            f"gap_ms={blocking_gap['gap_ms']}, "
            f"replay_scope_overlap_ms={blocking_gap['replay_scope_overlap_ms']}, "
            f"freshness_exposure_ms={blocking_gap['freshness_exposure_ms']}, "
            f"stale_after_ms={frame_gap_diagnostics['freshness_stop_threshold_ms']}, "
            f"after_seq_no={blocking_gap['after_seq_no']}, "
            f"before_seq_no={blocking_gap['before_seq_no']}, "
            f"barriers_crossed={blocking_gap['barriers_crossed']}"
        )
    after_inventory_sha = _file_sha256(q2frame_path)
    barrier_after_load_sha = (
        _file_sha256(barrier_q2frame_path)
        if barrier_q2frame_path is not None
        else None
    )
    if barrier_initial_sha != barrier_after_load_sha:
        raise RuntimeError("barrier Q2Frame changed while it was being inventoried")
    first = _run_once(
        q2frame_path,
        trade_date=trade_date,
        inventory=inventory,
        input_sha256=initial_sha,
        barrier_snapshots=barrier_snapshots,
        include_opening=include_opening,
        continue_through_input=continue_through_input,
        include_partial_reason_diagnostics=include_partial_reason_diagnostics,
        plate_amount_context=plate_amount_context,
        plate_auction_pressure_context=plate_auction_pressure_context,
        plate_field_delta_context=plate_field_delta_context,
        plate_price_reference_context=plate_price_reference_context,
    )
    between_runs_sha = _file_sha256(q2frame_path)
    barrier_between_runs_sha = (
        _file_sha256(barrier_q2frame_path)
        if barrier_q2frame_path is not None
        else None
    )
    repeat = _run_once(
        q2frame_path,
        trade_date=trade_date,
        inventory=inventory,
        input_sha256=initial_sha,
        barrier_snapshots=barrier_snapshots,
        include_opening=include_opening,
        continue_through_input=continue_through_input,
        include_partial_reason_diagnostics=include_partial_reason_diagnostics,
        plate_amount_context=plate_amount_context,
        plate_auction_pressure_context=plate_auction_pressure_context,
        plate_field_delta_context=plate_field_delta_context,
        plate_price_reference_context=plate_price_reference_context,
    )
    final_sha = _file_sha256(q2frame_path)
    barrier_final_sha = (
        _file_sha256(barrier_q2frame_path)
        if barrier_q2frame_path is not None
        else None
    )
    input_stable = len(
        {initial_sha, after_inventory_sha, between_runs_sha, final_sha}
    ) == 1
    compared_fields = (
        "anchor_evidence",
        "opening_evidence",
        "input_frames_processed",
        "input_updates_processed",
        "barrier_snapshot_update_count",
        "barrier_snapshot_applied_by_tag",
        "barrier_snapshot_comparison_by_tag",
        "processed_signals",
        "reducer_revision",
        "final_state_hash",
        "virtual_clock_ms",
    )
    determinism = {
        field: first[field] == repeat[field]
        for field in compared_fields
    }
    if continue_through_input:
        determinism["replay_termination_policy"] = (
            first["replay_termination_policy"] == repeat["replay_termination_policy"]
        )
        determinism["replay_end_ms"] = first["replay_end_ms"] == repeat["replay_end_ms"]
    determinism["input_sha256_stable"] = input_stable
    barrier_input_stable = (
        None
        if barrier_q2frame_path is None
        else len(
            {
                barrier_initial_sha,
                barrier_after_load_sha,
                barrier_between_runs_sha,
                barrier_final_sha,
            }
        )
        == 1
    )
    if barrier_q2frame_path is not None:
        determinism["barrier_input_sha256_stable"] = barrier_input_stable is True
    deterministic = all(determinism.values())
    if plate_field_delta_context is not None:
        contract_version = "Task008Q2FrameSessionEngineShadowV16"
    elif any(tag not in AUCTION_TAGS for tag in barrier_snapshots):
        contract_version = "Task008Q2FrameSessionEngineShadowV15"
    elif continue_through_input:
        contract_version = "Task008Q2FrameSessionEngineShadowV14"
    elif barrier_q2frame_path is not None:
        contract_version = "Task008Q2FrameSessionEngineShadowV13"
    elif plate_auction_pressure_context is not None:
        contract_version = "Task008Q2FrameSessionEngineShadowV12"
    elif plate_price_reference_context is not None:
        contract_version = "Task008Q2FrameSessionEngineShadowV11"
    elif plate_amount_context is not None:
        contract_version = "Task008Q2FrameSessionEngineShadowV10"
    elif include_opening:
        contract_version = "Task008Q2FrameSessionEngineShadowV7"
    else:
        contract_version = "Task008Q2FrameAuctionEngineShadowV5"
    return {
        "contract_version": contract_version,
        **(
            {"partial_reason_diagnostics_contract": PARTIAL_REASON_DIAGNOSTICS_CONTRACT}
            if include_partial_reason_diagnostics
            else {}
        ),
        "trade_date": trade_date,
        "run_mode": "REAL_T1V2_Q2FRAME_EVENT_TIME_REPLAY",
        "q2frame": {
            "path": str(q2frame_path),
            "sha256": final_sha,
            "expected_sha256": expected_sha256,
            "source": "t1-v2 exact-release local Q2Frame output",
        },
        "barrier_q2frame": (
            {
                "path": str(barrier_q2frame_path),
                "sha256": barrier_final_sha,
                "expected_sha256": expected_barrier_q2frame_sha256,
                "sha256_stable_across_load_and_repeats": barrier_input_stable,
                "source": "t1-v2 captured in-slice barrier Q2Frame sidecar",
                "auxiliary_observation_tags": sorted(
                    tag for tag in barrier_snapshots if tag not in AUCTION_TAGS
                ),
            }
            if barrier_q2frame_path is not None
            else None
        ),
        "inventory": {
            key: value
            for key, value in inventory.items()
            if key != "_frame_gap_diagnostics"
        },
        "frame_gap_diagnostics": frame_gap_diagnostics,
        "ordered": first,
        "repeat": repeat,
        "determinism": determinism,
        "deterministic": deterministic,
        **(
            {"replay_termination_policy": "THROUGH_FINAL_INPUT_FRAME"}
            if continue_through_input
            else {}
        ),
        "auction_timing_policy": {
            tag: {
                "policy_version": policies.policy_version,
                "business_anchor_ms": policies.at(trade_date)["business_anchor_ms"],
                "first_observable_ms": policies.at(trade_date)["first_observable_ms"],
                "preferred_finalize_ms": policies.at(trade_date)["preferred_finalize_ms"],
                "soft_deadline_ms": policies.at(trade_date)["soft_deadline_ms"],
            }
            for tag in AUCTION_TAGS
            for policies in (AuctionTimingPolicyV1.default(tag),)
        },
        "opening_timing_policy": (
            {
                "trigger_id": OPENING_TAG,
                "business_anchor_local": "09:32:00",
                "evaluation_local": OPENING_EVALUATION_LOCAL,
                "evaluation_time_ms": _opening_evaluation_ms(trade_date),
                "source_availability": "UNKNOWN_NOT_INFERRED",
            }
            if include_opening
            else None
        ),
        "plate_amount_context": (
            {
                "contract": plate_amount_context["contract"],
                "trade_date": plate_amount_context["trade_date"],
                "content_hash": plate_amount_context["content_hash"],
                "source_provenance": plate_amount_context["source_provenance"],
                "selected_plates": plate_amount_context["selected_plates"],
            }
            if plate_amount_context is not None
            else None
        ),
        "plate_price_reference_context": (
            {
                "contract": plate_price_reference_context["contract"],
                "trade_date": plate_price_reference_context["trade_date"],
                "content_hash": plate_price_reference_context["content_hash"],
                "source_provenance": plate_price_reference_context[
                    "source_provenance"
                ],
                "selected_plates": plate_price_reference_context["selected_plates"],
            }
            if plate_price_reference_context is not None
            else None
        ),
        "plate_auction_pressure_context": (
            {
                "contract": plate_auction_pressure_context["contract"],
                "trade_date": plate_auction_pressure_context["trade_date"],
                "content_hash": plate_auction_pressure_context["content_hash"],
                "source_provenance": plate_auction_pressure_context[
                    "source_provenance"
                ],
                "selected_plates": plate_auction_pressure_context["selected_plates"],
                "historical_available_at": "UNKNOWN_NOT_INFERRED",
                "source_layer": "CAPTURED_TD_AUCTION_0924_TO_0925_SIDECAR",
            }
            if plate_auction_pressure_context is not None
            else None
        ),
        "plate_field_delta_context": (
            {
                "contract": plate_field_delta_context["contract"],
                "trade_date": plate_field_delta_context["trade_date"],
                "content_hash": plate_field_delta_context["content_hash"],
                "source_provenance": plate_field_delta_context[
                    "source_provenance"
                ],
                "selected_plates": plate_field_delta_context["selected_plates"],
                "historical_available_at": "UNKNOWN_NOT_INFERRED",
                "source_layer": "CAPTURED_TD_AUCTION_0924_TO_0925_FIELD_FACTS",
            }
            if plate_field_delta_context is not None
            else None
        ),
        "auction_price_field_policy": {
            tag: field for tag, field in zip(AUCTION_TAGS, AUCTION_PRICE_FIELDS)
        },
        "missing_auction_price_behavior": "MISSING_NO_FALLBACK_TO_LATEST_PX",
        "replay_time_policy": (
            "logical frame and timer times are floored to whole seconds; "
            "raw Q2Frame/update source milliseconds are retained in payload/evidence"
        ),
        "historical_available_at": "UNKNOWN_NOT_INFERRED",
        "rabbit_arrival_order": "UNKNOWN_NOT_INFERRED",
        "rabbit_delivery_membership": "UNKNOWN_NOT_INFERRED",
        "normal_opening_acceptance": "NOT_EVALUATED",
        "decision_status": "FACT_ONLY",
        "production_side_effects": "NONE; local Q2Frame read and in-memory Core Engine only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q2frame", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument(
        "--barrier-q2frame",
        type=Path,
        help="optional t1-v2 captured in-slice barrier Q2Frame JSONL",
    )
    parser.add_argument(
        "--expected-barrier-q2frame-sha256",
        help="pin the optional barrier Q2Frame sidecar bytes",
    )
    parser.add_argument(
        "--include-opening",
        action="store_true",
        help="continue the same Engine/Q2Frame replay through 09:32:10",
    )
    parser.add_argument(
        "--continue-through-input",
        action="store_true",
        help=(
            "after configured auction/opening barriers, continue the same Engine "
            "through the final input Q2Frame"
        ),
    )
    parser.add_argument(
        "--include-partial-reason-diagnostics",
        action="store_true",
        help="add report-only 09:25 PARTIAL reason diagnostics outside Engine hashes",
    )
    parser.add_argument(
        "--plate-amount-context",
        type=Path,
        help="date-pinned OpeningPlateAmountContextV1 JSON input",
    )
    parser.add_argument(
        "--plate-price-reference-context",
        type=Path,
        help="date-pinned OpeningPlatePriceReferenceV1 JSON input",
    )
    parser.add_argument(
        "--plate-auction-pressure-context",
        type=Path,
        help="hash-pinned captured TD 0924→0925 pressure sidecar JSON input",
    )
    parser.add_argument(
        "--plate-field-delta-context",
        type=Path,
        help="hash-pinned captured TD 0924→0925 field delta sidecar JSON input",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plate_amount_context = (
        json.loads(args.plate_amount_context.read_text(encoding="utf-8"))
        if args.plate_amount_context is not None
        else None
    )
    plate_price_reference_context = (
        json.loads(args.plate_price_reference_context.read_text(encoding="utf-8"))
        if args.plate_price_reference_context is not None
        else None
    )
    plate_auction_pressure_context = (
        json.loads(args.plate_auction_pressure_context.read_text(encoding="utf-8"))
        if args.plate_auction_pressure_context is not None
        else None
    )
    plate_field_delta_context = (
        json.loads(args.plate_field_delta_context.read_text(encoding="utf-8"))
        if args.plate_field_delta_context is not None
        else None
    )
    result = run_q2frame_auction_engine_shadow(
        q2frame_path=args.q2frame,
        trade_date=args.trade_date,
        expected_sha256=args.expected_sha256,
        barrier_q2frame_path=args.barrier_q2frame,
        expected_barrier_q2frame_sha256=args.expected_barrier_q2frame_sha256,
        include_opening=args.include_opening,
        continue_through_input=args.continue_through_input,
        include_partial_reason_diagnostics=args.include_partial_reason_diagnostics,
        plate_amount_context=plate_amount_context,
        plate_auction_pressure_context=plate_auction_pressure_context,
        plate_field_delta_context=plate_field_delta_context,
        plate_price_reference_context=plate_price_reference_context,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(canonical_json(result))
        output.write("\n")
    print(
        canonical_json(
            {
                "output": str(args.output),
                "q2frame_sha256": result["q2frame"]["sha256"],
                "barrier_q2frame_sha256": (
                    result["barrier_q2frame"]["sha256"]
                    if result["barrier_q2frame"] is not None
                    else None
                ),
                "symbols": result["inventory"]["symbol_count"],
                "frames_processed_through_evaluation": result["ordered"][
                    "input_frames_processed"
                ],
                "input_frames_processed": result["ordered"]["input_frames_processed"],
                "input_updates_processed": result["ordered"]["input_updates_processed"],
                "replay_termination_policy": result["ordered"].get(
                    "replay_termination_policy", "LATEST_CONFIGURED_BARRIER"
                ),
                "updates_processed_through_evaluation": result["ordered"][
                    "input_updates_processed"
                ],
                "deterministic": result["deterministic"],
                "anchors": {
                    tag: result["ordered"]["anchor_evidence"][tag]["fact_status_counts"]
                    for tag in AUCTION_TAGS
                },
                "opening_fact_status_counts": (
                    result["ordered"]["opening_evidence"][OPENING_TAG][
                        "fact_status_counts"
                    ]
                    if args.include_opening
                    else None
                ),
            }
        )
    )
    return 0 if result["deterministic"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
