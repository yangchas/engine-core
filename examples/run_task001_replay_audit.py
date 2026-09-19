"""Run TASK-001 as a bounded, read-only historical replay audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from engine_core import (  # noqa: E402
    DeterministicEngine,
    MarketStateReducer,
    ProbeStrategy,
    SignalKind,
    TDEventTimeReplaySource,
    VirtualClock,
    WindowManager,
    WindowSpec,
    canonical_json,
    local_datetime_ms,
    replay_td_event_time,
)
from engine_core.auction_projection import read_redis_auction_projection  # noqa: E402
from engine_core.q2 import FreshnessPolicy, RedisQ2ProjectionAdapter  # noqa: E402
from engine_core.contracts import EngineSignal  # noqa: E402

try:
    from examples.run_real_auction_shadow import build_shadow_from_rows  # type: ignore
except ModuleNotFoundError:
    from run_real_auction_shadow import build_shadow_from_rows  # type: ignore


TRADE_DATE = "2026-09-18"
WINDOW_START = "09:15:00"
WINDOW_END = "09:40:00"
TD_STOCK_FIELDS = (
    "ts", "px_milli", "pc_milli", "o_milli", "h_milli", "l_milli",
    "amt_yuan", "vol_units", "ap1_milli", "ap2_milli", "ap3_milli",
    "ap4_milli", "ap5_milli", "bp1_milli", "bp2_milli", "bp3_milli",
    "bp4_milli", "bp5_milli", "av1", "av2", "av3", "av4", "av5",
    "bv1", "bv2", "bv3", "bv4", "bv5", "inst_vol", "inst_amt_yuan",
    "large_net_yuan", "symbol",
)
TD_AUCTION_FIELDS = (
    "ts", "px_milli", "chg_bp", "match_amt_yuan", "rest_bid_amt_yuan",
    "rest_ask_amt_yuan", "limit_state", "symbol", "trade_date", "auction_tag",
)
SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def _sequence_hash(values: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(_jsonable(value)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _redact_log(line: str) -> str:
    return re.sub(
        r"(?i)((?:password|passwd|token|cookie|secret|authorization|auth)[=:])[^\s,]+",
        r"\1<REDACTED>",
        line,
    )


def _connect_td():
    import taos  # type: ignore[import-not-found]

    return taos.connect(
        host=os.environ.get("TDENGINE_HOST", "127.0.0.1"),
        port=int(os.environ.get("TDENGINE_PORT", "6030")),
        user=os.environ.get("TDENGINE_USER", "root"),
        password=os.environ.get("TDENGINE_PASSWORD", "taosdata"),
        database=os.environ.get("TDENGINE_DATABASE", "market_data1"),
    )


def _capture_stock_ticks(output_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    schema_rows: list[dict[str, Any]] = []
    rows_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    raw_path = output_dir / "stock_tick_v2_rows.jsonl"
    start_ms = local_datetime_ms(TRADE_DATE, WINDOW_START)
    end_ms = local_datetime_ms(TRADE_DATE, WINDOW_END)
    sql = (
        "SELECT " + ", ".join(TD_STOCK_FIELDS)
        + " FROM market_data1.stock_tick_v2 "
        + "WHERE ts >= '2026-09-18 09:15:00' AND ts < '2026-09-18 09:40:00' "
        + "ORDER BY ts, symbol"
    )
    conn = _connect_td()
    try:
        cursor = conn.cursor()
        cursor.execute("DESCRIBE market_data1.stock_tick_v2")
        for row in cursor.fetchall():
            schema_rows.append({"field": row[0], "type": row[1], "length": row[2], "note": row[4:]})
        cursor.execute(sql)
        count = 0
        min_ts: str | None = None
        max_ts: str | None = None
        symbols: set[str] = set()
        missing = Counter()
        with raw_path.open("w", encoding="utf-8") as raw_output:
            while True:
                batch = cursor.fetchmany(10000)
                if not batch:
                    break
                for row in batch:
                    item = {name: _jsonable(value) for name, value in zip(TD_STOCK_FIELDS, row)}
                    raw_output.write(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                    count += 1
                    symbol = str(item["symbol"])
                    symbols.add(symbol)
                    ts = str(item["ts"])
                    min_ts = ts if min_ts is None or ts < min_ts else min_ts
                    max_ts = ts if max_ts is None or ts > max_ts else max_ts
                    for field in ("ts", "px_milli", "pc_milli", "amt_yuan", "vol_units"):
                        if item.get(field) is None:
                            missing[field] += 1
                    rows_by_symbol[symbol].append({
                        "ts": item["ts"], "px_milli": item["px_milli"],
                        "pc_milli": item["pc_milli"], "amt_yuan": item["amt_yuan"],
                        "vol_units": item["vol_units"], "symbol": symbol,
                        "raw_fields": item,
                    })
    finally:
        conn.close()
    raw_file_hash = _sha256(raw_path)
    _write_json(output_dir / "source_schema.json", {
        "table": "market_data1.stock_tick_v2",
        "selected_fields": list(TD_STOCK_FIELDS),
        "describe": schema_rows,
        "timestamp_semantics": "TD source record timestamp; not Rabbit arrival time",
        "source_sequence": "NOT_PRESENT_IN_SELECTED_SCHEMA",
        "input_file": raw_path.name,
    })
    return rows_by_symbol, {
        "table": "market_data1.stock_tick_v2",
        "query": sql,
        "window_start_ms": start_ms,
        "window_end_exclusive_ms": end_ms,
        "row_count": count,
        "symbol_count": len(symbols),
        "first_source_timestamp": min_ts,
        "last_source_timestamp": max_ts,
        "missing_required_field_counts": dict(sorted(missing.items())),
        "source_sequence": "UNKNOWN/UNAVAILABLE",
        "arrival_order": "UNKNOWN; query order is not Rabbit arrival order",
        "raw_file_sha256": raw_file_hash,
    }


class _RedisCapture:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.reads: list[dict[str, Any]] = []

    def smembers(self, key: str):
        value = sorted(self.client.smembers(key))
        self.reads.append({"operation": "SMEMBERS", "key": key, "value": value})
        return value

    def hgetall(self, key: str):
        value = dict(self.client.hgetall(key))
        self.reads.append({"operation": "HGETALL", "key": key, "value": value})
        return value


def _capture_redis(output_dir: Path) -> dict[str, Any]:
    import redis  # type: ignore[import-not-found]

    client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=10,
    )
    observed_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    q2_capture = _RedisCapture(client)
    try:
        q2_projection = RedisQ2ProjectionAdapter(q2_capture).read(
            TRADE_DATE,
            datetime.fromtimestamp(observed_at_ms / 1000, tz=timezone.utc),
            freshness_policy=FreshnessPolicy(stale_after_ms=600000),
        )
        auction_raw: dict[str, Any] = {}
        for tag in ("0920", "0924", "0925"):
            key = f"market:auction:20260918:{tag}"
            auction_raw[key] = dict(client.hgetall(key) or {})
    finally:
        client.close()

    class _FrozenAuctionClient:
        def hgetall(self, key: str):
            return auction_raw.get(key, {})

    auction_projections = read_redis_auction_projection(
        _FrozenAuctionClient(),
        trade_date=TRADE_DATE,
        observed_at_ms=observed_at_ms,
        tags=("0920", "0924", "0925"),
    )
    _write_json(output_dir / "redis_q2_capture.json", {
        "observed_at_ms": observed_at_ms,
        "reads": q2_capture.reads,
        "projection": {
            "trade_date": q2_projection.trade_date,
            "expected_symbols": q2_projection.expected_symbols,
            "missing_symbols": q2_projection.missing_symbols,
            "stale_symbols": q2_projection.stale_symbols,
            "coverage": q2_projection.coverage,
            "status": q2_projection.status.value,
            "consistency_status": q2_projection.consistency_status,
            "oldest_source_time_ms": q2_projection.oldest_source_time_ms,
            "newest_source_time_ms": q2_projection.newest_source_time_ms,
            "quotes": {symbol: quote.to_mapping() for symbol, quote in q2_projection.quotes.items()},
            "content_hash": q2_projection.content_hash,
        },
        "read_only": True,
        "side_effect_boundary": "SMEMBERS/HGETALL only",
    })
    _write_json(output_dir / "redis_auction_capture.json", {
        "observed_at_ms": observed_at_ms,
        "raw_hashes": auction_raw,
        "projections": [item.as_mapping() for item in auction_projections],
        "read_only": True,
        "side_effect_boundary": "HGETALL only",
    })
    return {
        "q2": {
            "active_read_count": sum(item["operation"] == "SMEMBERS" for item in q2_capture.reads),
            "hash_read_count": sum(item["operation"] == "HGETALL" for item in q2_capture.reads),
            "expected_symbols": len(q2_projection.expected_symbols),
            "quote_count": len(q2_projection.quotes),
            "coverage": q2_projection.coverage,
            "status": q2_projection.status.value,
            "consistency_status": q2_projection.consistency_status,
            "oldest_source_time_ms": q2_projection.oldest_source_time_ms,
            "newest_source_time_ms": q2_projection.newest_source_time_ms,
            "content_hash": q2_projection.content_hash,
            "available_at": "UNKNOWN",
        },
        "auction_projection": [
            {
                "tag": item.tag,
                "status": item.status,
                "scope": item.scope,
                "row_count": item.row_count,
                "content_hash": item.content_hash,
                "evidence_hash": item.evidence_hash,
                "requested_symbol_scope": "ALL_ROWS_RETURNED; TOP_AMOUNT projection",
            }
            for item in auction_projections
        ],
        "observed_at_ms": observed_at_ms,
        "historical_available_at": "UNKNOWN",
    }


def _capture_auction_td(output_dir: Path) -> dict[str, Any]:
    raw_path = output_dir / "auction_snapshot_v2_rows.jsonl"
    conn = _connect_td()
    count = 0
    tags = Counter()
    symbols: set[str] = set()
    rows_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sql = (
        "SELECT " + ", ".join(TD_AUCTION_FIELDS)
        + " FROM market_data1.auction_snapshot_v2 "
        + 'WHERE trade_date="20260918" AND auction_tag IN ("0920","0924","0925") ORDER BY ts, symbol'
    )
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        with raw_path.open("w", encoding="utf-8") as output:
            while True:
                batch = cursor.fetchmany(10000)
                if not batch:
                    break
                for row in batch:
                    source_item = dict(zip(TD_AUCTION_FIELDS, row))
                    item = {name: _jsonable(value) for name, value in source_item.items()}
                    output.write(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                    count += 1
                    tag = str(item.get("auction_tag") or "")
                    symbol = str(item.get("symbol") or "")
                    tags[tag] += 1
                    symbols.add(symbol)
                    rows_by_symbol[symbol].append(source_item)
    finally:
        conn.close()
    shadow_count = 0
    shadow_status = Counter()
    shadow_errors: list[dict[str, Any]] = []
    for symbol in sorted(rows_by_symbol):
        by_tag = {str(row.get("auction_tag")): row for row in rows_by_symbol[symbol]}
        if not {"0920", "0924", "0925"}.issubset(by_tag):
            continue
        try:
            shadow = build_shadow_from_rows(
                [by_tag["0920"], by_tag["0924"], by_tag["0925"]],
                trade_date=TRADE_DATE,
                symbol=symbol,
            )
            shadow_count += 1
            shadow_status[str(shadow["shadow"].get("fact_status", "UNKNOWN"))] += 1
        except Exception as exc:
            shadow_errors.append({"symbol": symbol, "error": type(exc).__name__ + ": " + str(exc)})
    raw_file_hash = _sha256(raw_path)
    return {
        "table": "market_data1.auction_snapshot_v2",
        "query": sql,
        "row_count": count,
        "symbol_count": len(symbols),
        "tag_counts": dict(sorted(tags.items())),
        "symbols_with_all_three_tags": shadow_count,
        "shadow_status_counts": dict(sorted(shadow_status.items())),
        "shadow_errors": shadow_errors[:50],
        "raw_file": raw_path.name,
        "raw_file_sha256": raw_file_hash,
        "source_arrival_order": "UNKNOWN",
    }


def _replay_one(rows_by_symbol: Mapping[str, list[dict[str, Any]]], *, shuffled: bool) -> dict[str, Any]:
    anchor_ms = local_datetime_ms(TRADE_DATE, WINDOW_START)
    end_ms = local_datetime_ms(TRADE_DATE, WINDOW_END)
    per_symbol: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    for symbol in sorted(rows_by_symbol):
        rows = list(rows_by_symbol[symbol])
        if shuffled:
            random.Random("TASK-001:" + symbol).shuffle(rows)
        try:
            clock = VirtualClock(datetime.fromtimestamp(anchor_ms / 1000, tz=timezone.utc))
            source = TDEventTimeReplaySource(
                TRADE_DATE,
                (symbol,),
                clock,
                slice_anchor_ms=anchor_ms,
                source_timezone=SHANGHAI,
                source_id="task001_td_event_time_replay",
            )
            slices = source.event_slices(rows)
            signals = source.signals_for(rows, signal_prefix="task001-td-event")
            engine = DeterministicEngine(
                MarketStateReducer(),
                WindowManager((WindowSpec("replay", anchor_ms, end_ms),)),
                ProbeStrategy(),
                session_id=TRADE_DATE + ":" + symbol,
                phase="REPLAY",
                result_history_limit=1,
            )
            replay_td_event_time(rows, source, engine, signal_prefix="task001-td-event")
            final_signal = EngineSignal(
                signal_id="task001-final:" + symbol,
                logical_time_ms=end_ms,
                signal_seq=len(signals) + 1,
                signal_kind=SignalKind.TIMER,
                payload={"trigger_id": "TASK001_FINAL", "close_windows": ("replay",)},
            )
            source.advance_before_consume(final_signal)
            engine.submit(final_signal)
            result = engine.run_until_empty()
            final_snapshot_hash = result.snapshots[-1].content_hash if result.snapshots else None
            final_strategy_hash = result.strategy_results[-1].content_hash if result.strategy_results else None
            per_symbol[symbol] = {
                "row_count": len(rows),
                "event_hash": _sequence_hash(event.content_hash for item in slices for event in item.events),
                "slice_hash": _sequence_hash(item.content_hash for item in slices),
                "signal_hash": _sequence_hash({"id": item.signal_id, "time": item.logical_time_ms, "payload": item.payload.content_hash} for item in signals),
                "projection_hash": _sequence_hash(item.payload.content_hash for item in signals),
                "processed_signals": result.processed_signals,
                "reducer_revision": result.snapshots[-1].market_state_revision if result.snapshots else None,
                "final_state_hash": final_snapshot_hash,
                "strategy_result_hash": final_strategy_hash,
                "virtual_clock": clock.now_utc().isoformat(),
            }
        except Exception as exc:
            failures.append({"symbol": symbol, "error": type(exc).__name__ + ": " + str(exc)})
    return {
        "symbol_count": len(per_symbol),
        "failed_symbol_count": len(failures),
        "failures": failures[:50],
        "rows": sum(item["row_count"] for item in per_symbol.values()),
        "processed_signals": sum(item["processed_signals"] for item in per_symbol.values()),
        "reducer_revision": sum(item["reducer_revision"] or 0 for item in per_symbol.values()),
        "event_hash": _sequence_hash({"symbol": symbol, "hash": per_symbol[symbol]["event_hash"]} for symbol in sorted(per_symbol)),
        "slice_hash": _sequence_hash({"symbol": symbol, "hash": per_symbol[symbol]["slice_hash"]} for symbol in sorted(per_symbol)),
        "signal_hash": _sequence_hash({"symbol": symbol, "hash": per_symbol[symbol]["signal_hash"]} for symbol in sorted(per_symbol)),
        "projection_hash": _sequence_hash({"symbol": symbol, "hash": per_symbol[symbol]["projection_hash"]} for symbol in sorted(per_symbol)),
        "final_state_hash": _sequence_hash({"symbol": symbol, "hash": per_symbol[symbol]["final_state_hash"]} for symbol in sorted(per_symbol)),
        "strategy_result_hash": _sequence_hash({"symbol": symbol, "hash": per_symbol[symbol]["strategy_result_hash"]} for symbol in sorted(per_symbol)),
        "virtual_clock": max((per_symbol[symbol]["virtual_clock"] for symbol in per_symbol), default=None),
        "per_symbol": per_symbol,
        "replay_scope": "all returned rows, replayed one symbol per in-memory Engine; no cross-symbol production equivalence claimed",
    }


def _capture_logs(output_dir: Path) -> dict[str, Any]:
    lines: list[str] = []
    command_status: dict[str, Any] = {}
    for service in ("engine-next", "t1-v2-live"):
        command = [
            "journalctl", "-u", service, "--since", "2026-09-18 00:00:00",
            "--until", "2026-09-19 00:00:00", "--no-pager", "--output=short-iso",
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            command_status[service] = {"returncode": completed.returncode, "line_count": len(completed.stdout.splitlines())}
            for line in completed.stdout.splitlines():
                if re.search(r"progress|last_ts_ms|batches|source|timestamp|tdengine|commit", line, re.I):
                    lines.append(service + " " + _redact_log(line))
        except Exception as exc:
            command_status[service] = {"error": type(exc).__name__}
    path = output_dir / "production_log_inventory.txt"
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return {"services": command_status, "filtered_line_count": len(lines), "file_sha256": _sha256(path), "credentials_redacted": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    rows_by_symbol, stock_inventory = _capture_stock_ticks(output_dir)
    auction_inventory = _capture_auction_td(output_dir)
    redis_inventory = _capture_redis(output_dir)
    logs_inventory = _capture_logs(output_dir)
    ordered = _replay_one(rows_by_symbol, shuffled=False)
    shuffled = _replay_one(rows_by_symbol, shuffled=True)
    keys = ("event_hash", "slice_hash", "signal_hash", "projection_hash", "final_state_hash", "strategy_result_hash", "processed_signals", "reducer_revision")
    determinism = {
        "ordered": {key: ordered.get(key) for key in (*keys, "virtual_clock")},
        "shuffled": {key: shuffled.get(key) for key in (*keys, "virtual_clock")},
        "equal": all(ordered.get(key) == shuffled.get(key) for key in keys),
        "ordering_contract": "event_time + symbol + raw content hash; synthetic tie-break is not Rabbit arrival order",
    }
    _write_json(output_dir / "determinism_comparison.json", determinism)
    _write_json(output_dir / "inventory.json", {"stock_tick": stock_inventory, "auction_td": auction_inventory, "redis": redis_inventory, "logs": logs_inventory})
    _write_json(output_dir / "engine_summary.json", {"ordered": ordered, "shuffled": shuffled, "determinism": determinism})
    overall = "REPLAY_READY_BOUNDED" if determinism["equal"] and not ordered["failures"] else "REPLAY_NON_DETERMINISTIC" if not determinism["equal"] else "REPLAY_PARTIAL"
    _write_json(output_dir / "replay_summary.json", {
        "task_id": "TASK-001",
        "status": overall,
        "trade_date": TRADE_DATE,
        "window": {"start": WINDOW_START, "end_exclusive": WINDOW_END, "timezone": "Asia/Shanghai"},
        "stock_tick": {"row_count": stock_inventory["row_count"], "symbol_count": stock_inventory["symbol_count"]},
        "auction_0920_0924_0925": auction_inventory,
        "continuous_0932": {"status": "PARTIAL", "reason": "No historical available_at proof; no synthetic 0932 input"},
        "historical_available_at": "UNKNOWN",
        "side_effects": "NONE_OBSERVED",
    })
    (output_dir / "unknowns_and_limits.md").write_text(
        "# TASK-001 unknowns and limits\n\n"
        "- TD timestamps are source record times; they do not recover Rabbit arrival or batch order.\n"
        "- Historical `available_at` is UNKNOWN; observed/query completion time is not substituted.\n"
        "- Redis Q2 is a read cohort and auction Redis data is TOP_AMOUNT projection scope, not full-market authority.\n"
        "- Stock ticks cover all rows returned by the bounded query, replayed one symbol per in-memory Engine; this does not prove cross-symbol production equivalence.\n"
        "- 0932 continuous-session replay is PARTIAL because no historical cutoff-safe input was fabricated.\n",
        encoding="utf-8",
    )
    (output_dir / "replay_audit.md").write_text(
        f"# TASK-001 replay audit\n\nStatus: `{overall}`\n\n"
        f"Stock tick rows: `{stock_inventory['row_count']}` across `{stock_inventory['symbol_count']}` symbols.\n\n"
        f"Ordered/shuffled deterministic equality: `{determinism['equal']}`.\n\n"
        "This is a bounded event-time replay audit, not NORMAL production evidence and not Rabbit arrival-order equivalence.\n",
        encoding="utf-8",
    )
    _write_json(output_dir / "side_effect_audit.json", {
        "status": "NONE_OBSERVED",
        "td_operations": ["DESCRIBE", "SELECT"],
        "redis_operations": ["SMEMBERS", "HGETALL"],
        "rabbit_consume": False,
        "rabbit_ack_change": False,
        "redis_write": False,
        "td_write": False,
        "service_restart": False,
        "effect_or_notification": False,
        "production_directory_write": False,
    })
    checksums = []
    for path in sorted(output_dir.iterdir()):
        if path.name == "sha256sums.txt" or not path.is_file():
            continue
        checksums.append(f"{_sha256(path)}  {path.name}")
    (output_dir / "sha256sums.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    handoff = ROOT / "docs/work/handoffs/TASK-001-replay-investigator.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        f"# TASK-001 replay-investigator handoff\n\n- status: `{overall}`\n- validation_dir: `{output_dir}`\n- owner: `replay-investigator`\n- branch: `codex/task-real-data-replay-20260918`\n- worktree: `../engine-core-replay-20260918`\n- deterministic_ordered_vs_shuffled: `{determinism['equal']}`\n- side_effects: `NONE_OBSERVED`\n\nTester and auditor review are still required; this task is not MERGED.\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": overall, "output_dir": str(output_dir), "stock_rows": stock_inventory["row_count"], "symbols": stock_inventory["symbol_count"], "deterministic": determinism["equal"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
