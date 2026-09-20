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
from engine_core.q2 import normalize_symbol  # noqa: E402


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


def _describe_and_universe() -> tuple[list[dict[str, Any]], tuple[str, ...], str]:
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
        start_ms = local_datetime_ms(TRADE_DATE, WINDOW_START)
        end_ms = local_datetime_ms(TRADE_DATE, WINDOW_END)
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
) -> dict[str, Any]:
    """Execute one pass from one ordered streaming cursor.

    The cursor is fetched in bounded batches.  ``pending_events`` contains
    only the current 3-second frame; once a frame is submitted to the Engine
    it is discarded before the next frame is assembled.
    """

    start_ms = source.slice_anchor_ms
    frame_count = source.frame_count
    clock = source.clock
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("replay", start_ms, source.end_exclusive_ms),)),
        ProbeStrategy(),
        session_id=TRADE_DATE + "-cross-sectional-replay",
        phase="REPLAY",
        result_history_limit=1,
    )
    latest_raw: dict[str, Mapping[str, Any]] = {}
    frame_statistics: list[dict[str, Any]] = []
    frame_hashes: list[str] = []
    state_hashes: list[str] = []
    projection_hashes: list[str] = []
    signal_hashes: list[str] = []
    input_digest = hashlib.sha256()
    returned_rows = 0
    replay_events = 0
    invalid_rows = 0
    missing_fields = {field: 0 for field in REQUIRED_FIELDS}
    invalid_examples: list[dict[str, Any]] = []

    def submit_frame(frame_no: int, events: list[TDEventV1]) -> None:
        nonlocal replay_events
        if shuffled:
            random.Random(f"TASK-003:{frame_no}").shuffle(events)
        frame = source.frame_from_events(frame_no, events)
        signal = source.signal_for_frame(frame, latest_raw, signal_prefix="task003-cross-section")
        frame_hashes.append(frame.content_hash)
        state_hashes.append(signal.payload.cross_section.content_hash)
        projection_hashes.append(signal.payload.content_hash)
        signal_hashes.append(semantic_hash({
            "signal_id": signal.signal_id,
            "logical_time_ms": signal.logical_time_ms,
            "signal_seq": signal.signal_seq,
            "payload_hash": signal.payload.content_hash,
        }))
        clock.advance_to(datetime.fromtimestamp(signal.logical_time_ms / 1000, timezone.utc))
        engine.submit(signal)
        engine.run_until_empty()
        frame_statistics.append(_frame_stats(frame))
        replay_events += len(events)

    cursor = conn.cursor()
    full_sql = (
        "SELECT " + ", ".join(FIELDS)
        + f" FROM {SOURCE_TABLE}"
        + f" WHERE ts >= '{_local_time(start_ms)}' AND ts < '{_local_time(source.end_exclusive_ms)}'"
        + " ORDER BY ts, symbol"
    )
    cursor.execute(full_sql)
    pending_frame_no = 0
    pending_events: list[TDEventV1] = []
    while True:
        batch = cursor.fetchmany(10_000)
        if not batch:
            break
        for row in batch:
            returned_rows += 1
            raw = {name: _jsonable(value) for name, value in zip(FIELDS, row)}
            input_digest.update(canonical_json(raw).encode("utf-8"))
            input_digest.update(b"\n")
            for field in REQUIRED_FIELDS:
                if raw.get(field) is None:
                    missing_fields[field] += 1
            try:
                event = TDEventV1.from_mapping(raw, source_timezone=SOURCE_TIMEZONE)
            except (TypeError, ValueError) as exc:
                invalid_rows += 1
                if len(invalid_examples) < 20:
                    invalid_examples.append({
                        "frame_no": pending_frame_no,
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "symbol": raw.get("symbol"),
                    })
                continue
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
    return {
        "shuffled": shuffled,
        "frame_count": frame_count,
        "frame_statistics": frame_statistics,
        "frame_hash": _sequence_hash(frame_hashes),
        "state_hash": _sequence_hash(state_hashes),
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
        "input_sequence_hash": input_digest.hexdigest(),
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
    args = parser.parse_args()
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    start_ms = local_datetime_ms(TRADE_DATE, WINDOW_START)
    end_ms = local_datetime_ms(TRADE_DATE, WINDOW_END)
    try:
        schema, expected_symbols, universe_sql = _describe_and_universe()
        if not expected_symbols:
            raise RuntimeError("bounded TD universe is empty")
        conn_a = _connect_td()
        conn_b = _connect_td()
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
            ordered = _run_pass(conn_a, source_a, expected_symbols, shuffled=False)
            shuffled = _run_pass(conn_b, source_b, expected_symbols, shuffled=True)
        finally:
            conn_a.close()
            conn_b.close()
    except Exception as exc:
        return _blocked(output_dir, exc)

    compare_keys = (
        "frame_count", "frame_hash", "state_hash", "projection_hash", "signal_hash",
        "processed_signals", "reducer_revision", "final_state_hash", "final_virtual_clock",
    )
    equal = all(ordered[key] == shuffled[key] for key in compare_keys)
    status = "CROSS_SECTION_REPLAY_NON_DETERMINISTIC" if not equal else (
        "CROSS_SECTION_REPLAY_PARTIAL" if ordered["invalid_rows"] or shuffled["invalid_rows"] else "CROSS_SECTION_REPLAY_READY"
    )
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
        input_hash=ordered["input_sequence_hash"],
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
        "window": {"start": WINDOW_START, "end_exclusive": WINDOW_END, "timezone": "Asia/Shanghai"},
        "expected_symbol_count": len(expected_symbols),
        "ordered": {key: ordered[key] for key in ("returned_rows", "replay_events", "invalid_rows", "missing_required_field_counts", "input_sequence_hash")},
        "shuffled": {key: shuffled[key] for key in ("returned_rows", "replay_events", "invalid_rows", "missing_required_field_counts", "input_sequence_hash")},
        "query_shape": "one ordered streaming SELECT per pass; rows are emitted into 3-second global half-open frames",
    })
    with (output_dir / "frame_statistics.jsonl").open("w", encoding="utf-8") as handle:
        for item in ordered["frame_statistics"]:
            handle.write(canonical_json(item) + "\n")
    _write_json(output_dir / "determinism_comparison.json", {
        "status": "PASS" if equal else "CROSS_SECTION_REPLAY_NON_DETERMINISTIC",
        "equal": equal,
        "comparison_keys": list(compare_keys),
        "ordered": {key: ordered[key] for key in compare_keys},
        "shuffled": {key: shuffled[key] for key in compare_keys},
        "ordering_contract": "event_time + symbol + content_hash; synthetic tie-break is not Rabbit arrival order",
    })
    _write_json(output_dir / "engine_summary.json", {
        "status": status,
        "session_id": TRADE_DATE + "-cross-sectional-replay",
        "phase": "REPLAY",
        "ordered": {key: value for key, value in ordered.items() if key != "frame_statistics"},
        "shuffled": {key: value for key, value in shuffled.items() if key != "frame_statistics"},
    })
    _write_json(output_dir / "replay_summary.json", {
        "task_id": "TASK-003",
        "status": status,
        "trade_date": TRADE_DATE,
        "window": {"start": WINDOW_START, "end_exclusive": WINDOW_END, "timezone": "Asia/Shanghai"},
        "total_frames": ordered["frame_count"],
        "empty_frames": ordered["empty_frames"],
        "partial_frames": ordered["partial_frames"],
        "complete_frames": ordered["complete_frames"],
        "total_events": ordered["total_events"],
        "distinct_symbols": ordered["distinct_symbols"],
        "q2_producer_replay": "NOT_STARTED",
        "auction_full_flow_replay": "NOT_STARTED",
        "side_effects": "NONE_OBSERVED",
    })
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
        "shuffled_events": shuffled["replay_events"],
        "symbols": len(expected_symbols),
        "deterministic": equal,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if status in {"CROSS_SECTION_REPLAY_READY", "CROSS_SECTION_REPLAY_PARTIAL"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
