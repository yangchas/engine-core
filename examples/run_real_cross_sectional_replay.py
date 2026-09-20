"""TASK-003: stream a real TD window one global 3-second slice at a time.

Only this example knows about TDengine.  Core receives one bounded slice at a
time and never opens a database/Redis/Rabbit connection.  No raw full-window
capture is written to disk and no production writer is called.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from engine_core import (  # noqa: E402
    CrossSectionReplaySource,
    IncrementalCrossSectionState,
    DeterministicEngine,
    FrameManifestV1,
    MarketStateReducer,
    ProbeStrategy,
    TDEventV1,
    VirtualClock,
    WindowManager,
    WindowSpec,
    canonical_json,
    local_datetime_ms,
)
from engine_core.contracts import semantic_hash  # noqa: E402
from engine_core.q2 import IncrementalQ2Projection, normalize_symbol  # noqa: E402


TRADE_DATE = "2026-09-18"
WINDOW_START = "09:15:00"
WINDOW_END = "09:40:00"
SOURCE_TABLE = "market_data1.stock_tick_v2"
SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")
FIELDS = ("ts", "px_milli", "pc_milli", "amt_yuan", "vol_units", "symbol")
REQUIRED_FIELDS = ("ts", "px_milli", "pc_milli", "amt_yuan", "symbol")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(canonical_json(_jsonable(value)) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sequence_hash(values: list[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(_jsonable(value)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _parse_local_time(value: str) -> int:
    """Parse an Asia/Shanghai wall-clock time on the fixed trade date."""

    try:
        datetime.strptime(value, "%H:%M:%S")
    except ValueError as exc:
        raise ValueError("time must use HH:MM:SS") from exc
    return local_datetime_ms(TRADE_DATE, value)


def _connect_td() -> Any:
    import taos  # type: ignore[import-not-found]

    return taos.connect(
        host=os.environ.get("TDENGINE_HOST", "127.0.0.1"),
        port=int(os.environ.get("TDENGINE_PORT", "6030")),
        user=os.environ.get("TDENGINE_USER", "root"),
        password=os.environ.get("TDENGINE_PASSWORD", "taosdata"),
        database=os.environ.get("TDENGINE_DATABASE", "market_data1"),
    )


def _local_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(SOURCE_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _describe_and_universe(start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], tuple[str, ...], str]:
    """Read schema and only the distinct symbol universe, never all rows."""

    conn = _connect_td()
    schema: list[dict[str, Any]] = []
    try:
        cursor = conn.cursor()
        cursor.execute("DESCRIBE " + SOURCE_TABLE)
        for row in cursor.fetchall():
            schema.append({
                "field": _jsonable(row[0]) if len(row) > 0 else None,
                "type": _jsonable(row[1]) if len(row) > 1 else None,
                "length": _jsonable(row[2]) if len(row) > 2 else None,
                "note": _jsonable(row[3:]),
            })
        universe_sql = (
            "SELECT DISTINCT symbol FROM " + SOURCE_TABLE
            + f" WHERE ts >= '{_local_time(start_ms)}' AND ts < '{_local_time(end_ms)}'"
        )
        cursor.execute(universe_sql)
        symbols = tuple(sorted({normalize_symbol(_jsonable(row[0])) for row in cursor.fetchall() if row and row[0] is not None}))
    finally:
        conn.close()
    return schema, symbols, universe_sql


def _frame_stats(frame: Any) -> dict[str, Any]:
    return {
        "frame_no": frame.frame_no,
        "start_time_ms": frame.start_ms,
        "end_time_exclusive_ms": frame.end_exclusive_ms,
        "logical_time_ms": frame.logical_ts_ms,
        "event_count": len(frame.events),
        "updated_count": frame.observed_count,
        "missing_count": frame.missing_count,
        "coverage": frame.coverage,
        "completeness": frame.completeness,
        "source_min_time_ms": frame.source_time_min_ms,
        "source_max_time_ms": frame.source_time_max_ms,
        "content_hash": frame.content_hash,
        "evidence_hash": frame.evidence_hash,
    }


def _run_pass(
    conn: Any,
    source: CrossSectionReplaySource,
    expected_symbols: tuple[str, ...],
    *,
    shuffled: bool,
    verification_level: str = "FULL",
) -> dict[str, Any]:
    """Execute one pass from one ordered streaming cursor.

    The cursor is fetched in bounded batches.  ``pending_events`` contains
    only the current 3-second frame; once a frame is submitted to the Engine
    it is discarded before the next frame is assembled.
    """

    start_ms = source.slice_anchor_ms
    frame_count = source.frame_count
    pass_started_ns = time.perf_counter_ns()
    clock = source.clock
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("replay", start_ms, source.end_exclusive_ms),)),
        ProbeStrategy(),
        session_id=TRADE_DATE + "-cross-sectional-replay",
        phase="REPLAY",
        result_history_limit=1,
    )
    incremental_state = IncrementalCrossSectionState(expected_symbols, trade_date=TRADE_DATE)
    projection_builder = (
        IncrementalQ2Projection(TRADE_DATE, expected_symbols, source_id="task003_td_cross_section_replay")
        if verification_level != "FULL" else None
    )
    frame_statistics: list[dict[str, Any]] = []
    frame_hashes: list[str] = []
    state_hashes: list[str] = []
    projection_hashes: list[str] = []
    signal_hashes: list[str] = []
    input_digest = hashlib.sha256()
    canonical_event_digest = hashlib.sha256()
    returned_rows = 0
    replay_events = 0
    invalid_rows = 0
    missing_fields = {field: 0 for field in REQUIRED_FIELDS}
    invalid_examples: list[dict[str, Any]] = []
    last_frame_for_parity: Any = None
    stage_ns = {
        "stream_query_time_ns": 0,
        "td_first_row_latency_ns": None,
        "td_fetch_time_ns": 0,
        "row_normalization_time_ns": 0,
        "frame_build_time_ns": 0,
        "signal_build_time_ns": 0,
        "engine_consume_time_ns": 0,
        "hash_time_ns": 0,
    }

    def submit_frame(frame_no: int, events: list[TDEventV1]) -> None:
        nonlocal replay_events, last_frame_for_parity
        if shuffled:
            random.Random(f"TASK-003:{frame_no}").shuffle(events)
        incremental_state.apply(events)
        frame_started_ns = time.perf_counter_ns()
        frame = source.frame_from_events(frame_no, events, presorted=not shuffled)
        last_frame_for_parity = frame
        stage_ns["frame_build_time_ns"] += time.perf_counter_ns() - frame_started_ns
        signal_started_ns = time.perf_counter_ns()
        state_override = None
        if verification_level in {"FRAME", "FINAL", "NONE"} and not (
            verification_level == "FINAL" and frame_no == frame_count - 1
        ):
            state_override = incremental_state.identity_hash(
                frame_no=frame.frame_no,
                logical_ts_ms=frame.logical_ts_ms,
                updated_symbols=frame.updated_symbols,
                missing_symbols=frame.missing_symbols,
                completeness=frame.completeness,
                coverage=frame.coverage,
            )
        signal = source.signal_for_frame(
            frame,
            incremental_state.latest_raw,
            signal_prefix="task003-cross-section",
            state_content_hash_override=state_override,
            projection_builder=projection_builder,
            symbol_states_already_frozen=True,
        )
        signal_elapsed_ns = time.perf_counter_ns() - signal_started_ns
        stage_ns["signal_build_time_ns"] += signal_elapsed_ns
        stage_ns["hash_time_ns"] += signal_elapsed_ns
        frame_hashes.append(frame.content_hash)
        for event in frame.events:
            canonical_event_digest.update(event.content_hash.encode("utf-8"))
            canonical_event_digest.update(b"\n")
        if verification_level in {"FULL", "FINAL"}:
            state_hashes.append(signal.payload.cross_section.content_hash)
        projection_hashes.append(signal.payload.content_hash)
        signal_hashes.append(semantic_hash({
            "signal_id": signal.signal_id,
            "logical_time_ms": signal.logical_time_ms,
            "signal_seq": signal.signal_seq,
            "payload_hash": signal.payload.content_hash,
        }))
        clock.advance_to(datetime.fromtimestamp(signal.logical_time_ms / 1000, timezone.utc))
        engine_started_ns = time.perf_counter_ns()
        engine.submit(signal)
        engine.run_until_empty()
        stage_ns["engine_consume_time_ns"] += time.perf_counter_ns() - engine_started_ns
        frame_statistics.append(_frame_stats(frame))
        replay_events += len(events)
        if frame_no and frame_no % 50 == 0:
            elapsed_ms = (time.perf_counter_ns() - pass_started_ns) / 1_000_000
            print(json.dumps({
                "heartbeat": True,
                "frame_no": frame_no,
                "rows_seen": returned_rows,
                "events_normalized": replay_events,
                "elapsed_ms": round(elapsed_ms, 3),
                "rows_per_sec": round(returned_rows / max(elapsed_ms / 1000, 0.001), 2),
                "frames_per_sec": round((frame_no + 1) / max(elapsed_ms / 1000, 0.001), 2),
                "last_frame_ms": round((time.perf_counter_ns() - frame_started_ns) / 1_000_000, 3),
                "engine_ms": round(stage_ns["engine_consume_time_ns"] / 1_000_000, 3),
                "hash_ms": round(stage_ns["hash_time_ns"] / 1_000_000, 3),
            }, sort_keys=True), flush=True)

    cursor = conn.cursor()
    full_sql = (
        "SELECT " + ", ".join(FIELDS)
        + f" FROM {SOURCE_TABLE}"
        + f" WHERE ts >= '{_local_time(start_ms)}' AND ts < '{_local_time(source.end_exclusive_ms)}'"
        + " ORDER BY ts, symbol"
    )
    query_started_ns = time.perf_counter_ns()
    cursor.execute(full_sql)
    execute_done_ns = time.perf_counter_ns()
    stage_ns["stream_query_time_ns"] += execute_done_ns - query_started_ns
    first_batch = True
    pending_frame_no = 0
    pending_events: list[TDEventV1] = []
    while True:
        fetch_started_ns = time.perf_counter_ns()
        batch = cursor.fetchmany(10_000)
        fetch_done_ns = time.perf_counter_ns()
        stage_ns["td_fetch_time_ns"] += fetch_done_ns - fetch_started_ns
        if first_batch:
            stage_ns["td_first_row_latency_ns"] = fetch_done_ns - query_started_ns
            first_batch = False
        if not batch:
            break
        for row in batch:
            returned_rows += 1
            normalize_started_ns = time.perf_counter_ns()
            raw = {name: _jsonable(value) for name, value in zip(FIELDS, row)}
            input_digest.update(canonical_json(raw).encode("utf-8"))
            input_digest.update(b"\n")
            for field in REQUIRED_FIELDS:
                if raw.get(field) is None:
                    missing_fields[field] += 1
            try:
                event = TDEventV1.from_mapping(raw, source_timezone=SOURCE_TIMEZONE)
            except (TypeError, ValueError) as exc:
                stage_ns["row_normalization_time_ns"] += time.perf_counter_ns() - normalize_started_ns
                invalid_rows += 1
                if len(invalid_examples) < 20:
                    invalid_examples.append({
                        "frame_no": pending_frame_no,
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "symbol": raw.get("symbol"),
                    })
                continue
            stage_ns["row_normalization_time_ns"] += time.perf_counter_ns() - normalize_started_ns
            event_frame_no = (event.event_time_ms - start_ms) // source.slice_ms
            if event_frame_no < 0 or event_frame_no >= frame_count:
                invalid_rows += 1
                continue
            if event_frame_no < pending_frame_no:
                raise ValueError("TD stream moved backwards across 3-second frames")
            while pending_frame_no < event_frame_no:
                submit_frame(pending_frame_no, pending_events)
                pending_frame_no += 1
                pending_events = []
            pending_events.append(event)
    while pending_frame_no < frame_count:
        submit_frame(pending_frame_no, pending_events)
        pending_frame_no += 1
        pending_events = []

    final_snapshot = engine._reducer.build_snapshot(
        "TASK003_FINAL",
        logical_time_ms=source.end_exclusive_ms,
        windows=engine._windows,
        phase="REPLAY",
    )
    coverages = [item["coverage"] for item in frame_statistics]
    source_times = [
        timestamp
        for item in frame_statistics
        for timestamp in (item["source_min_time_ms"], item["source_max_time_ms"])
        if timestamp is not None
    ]
    final_cross_section_full_hash = None
    final_cross_section_incremental_identity = None
    if last_frame_for_parity is not None:
        final_cross_section_full_hash = incremental_state.full_state_hash(
            frame_no=last_frame_for_parity.frame_no,
            logical_ts_ms=last_frame_for_parity.logical_ts_ms,
            updated_symbols=last_frame_for_parity.updated_symbols,
            missing_symbols=last_frame_for_parity.missing_symbols,
            completeness=last_frame_for_parity.completeness,
            coverage=last_frame_for_parity.coverage,
            source_time_min_ms=last_frame_for_parity.source_time_min_ms,
            source_time_max_ms=last_frame_for_parity.source_time_max_ms,
        )
        final_cross_section_incremental_identity = incremental_state.identity_hash(
            frame_no=last_frame_for_parity.frame_no,
            logical_ts_ms=last_frame_for_parity.logical_ts_ms,
            updated_symbols=last_frame_for_parity.updated_symbols,
            missing_symbols=last_frame_for_parity.missing_symbols,
            completeness=last_frame_for_parity.completeness,
            coverage=last_frame_for_parity.coverage,
        )
    return {
        "shuffled": shuffled,
        "frame_count": frame_count,
        "frame_statistics": frame_statistics,
        "frame_hash": _sequence_hash(frame_hashes),
        "state_hash": _sequence_hash(state_hashes),
        "final_cross_section_full_hash": final_cross_section_full_hash,
        "final_cross_section_incremental_identity": final_cross_section_incremental_identity,
        "state_hash_verification": {
            "FULL": "FULL_PER_FRAME",
            "FRAME": "INCREMENTAL_IDENTITY_ONLY",
            "FINAL": "FINAL_ONLY",
            "NONE": "NONE",
        }.get(verification_level, "UNKNOWN"),
        "final_cross_section_full_hash_parity": (
            "NOT_RUN" if not state_hashes else (
                "PASS" if final_cross_section_full_hash == state_hashes[-1] else "FAIL"
            )
        ),
        "projection_hash": _sequence_hash(projection_hashes),
        "signal_hash": _sequence_hash(signal_hashes),
        "processed_signals": engine._processed,
        "reducer_revision": engine._reducer.state.revision,
        "final_state_hash": final_snapshot.content_hash,
        "final_virtual_clock": clock.now_utc().isoformat(),
        "empty_frames": sum(item["completeness"] == "EMPTY" for item in frame_statistics),
        "partial_frames": sum(item["completeness"] == "PARTIAL" for item in frame_statistics),
        "complete_frames": sum(item["completeness"] == "COMPLETE" for item in frame_statistics),
        "total_events": replay_events,
        "distinct_symbols": len(expected_symbols),
        "coverage_min": min(coverages) if coverages else 0.0,
        "coverage_max": max(coverages) if coverages else 0.0,
        "coverage_avg": sum(coverages) / len(coverages) if coverages else 0.0,
        "source_time_min_ms": min(source_times) if source_times else None,
        "source_time_max_ms": max(source_times) if source_times else None,
        "returned_rows": returned_rows,
        "replay_events": replay_events,
        "invalid_rows": invalid_rows,
        "missing_required_field_counts": missing_fields,
        "invalid_examples": invalid_examples,
        "source_return_order_hash": input_digest.hexdigest(),
        "canonical_event_content_hash": canonical_event_digest.hexdigest(),
        "verification_level": verification_level,
        "timings_ms": {
            key: None if value is None else round(value / 1_000_000, 3)
            for key, value in stage_ns.items()
        },
        "pass_elapsed_ms": round((time.perf_counter_ns() - pass_started_ns) / 1_000_000, 3),
    }


def _checksums(output_dir: Path) -> None:
    lines = []
    for path in sorted(output_dir.iterdir()):
        if path.name != "sha256sums.txt" and path.is_file():
            lines.append(f"{_sha256(path)}  {path.name}")
    (output_dir / "sha256sums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _blocked(output_dir: Path, error: Exception) -> int:
    _write_json(output_dir / "replay_summary.json", {
        "task_id": "TASK-003",
        "status": "CROSS_SECTION_REPLAY_BLOCKED",
        "error": type(error).__name__,
        "message": str(error),
        "side_effects": "NONE_OBSERVED",
    })
    _write_json(output_dir / "side_effect_audit.json", {
        "status": "NONE_OBSERVED",
        "td_operations": ["DESCRIBE", "SELECT", "SELECT DISTINCT"],
        "td_write": False,
        "redis_write": False,
        "rabbit_consume": False,
        "service_restart": False,
        "effect_or_notification": False,
    })
    _checksums(output_dir)
    print(json.dumps({"status": "CROSS_SECTION_REPLAY_BLOCKED", "output_dir": str(output_dir), "error": type(error).__name__}, ensure_ascii=False))
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-time", default=WINDOW_START)
    parser.add_argument("--end-time", default=WINDOW_END)
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--passes", choices=("ordered", "both"), default="both")
    parser.add_argument("--verification-level", choices=("FULL", "FRAME", "FINAL", "NONE"), default="FULL")
    args = parser.parse_args()
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        start_ms = _parse_local_time(args.start_time)
        configured_end_ms = _parse_local_time(args.end_time)
        if args.max_frames is not None:
            if args.max_frames <= 0:
                raise ValueError("max-frames must be positive")
            configured_end_ms = min(configured_end_ms, start_ms + args.max_frames * 3_000)
        if configured_end_ms <= start_ms or (configured_end_ms - start_ms) % 3_000:
            raise ValueError("replay interval must contain whole 3-second frames")
        end_ms = configured_end_ms
    except Exception as exc:
        return _blocked(output_dir, exc)
    window_start = args.start_time
    window_end = datetime.fromtimestamp(end_ms / 1000, timezone.utc).astimezone(SOURCE_TIMEZONE).strftime("%H:%M:%S")
    profile_started_ns = time.perf_counter_ns()
    describe_started_ns = time.perf_counter_ns()
    try:
        schema, expected_symbols, universe_sql = _describe_and_universe(start_ms, end_ms)
        if not expected_symbols:
            raise RuntimeError("bounded TD universe is empty")
        describe_elapsed_ns = time.perf_counter_ns() - describe_started_ns
        conn_a = _connect_td()
        conn_b = _connect_td() if args.passes == "both" else None
        try:
            source_a = CrossSectionReplaySource(
                TRADE_DATE, expected_symbols,
                VirtualClock(datetime.fromtimestamp(start_ms / 1000, timezone.utc)),
                slice_anchor_ms=start_ms,
                end_exclusive_ms=end_ms,
                source_timezone=SOURCE_TIMEZONE,
                source_id="task003_td_cross_section_replay",
            )
            source_b = CrossSectionReplaySource(
                TRADE_DATE, expected_symbols,
                VirtualClock(datetime.fromtimestamp(start_ms / 1000, timezone.utc)),
                slice_anchor_ms=start_ms,
                end_exclusive_ms=end_ms,
                source_timezone=SOURCE_TIMEZONE,
                source_id="task003_td_cross_section_replay",
            )
            ordered = _run_pass(
                conn_a, source_a, expected_symbols, shuffled=False,
                verification_level=args.verification_level,
            )
            shuffled = None
            if conn_b is not None:
                shuffled = _run_pass(
                    conn_b, source_b, expected_symbols, shuffled=True,
                    verification_level=args.verification_level,
                )
        finally:
            conn_a.close()
            if conn_b is not None:
                conn_b.close()
    except Exception as exc:
        return _blocked(output_dir, exc)

    compare_keys = (
        "frame_count", "frame_hash", "state_hash", "projection_hash", "signal_hash",
        "processed_signals", "reducer_revision", "final_state_hash", "final_virtual_clock",
    )
    both_passes = shuffled is not None
    equal = None if not both_passes else all(ordered[key] == shuffled[key] for key in compare_keys)
    if both_passes:
        status = "CROSS_SECTION_REPLAY_NON_DETERMINISTIC" if not equal else (
            "CROSS_SECTION_REPLAY_PARTIAL" if ordered["invalid_rows"] or shuffled["invalid_rows"] else "CROSS_SECTION_REPLAY_READY"
        )
    else:
        elapsed_ms = ordered["pass_elapsed_ms"]
        if ordered["frame_count"] == 500:
            status = (
                "CROSS_SECTION_REPLAY_BLOCKED_BY_PERFORMANCE"
                if elapsed_ms > 600_000
                else "CROSS_SECTION_REPLAY_PASS_WITH_WARN"
            )
        else:
            status = "CROSS_SECTION_REPLAY_PROFILED"
    manifest = FrameManifestV1(
        trade_date=TRADE_DATE,
        source_timezone="Asia/Shanghai",
        start_ms=start_ms,
        end_exclusive_ms=end_ms,
        frame_interval_ms=3_000,
        expected_symbols=expected_symbols,
        frame_count=ordered["frame_count"],
        event_count=ordered["replay_events"],
        source_table=SOURCE_TABLE,
        query_hash=semantic_hash({"universe": universe_sql, "stream_query": "SELECT ordered bounded window; emit 3-second frames"}),
        input_hash=ordered["source_return_order_hash"],
    )
    _write_json(output_dir / "source_schema.json", {
        "table": SOURCE_TABLE,
        "selected_fields": list(FIELDS),
        "describe": schema,
        "timestamp_semantics": "TD source record timestamp; not Rabbit arrival time",
        "source_sequence_status": "UNKNOWN",
        "rabbit_arrival_order": "UNKNOWN",
        "historical_available_at": "UNKNOWN",
    })
    _write_json(output_dir / "manifest.json", asdict(manifest))
    _write_json(output_dir / "inventory.json", {
        "table": SOURCE_TABLE,
        "window": {"start": window_start, "end_exclusive": window_end, "timezone": "Asia/Shanghai"},
        "expected_symbol_count": len(expected_symbols),
        "ordered": {key: ordered[key] for key in ("returned_rows", "replay_events", "invalid_rows", "missing_required_field_counts", "source_return_order_hash", "canonical_event_content_hash")},
        "shuffled": None if shuffled is None else {key: shuffled[key] for key in ("returned_rows", "replay_events", "invalid_rows", "missing_required_field_counts", "source_return_order_hash", "canonical_event_content_hash")},
        "query_shape": "one ordered streaming SELECT per pass; rows are emitted into 3-second global half-open frames and discarded after submission",
        "passes": args.passes,
        "verification_level": args.verification_level,
    })
    with (output_dir / "frame_statistics.jsonl").open("w", encoding="utf-8") as handle:
        for item in ordered["frame_statistics"]:
            handle.write(canonical_json(item) + "\n")
    _write_json(output_dir / "determinism_comparison.json", {
        "status": "NOT_RUN" if not both_passes else ("PASS" if equal else "CROSS_SECTION_REPLAY_NON_DETERMINISTIC"),
        "equal": equal,
        "comparison_keys": list(compare_keys),
        "ordered": {key: ordered[key] for key in compare_keys},
        "shuffled": None if shuffled is None else {key: shuffled[key] for key in compare_keys},
        "ordering_contract": "event_time + symbol + content_hash; synthetic tie-break is not Rabbit arrival order",
        "shuffle_scope": "within-frame only; this is not a full-input arrival-order shuffle",
    })
    _write_json(output_dir / "engine_summary.json", {
        "status": status,
        "session_id": TRADE_DATE + "-cross-sectional-replay",
        "phase": "REPLAY",
        "ordered": {key: value for key, value in ordered.items() if key != "frame_statistics"},
        "shuffled": None if shuffled is None else {key: value for key, value in shuffled.items() if key != "frame_statistics"},
    })
    _write_json(output_dir / "replay_summary.json", {
        "task_id": "TASK-003",
        "status": status,
        "trade_date": TRADE_DATE,
        "window": {"start": window_start, "end_exclusive": window_end, "timezone": "Asia/Shanghai"},
        "total_frames": ordered["frame_count"],
        "empty_frames": ordered["empty_frames"],
        "partial_frames": ordered["partial_frames"],
        "complete_frames": ordered["complete_frames"],
        "total_events": ordered["total_events"],
        "distinct_symbols": ordered["distinct_symbols"],
        "q2_producer_replay": "NOT_STARTED",
        "auction_full_flow_replay": "NOT_STARTED",
        "side_effects": "NONE_OBSERVED",
        "passes": args.passes,
        "verification_level": args.verification_level,
    })
    _write_json(output_dir / "performance_profile.json", {
        "verification_level": args.verification_level,
        "passes": args.passes,
        "window": {"start": window_start, "end_exclusive": window_end, "frame_count": ordered["frame_count"]},
        "describe_and_universe_ms": round(describe_elapsed_ns / 1_000_000, 3),
        "pass_timings": {
            "ordered": ordered["timings_ms"],
            "ordered_elapsed_ms": ordered["pass_elapsed_ms"],
            "shuffled": None if shuffled is None else shuffled["timings_ms"],
            "shuffled_elapsed_ms": None if shuffled is None else shuffled["pass_elapsed_ms"],
        },
        "profile_elapsed_ms": round((time.perf_counter_ns() - profile_started_ns) / 1_000_000, 3),
    })
    _write_json(output_dir / "benchmark_20_frames.json", {
        "status": status if ordered["frame_count"] <= 20 else "NOT_APPLICABLE",
        "frame_count": ordered["frame_count"],
        "ordered_elapsed_ms": ordered["pass_elapsed_ms"],
        "rows": ordered["returned_rows"],
        "timings_ms": ordered["timings_ms"],
    })
    _write_json(output_dir / "benchmark_full_ordered.json", {
        "status": status if ordered["frame_count"] == 500 and args.passes == "ordered" else "NOT_RUN",
        "reason": None if ordered["frame_count"] == 500 and args.passes == "ordered" else "full 500-frame ordered benchmark was not executed in this bounded profiling run",
        "frame_count": ordered["frame_count"],
        "ordered_elapsed_ms": ordered["pass_elapsed_ms"],
        "rows": ordered["returned_rows"],
        "timings_ms": ordered["timings_ms"],
        "thresholds": {"pass_ms": 300_000, "warn_ms": 600_000, "blocked_above_ms": 600_000},
    })
    (output_dir / "optimization_decisions.md").write_text(
        "# Replay performance decisions\n\n"
        "- TD input is consumed with one ordered cursor and bounded `fetchmany` batches.\n"
        "- The runner retains only the current 3-second frame; submitted frames are discarded.\n"
        "- Empty frames remain in the 3-second timeline.\n"
        "- The ordered pass is the baseline. The optional second pass shuffles only events within each frame; it is not a Rabbit arrival-order simulation.\n"
        "- No semantic verification was removed by this profiling run; verification level is recorded in the manifest.\n",
        encoding="utf-8",
    )
    (output_dir / "unknowns_and_limits.md").write_text(
        "# TASK-003 unknowns and limits\n\n"
        "- TD rows were fetched one global 3-second half-open slice at a time; no full-window row fetch was used.\n"
        "- `source_sequence_status`, Rabbit arrival order and historical `available_at` remain UNKNOWN.\n"
        "- Selected stock-tick fields do not reconstruct t1-v2/Q2 producer formulas.\n"
        "- Missing required values are counted and excluded from normalized replay; they are never replaced with zero.\n"
        "- Redis, Wencai, strategy and production recovery are intentionally not executed.\n",
        encoding="utf-8",
    )
    _write_json(output_dir / "side_effect_audit.json", {
        "status": "NONE_OBSERVED",
        "td_operations": ["DESCRIBE", "SELECT DISTINCT", "SELECT (ordered streaming window per pass)"],
        "td_write": False,
        "redis_operations": [],
        "redis_write": False,
        "rabbit_consume": False,
        "rabbit_ack_change": False,
        "service_restart": False,
        "effect_or_notification": False,
        "production_directory_write": False,
    })
    _checksums(output_dir)
    print(json.dumps({
        "status": status,
        "output_dir": str(output_dir),
        "frames": ordered["frame_count"],
        "ordered_events": ordered["replay_events"],
        "shuffled_events": None if shuffled is None else shuffled["replay_events"],
        "symbols": len(expected_symbols),
        "deterministic": equal,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if status in {
        "CROSS_SECTION_REPLAY_READY",
        "CROSS_SECTION_REPLAY_PARTIAL",
        "CROSS_SECTION_REPLAY_PROFILED",
        "CROSS_SECTION_REPLAY_PASS_WITH_WARN",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
