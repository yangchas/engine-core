"""Run TASK-007 against real TD rows with a read-only 3-second cursor.

This validation harness is intentionally outside the canonical package: only
this file imports the TD client.  It queries one half-open stock-tick frame at
a time, adapts rows into Rabbit-primary ``TickBatchV1`` values, feeds one
``OfflineCanonicalReplay``/``DeterministicEngine`` session, and records hashes
and diagnostics without writing TD/Redis or consuming Rabbit.
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
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from engine_core import (  # noqa: E402
    ArrivalOrderStatus,
    BatchQuality,
    DeterministicEngine,
    FrameManifestV1,
    HistoricalAvailabilityStatus,
    MarketStateReducer,
    OfflineCanonicalReplay,
    ProbeStrategy,
    ReplayOrderStatus,
    ReplaySessionTimeline,
    SequenceStatus,
    TDFrameAdapter,
    TickBatchV1,
    WindowManager,
    WindowSpec,
    canonical_json,
    local_datetime_ms,
    semantic_hash,
)


TRADE_DATE = "2026-09-18"
WINDOW_START = "09:15:00"
WINDOW_END = "09:40:00"
SOURCE_TABLE = "market_data1.stock_tick_v2"
AUCTION_TABLE = "market_data1.auction_snapshot_v2"
SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")
TICK_FIELDS = ("ts", "px_milli", "pc_milli", "amt_yuan", "vol_units", "symbol")
AUCTION_FIELDS = (
    "ts",
    "px_milli",
    "chg_bp",
    "match_amt_yuan",
    "rest_bid_amt_yuan",
    "rest_ask_amt_yuan",
    "limit_state",
    "symbol",
    "trade_date",
    "auction_tag",
)
AUCTION_TAGS = ("0920", "0924", "0925")


def _performance_status(elapsed_ms: float) -> str:
    if elapsed_ms <= 300_000:
        return "PASS"
    if elapsed_ms <= 600_000:
        return "PASS_WITH_WARN"
    return "BLOCKED_BY_PERFORMANCE"


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
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


def _checksums(output_dir: Path) -> None:
    lines = []
    for path in sorted(output_dir.iterdir()):
        if path.name != "sha256sums.txt" and path.is_file():
            lines.append(f"{_sha256(path)}  {path.name}")
    (output_dir / "sha256sums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _connect_td(args: argparse.Namespace) -> Any:
    import taos  # type: ignore[import-not-found]

    return taos.connect(
        host=args.td_host,
        port=args.td_port,
        user=args.td_user,
        password=args.td_password,
        database=args.td_database,
    )


def _local_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(SOURCE_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"unsupported TD timestamp type: {type(value).__name__}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SOURCE_TIMEZONE)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _read_schema(cursor: Any, table: str) -> list[dict[str, Any]]:
    cursor.execute("DESCRIBE " + table)
    return [
        {
            "field": _jsonable(row[0]) if len(row) > 0 else None,
            "type": _jsonable(row[1]) if len(row) > 1 else None,
            "length": _jsonable(row[2]) if len(row) > 2 else None,
            "note": _jsonable(row[3:]),
        }
        for row in cursor.fetchall()
    ]


def _read_universe_and_count(cursor: Any, start_ms: int, end_ms: int) -> tuple[tuple[str, ...], int, str, str]:
    start = _local_time(start_ms)
    end = _local_time(end_ms)
    count_sql = f"SELECT COUNT(*) FROM {SOURCE_TABLE} WHERE ts >= '{start}' AND ts < '{end}'"
    universe_sql = f"SELECT DISTINCT symbol FROM {SOURCE_TABLE} WHERE ts >= '{start}' AND ts < '{end}'"
    cursor.execute(count_sql)
    count_row = cursor.fetchone()
    event_count = int(count_row[0]) if count_row and count_row[0] is not None else 0
    cursor.execute(universe_sql)
    from engine_core.canonical_ticks import normalize_td_symbol

    symbols = tuple(
        sorted(
            {
                normalize_td_symbol(str(_jsonable(row[0])).strip())
                for row in cursor.fetchall()
                if row and row[0] is not None
            }
        )
    )
    if not symbols:
        raise RuntimeError("bounded TD stock universe is empty")
    return symbols, event_count, count_sql, universe_sql


def _read_auction_rows(cursor: Any, trade_date: str) -> tuple[dict[str, list[dict[str, Any]]], Optional[str]]:
    compact_date = trade_date.replace("-", "")
    fields = ", ".join(AUCTION_FIELDS)
    sql = (
        f"SELECT {fields} FROM {AUCTION_TABLE} "
        f"WHERE trade_date=\"{compact_date}\" "
        'AND auction_tag IN ("0920","0924","0925") ORDER BY ts, symbol, auction_tag'
    )
    try:
        cursor.execute(sql)
        grouped: dict[str, list[dict[str, Any]]] = {tag: [] for tag in AUCTION_TAGS}
        for row in cursor.fetchall():
            if len(row) != len(AUCTION_FIELDS):
                raise ValueError("auction row has an unexpected column count")
            item = {name: _jsonable(value) for name, value in zip(AUCTION_FIELDS, row)}
            item["source_time_ms"] = _timestamp_ms(item["ts"])
            tag = str(item.get("auction_tag") or "").strip()
            if tag in grouped:
                grouped[tag].append(item)
        return grouped, None
    except Exception as exc:  # A missing projection remains explicit in evidence.
        return {tag: [] for tag in AUCTION_TAGS}, f"{type(exc).__name__}: {exc}"


def _frame_rows(cursor: Any, frame_start_ms: int, frame_end_ms: int) -> list[dict[str, Any]]:
    sql = (
        "SELECT " + ", ".join(TICK_FIELDS)
        + f" FROM {SOURCE_TABLE}"
        + f" WHERE ts >= '{_local_time(frame_start_ms)}' AND ts < '{_local_time(frame_end_ms)}'"
        + " ORDER BY ts, symbol"
    )
    cursor.execute(sql)
    rows: list[dict[str, Any]] = []
    while True:
        batch = cursor.fetchmany(10_000)
        if not batch:
            break
        rows.extend({name: _jsonable(value) for name, value in zip(TICK_FIELDS, row)} for row in batch)
    return rows


def _batch_stream(
    cursor: Any,
    adapter: TDFrameAdapter,
    *,
    start_ms: int,
    end_ms: int,
    frame_count: int,
    shuffled: bool,
    stats: dict[str, Any],
) -> Iterable[TickBatchV1]:
    for frame_no in range(frame_count):
        frame_start = start_ms + frame_no * 3_000
        frame_end = frame_start + 3_000
        rows = _frame_rows(cursor, frame_start, frame_end)
        if shuffled:
            random.Random(f"TASK-007:{frame_no}").shuffle(rows)
        stats["frame_rows"] += len(rows)
        stats["empty_frames"] += not bool(rows)
        stats["source_return_order_hash"] = semantic_hash(
            (stats["source_return_order_hash"], tuple(tuple(row.get(field) for field in TICK_FIELDS) for row in rows))
        )
        yield adapter.build_batch(
            rows,
            trade_date=TRADE_DATE,
            frame_start_ms=frame_start,
            frame_end_ms=frame_end,
            seq_no=frame_no,
            mode="REPLAY",
            query_succeeded=True,
            completeness_proven=False,
        )


class _CaptureEngine:
    def __init__(self, engine: DeterministicEngine) -> None:
        self.engine = engine
        self.signal_hashes: list[str] = []
        self.signal_count = 0

    def submit(self, signal: Any) -> None:
        self.signal_count += 1
        self.signal_hashes.append(semantic_hash({
            "signal_id": signal.signal_id,
            "logical_time_ms": signal.logical_time_ms,
            "signal_seq": signal.signal_seq,
            "payload_hash": signal.payload.content_hash,
        }))
        self.engine.submit(signal)

    def run_until_empty(self) -> None:
        self.engine.run_until_empty()


def _run_pass(
    conn: Any,
    *,
    expected_symbols: tuple[str, ...],
    start_ms: int,
    end_ms: int,
    frame_count: int,
    manifest: FrameManifestV1,
    auction_rows: Mapping[str, list[dict[str, Any]]],
    auction_error: Optional[str],
    shuffled: bool,
) -> dict[str, Any]:
    source = OfflineCanonicalReplay(
        TRADE_DATE,
        expected_symbols,
        start_ms=start_ms,
        end_exclusive_ms=end_ms,
        slice_ms=3_000,
        session_timeline=ReplaySessionTimeline(manifest),
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("replay", start_ms, end_ms),)),
        ProbeStrategy(),
        session_id=TRADE_DATE + "-task007-canonical-replay",
        phase="REPLAY",
        result_history_limit=1,
    )
    capture = _CaptureEngine(engine)
    stats: dict[str, Any] = {
        "frame_rows": 0,
        "empty_frames": 0,
        "source_return_order_hash": semantic_hash(("TASK-007", shuffled)),
    }
    started = time.perf_counter()
    adapter = TDFrameAdapter()
    cursor = conn.cursor()
    source.replay(
        _batch_stream(
            cursor,
            adapter,
            start_ms=start_ms,
            end_ms=end_ms,
            frame_count=frame_count,
            shuffled=shuffled,
            stats=stats,
        ),
        capture,
        signal_prefix="task007-canonical",
    )
    auction_revisions = {}
    for tag in AUCTION_TAGS:
        policy_time = {
            "0920": "09:20:03",
            "0924": "09:24:10",
            "0925": "09:25:06",
        }[tag]
        revision = source.observe_auction(
            tag,
            auction_rows.get(tag, ()),
            evaluation_time_ms=local_datetime_ms(TRADE_DATE, policy_time),
            expected_symbols=expected_symbols,
            source_layers=("TD_SELECT",) if auction_error is None else ("TD_SELECT_UNAVAILABLE",),
        )
        auction_revisions[tag] = {
            "revision": revision.revision,
            "state": revision.state,
            "coverage": revision.coverage,
            "observed_count": len(revision.observed_symbols),
            "missing_count": len(revision.missing_symbols),
            "content_hash": revision.content_hash,
            "evidence_hash": revision.evidence_hash,
        }
    checkpoint = source.session_timeline.finalize()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    snapshot = source.session_timeline.snapshot()
    return {
        "shuffled": shuffled,
        "frame_count": source.session_timeline.frame_count,
        "processed_signals": capture.signal_count,
        "reducer_revision": engine._reducer.state.revision,
        "final_virtual_clock": source.source.clock.now_utc().isoformat(),
        "frame_rows": stats["frame_rows"],
        "empty_frames": stats["empty_frames"],
        "source_return_order_hash": stats["source_return_order_hash"],
        "auction_revisions": auction_revisions,
        "checkpoint_id": checkpoint.node_id,
        "session_snapshot": snapshot,
        "signal_hash": semantic_hash(tuple(capture.signal_hashes)),
        "session_content_hash": source.session_timeline.content_hash,
        "session_evidence_hash": source.session_timeline.evidence_hash,
        "elapsed_ms": elapsed_ms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-time", default=WINDOW_START)
    parser.add_argument("--end-time", default=WINDOW_END)
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--passes", choices=("ordered", "both"), default="ordered")
    parser.add_argument("--td-host", default=os.environ.get("TDENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--td-port", type=int, default=int(os.environ.get("TDENGINE_PORT", "6030")))
    parser.add_argument("--td-user", default=os.environ.get("TDENGINE_USER", "root"))
    parser.add_argument("--td-password", default=os.environ.get("TDENGINE_PASSWORD", "taosdata"))
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    args = parser.parse_args()
    output_dir: Path = args.output_dir
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("output directory must be new and empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    start_ms = local_datetime_ms(TRADE_DATE, args.start_time)
    configured_end = local_datetime_ms(TRADE_DATE, args.end_time)
    if args.max_frames is not None:
        if args.max_frames <= 0:
            raise SystemExit("max-frames must be positive")
        configured_end = min(configured_end, start_ms + args.max_frames * 3_000)
    if configured_end <= start_ms or (configured_end - start_ms) % 3_000:
        raise SystemExit("replay interval must contain whole 3-second frames")
    end_ms = configured_end
    frame_count = (end_ms - start_ms) // 3_000
    conn = _connect_td(args)
    try:
        cursor = conn.cursor()
        stock_schema = _read_schema(cursor, SOURCE_TABLE)
        auction_schema = _read_schema(cursor, AUCTION_TABLE)
        expected_symbols, event_count, count_sql, universe_sql = _read_universe_and_count(cursor, start_ms, end_ms)
        auction_rows, auction_error = _read_auction_rows(cursor, TRADE_DATE)
        query_hash = semantic_hash({
            "count_sql": count_sql,
            "universe_sql": universe_sql,
            "frame_sql": "SELECT tick fields FROM stock_tick_v2 WHERE ts >= frame_start AND ts < frame_end ORDER BY ts, symbol",
            "auction_sql": "SELECT auction fields FROM auction_snapshot_v2 WHERE trade_date = target AND auction_tag IN (0920,0924,0925)",
        })
        manifest = FrameManifestV1(
            trade_date=TRADE_DATE,
            source_timezone="Asia/Shanghai",
            start_ms=start_ms,
            end_exclusive_ms=end_ms,
            frame_interval_ms=3_000,
            expected_symbols=expected_symbols,
            frame_count=frame_count,
            event_count=event_count,
            source_table=SOURCE_TABLE,
            query_hash=query_hash,
            input_hash=semantic_hash((TRADE_DATE, start_ms, end_ms, event_count, expected_symbols)),
        )
        ordered = _run_pass(
            conn,
            expected_symbols=expected_symbols,
            start_ms=start_ms,
            end_ms=end_ms,
            frame_count=frame_count,
            manifest=manifest,
            auction_rows=auction_rows,
            auction_error=auction_error,
            shuffled=False,
        )
        shuffled = None
        if args.passes == "both":
            shuffled = _run_pass(
                conn,
                expected_symbols=expected_symbols,
                start_ms=start_ms,
                end_ms=end_ms,
                frame_count=frame_count,
                manifest=manifest,
                auction_rows=auction_rows,
                auction_error=auction_error,
                shuffled=True,
            )
    finally:
        conn.close()
    compare_keys = (
        "frame_count",
        "processed_signals",
        "reducer_revision",
        "final_virtual_clock",
        "signal_hash",
        "session_content_hash",
        "auction_revisions",
    )
    equal = None if shuffled is None else all(ordered[key] == shuffled[key] for key in compare_keys)
    if equal is False:
        status = "REPLAY_NON_DETERMINISTIC"
    elif auction_error is not None:
        status = "REPLAY_PARTIAL"
    else:
        status = "REPLAY_READY_BOUNDED"
    _write_json(output_dir / "manifest.json", asdict(manifest))
    _write_json(output_dir / "source_schema.json", {
        "stock_table": SOURCE_TABLE,
        "auction_table": AUCTION_TABLE,
        "stock_describe": stock_schema,
        "auction_describe": auction_schema,
        "selected_stock_fields": list(TICK_FIELDS),
        "selected_auction_fields": list(AUCTION_FIELDS),
        "timestamp_semantics": "TD source record timestamp; not Rabbit arrival time",
        "source_sequence_status": "UNKNOWN",
        "rabbit_arrival_order": "UNKNOWN",
        "historical_available_at": "UNKNOWN",
    })
    _write_json(output_dir / "inventory.json", {
        "trade_date": TRADE_DATE,
        "window": {"start": args.start_time, "end_exclusive": _local_time(end_ms), "timezone": "Asia/Shanghai"},
        "expected_symbol_count": len(expected_symbols),
        "event_count_from_count_query": event_count,
        "auction_row_counts": {tag: len(rows) for tag, rows in auction_rows.items()},
        "auction_error": auction_error,
        "query_hash": query_hash,
        "streaming_contract": "one SELECT per 3-second half-open frame; current frame discarded after submission",
    })
    performance = {
        "ordered_elapsed_ms": ordered["elapsed_ms"],
        "ordered_status": _performance_status(ordered["elapsed_ms"]),
        "thresholds_ms": {"pass": 300_000, "pass_with_warn": 600_000},
    }
    if shuffled is not None:
        performance.update({
            "shuffled_elapsed_ms": shuffled["elapsed_ms"],
            "shuffled_status": _performance_status(shuffled["elapsed_ms"]),
        })
    _write_json(output_dir / "engine_summary.json", {
        "status": status,
        "performance": performance,
        "ordered": ordered,
        "shuffled": shuffled,
    })
    _write_json(output_dir / "determinism_comparison.json", {
        "status": "NOT_RUN" if shuffled is None else ("PASS" if equal else "FAIL"),
        "equal": equal,
        "comparison_keys": list(compare_keys),
        "ordered": {key: ordered[key] for key in compare_keys},
        "shuffled": None if shuffled is None else {key: shuffled[key] for key in compare_keys},
        "shuffle_scope": "TD row order before canonical adapter; not Rabbit arrival-order evidence",
    })
    _write_json(output_dir / "auction_summary.json", {
        "status": "UNAVAILABLE" if auction_error else "OBSERVED",
        "row_counts": {tag: len(rows) for tag, rows in auction_rows.items()},
        "error": auction_error,
        "ordered": ordered["auction_revisions"],
    })
    _write_json(output_dir / "replay_summary.json", {
        "task_id": "TASK-007",
        "status": status,
        "trade_date": TRADE_DATE,
        "frame_count": frame_count,
        "passes": args.passes,
        "expected_symbol_count": len(expected_symbols),
        "event_count": event_count,
        "performance": performance,
        "side_effects": "NONE_OBSERVED",
        "production_equivalence": "UNPROVEN",
    })
    _write_json(output_dir / "side_effect_audit.json", {
        "status": "NONE_OBSERVED",
        "td_operations": ["DESCRIBE", "SELECT COUNT", "SELECT DISTINCT", "SELECT stock frames", "SELECT auction rows"],
        "td_write": False,
        "redis_operations": [],
        "redis_write": False,
        "rabbit_consume": False,
        "rabbit_ack_change": False,
        "service_restart": False,
        "effect_or_notification": False,
        "production_directory_write": False,
    })
    (output_dir / "unknowns_and_limits.md").write_text(
        "# TASK-007 real replay limits\n\n"
        "- TD source timestamps are preserved; Rabbit arrival order remains UNKNOWN.\n"
        "- historical available_at remains UNKNOWN.\n"
        "- This is read-only TD replay and does not prove production Rabbit/TD batch equivalence.\n"
        "- Auction rows are observed through TD SELECT only; Redis/Wencai recovery is not invoked.\n",
        encoding="utf-8",
    )
    _checksums(output_dir)
    print(canonical_json({
        "status": status,
        "output_dir": str(output_dir),
        "frame_count": frame_count,
        "event_count": event_count,
        "symbols": len(expected_symbols),
        "deterministic": equal,
        "auction_error": auction_error,
    }))
    return 0 if status in {"REPLAY_READY_BOUNDED", "REPLAY_PARTIAL"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
