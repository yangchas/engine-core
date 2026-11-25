"""Compare the live opening node with Core replay on the same pinned Q2Frame.

This is an offline, read-only parity runner. It reconstructs one Q2 projection
through ``Q2FrameReplaySource`` and invokes the existing
``run_live_morning_shadow.build_node_evidence`` composition for OPENING_0932.
It never connects to Redis, TDengine, RabbitMQ, or a production service.

Calendar input is local session configuration for timer calculation only; it
is not presented as observed market data. Differences are reported, not used
as a readiness gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

try:
    import resource
except ImportError:  # pragma: no cover - Windows control-test environment
    resource = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    FreshnessPolicy,
    Q2FrameReplaySource,
    Q2FrameV1,
    VirtualClock,
    build_a_share_session_plan,
    build_calendar_snapshot,
    canonical_json,
    due_timer_firings,
    semantic_hash,
)

try:  # Script execution resolves sibling examples directly.
    from task008_parity_checkpoint import (
        BatchCheckpoint,
        aggregate_row_hash as _aggregate_row_hash,
        row_hash as _row_hash,
        write_bytes_atomic_exclusive as _write_bytes_atomic_exclusive,
        write_json_atomic_exclusive as _write_json_atomic_exclusive,
    )
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.task008_parity_checkpoint import (
        BatchCheckpoint,
        aggregate_row_hash as _aggregate_row_hash,
        row_hash as _row_hash,
        write_bytes_atomic_exclusive as _write_bytes_atomic_exclusive,
        write_json_atomic_exclusive as _write_json_atomic_exclusive,
    )

try:  # Script execution resolves sibling examples directly.
    from run_live_morning_shadow import (
        CORE_TIMER_SPECS,
        OPENING_0932_EVALUATION_DELAY_MS,
        build_node_evidence,
    )
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.run_live_morning_shadow import (
        CORE_TIMER_SPECS,
        OPENING_0932_EVALUATION_DELAY_MS,
        build_node_evidence,
    )


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DETAIL_LIMIT = 100


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def compare_opening_fact_maps(
    core_facts: Mapping[str, Any],
    live_facts: Mapping[str, Any],
    *,
    detail_limit: int = DETAIL_LIMIT,
) -> dict[str, Any]:
    """Compare per-symbol opening facts without turning differences into gates.

    Explicit ``None`` is a present value and is kept distinct from an omitted
    field. Canonical JSON comparison also distinguishes integers from floats.
    This function is used with real reports by the runner; its unit tests use
    clearly synthetic mappings only to validate comparison bookkeeping.
    """

    if not isinstance(core_facts, Mapping) or not isinstance(live_facts, Mapping):
        raise TypeError("fact maps must be mappings")
    if isinstance(detail_limit, bool) or not isinstance(detail_limit, int) or detail_limit < 0:
        raise ValueError("detail_limit must be a non-negative integer")

    core_symbols = set(core_facts)
    live_symbols = set(live_facts)
    shared = core_symbols & live_symbols
    fields = sorted(
        {
            str(field_name)
            for symbol in shared
            for source in (core_facts[symbol], live_facts[symbol])
            if isinstance(source, Mapping)
            for field_name in source
        }
    )
    field_counts: dict[str, dict[str, int]] = {
        field_name: {
            "equal": 0,
            "different": 0,
            "missing_core_field": 0,
            "missing_live_field": 0,
        }
        for field_name in fields
    }
    differences: list[dict[str, Any]] = []
    difference_count = 0
    difference_symbols: set[str] = set()

    def add_difference(item: dict[str, Any]) -> None:
        nonlocal difference_count
        difference_count += 1
        if item.get("symbol") is not None:
            difference_symbols.add(str(item["symbol"]))
        if len(differences) < detail_limit:
            differences.append(item)

    for symbol in sorted(core_symbols | live_symbols):
        core_present = symbol in core_facts
        live_present = symbol in live_facts
        if not core_present or not live_present:
            add_difference(
                {
                    "symbol": str(symbol),
                    "kind": (
                        "SYMBOL_MISSING_ON_CORE"
                        if not core_present
                        else "SYMBOL_MISSING_ON_LIVE"
                    ),
                    "explanation": "UNKNOWN",
                }
            )
            continue
        core_fact = core_facts[symbol]
        live_fact = live_facts[symbol]
        if not isinstance(core_fact, Mapping) or not isinstance(live_fact, Mapping):
            add_difference(
                {
                    "symbol": str(symbol),
                    "kind": "FACT_NOT_A_MAPPING",
                    "core_type": type(core_fact).__name__,
                    "live_type": type(live_fact).__name__,
                    "explanation": "UNKNOWN",
                }
            )
            continue
        for field_name in fields:
            counts = field_counts[field_name]
            has_core = field_name in core_fact
            has_live = field_name in live_fact
            if not has_core:
                counts["missing_core_field"] += 1
            if not has_live:
                counts["missing_live_field"] += 1
            if not has_core or not has_live:
                if has_core != has_live:
                    add_difference(
                        {
                            "symbol": str(symbol),
                            "field": field_name,
                            "kind": (
                                "FIELD_MISSING_ON_LIVE"
                                if has_core
                                else "FIELD_MISSING_ON_CORE"
                            ),
                            "core_value": core_fact.get(field_name),
                            "live_value": live_fact.get(field_name),
                            "explanation": "UNKNOWN",
                        }
                    )
                continue
            if canonical_json(core_fact[field_name]) == canonical_json(live_fact[field_name]):
                counts["equal"] += 1
            else:
                counts["different"] += 1
                add_difference(
                    {
                        "symbol": str(symbol),
                        "field": field_name,
                        "kind": "VALUE_MISMATCH",
                        "core_value": core_fact[field_name],
                        "live_value": live_fact[field_name],
                        "explanation": "UNKNOWN",
                    }
                )

    return {
        "comparison_status": "MATCH" if difference_count == 0 else "DIFFERENCES",
        "symbol_counts": {
            "core_symbol_count": len(core_symbols),
            "live_symbol_count": len(live_symbols),
            "shared_symbol_count": len(shared),
            "core_only_symbol_count": len(core_symbols - live_symbols),
            "live_only_symbol_count": len(live_symbols - core_symbols),
        },
        "field_counts": field_counts,
        "difference_count": difference_count,
        "difference_symbols": sorted(difference_symbols),
        "difference_symbol_count": len(difference_symbols),
        "difference_symbols_hash": semantic_hash(tuple(sorted(difference_symbols))),
        "difference_details": differences,
        "difference_details_limit": detail_limit,
        "difference_details_truncated": difference_count > len(differences),
    }


def _opening_facts_from_live_node(node: Mapping[str, Any]) -> tuple[
    dict[str, Mapping[str, Any]], dict[str, str], dict[str, Mapping[str, Any]]
]:
    facts: dict[str, Mapping[str, Any]] = {}
    fact_statuses: dict[str, str] = {}
    field_statuses: dict[str, Mapping[str, Any]] = {}
    rows = node.get("fact_dispatch")
    if not isinstance(rows, (tuple, list)):
        raise ValueError("live node fact_dispatch is missing")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "")
        engine = row.get("engine_shadow")
        result = engine.get("result") if isinstance(engine, Mapping) else None
        strategy = result.get("strategy_result") if isinstance(result, Mapping) else None
        trace = strategy.get("trace") if isinstance(strategy, Mapping) else None
        if not symbol or not isinstance(trace, Mapping):
            continue
        fact = trace.get("opening_fact")
        if isinstance(fact, Mapping):
            facts[symbol] = fact
        fact_statuses[symbol] = str(trace.get("fact_status") or "UNKNOWN")
        field_status = trace.get("opening_fact_field_status")
        if isinstance(field_status, Mapping):
            field_statuses[symbol] = field_status
    return facts, fact_statuses, field_statuses


def _read_projection_through_cutoff(
    q2frame_path: Path,
    *,
    trade_date: str,
    expected_symbols: tuple[str, ...],
    evaluation_time_ms: int,
) -> tuple[Any, dict[str, Any]]:
    source = None
    projection = None
    frame_count = update_count = 0
    first_frame_time_ms = last_frame_time_ms = None

    with q2frame_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                raise ValueError(f"Q2Frame row {line_number} must be an object")
            frame = Q2FrameV1.from_mapping(raw)
            if frame.logical_ts_ms > evaluation_time_ms:
                break
            if source is None:
                first_frame_time_ms = frame.logical_ts_ms
                initial_time = datetime.fromtimestamp(
                    frame.logical_ts_ms / 1000.0, tz=timezone.utc
                )
                source = Q2FrameReplaySource(
                    trade_date,
                    expected_symbols,
                    VirtualClock(initial_time),
                    freshness_policy=FreshnessPolicy(
                        stale_after_ms=60_000,
                        max_future_skew_ms=999,
                    ),
                    source_id="t1_v2_q2frame",
                )
            projection = source.apply(frame)
            frame_count += 1
            update_count += len(frame.q2_updates)
            last_frame_time_ms = frame.logical_ts_ms

    if projection is None or source is None:
        raise ValueError("Q2Frame has no rows at or before the opening observation")
    return projection, {
        "frame_count": frame_count,
        "update_count": update_count,
        "first_frame_time_ms": first_frame_time_ms,
        "last_frame_time_ms": last_frame_time_ms,
        "last_seq_no": source.last_seq_no,
        "evaluation_time_ms": evaluation_time_ms,
        "reached_evaluation_frame": last_frame_time_ms == evaluation_time_ms,
        "freshness_policy": {
            "stale_after_ms": 60_000,
            "max_future_skew_ms": 999,
        },
    }


def _build_calendar_for_session_config(trade_date: str) -> Any:
    """Build a one-day schedule input, explicitly not a real calendar claim."""

    return build_calendar_snapshot(
        (trade_date,),
        version="task008-local-session-config-v1",
        declared_valid_from=trade_date,
        declared_valid_to=trade_date,
        source_guard_valid_from=trade_date,
        source_guard_valid_to=trade_date,
        source_id="LOCAL_CONFIGURATION_NOT_OBSERVED_CALENDAR_DATA",
    )


def _node_timer_firing(trade_date: str, evaluation_time_ms: int) -> tuple[Any, Any]:
    calendar = _build_calendar_for_session_config(trade_date)
    plan = build_a_share_session_plan(trade_date, calendar)
    firings = due_timer_firings(
        plan,
        CORE_TIMER_SPECS,
        previous_time_ms=evaluation_time_ms - OPENING_0932_EVALUATION_DELAY_MS - 1,
        current_time_ms=evaluation_time_ms,
    )
    opening = tuple(item for item in firings if item.timer_id == "OPENING_0932")
    if len(opening) != 1:
        raise ValueError("opening evaluation time did not produce one OPENING_0932 timer")
    return calendar, opening[0]


def _run_live_node(
    firing: Any,
    *,
    observed_at: datetime,
    trade_date: str,
    symbols: tuple[str, ...],
    projection: Any,
) -> dict[str, Any]:
    return build_node_evidence(
        firing,
        observed_at=observed_at,
        trade_date=trade_date,
        symbols=symbols,
        td_rows_by_symbol={symbol: () for symbol in symbols},
        projection=projection,
        node_readiness={
            "status": "NOT_REASSESSED_BY_OFFLINE_PARITY_RUNNER",
            "source": "same reconstructed Q2Frame projection",
        },
    )


def _runner_error_row(symbol: str, exc: BaseException | str) -> dict[str, Any]:
    error_type = (
        str(exc)
        if isinstance(exc, str)
        else f"{type(exc).__module__}.{type(exc).__name__}"
    )
    row = {
        "symbol": symbol,
        "row_outcome": "RUNNER_ERROR",
        "fact_status": "UNKNOWN",
        "opening_fact": None,
        "field_status": None,
        "engine_status": "RUNNER_ERROR",
        "engine_error_type": error_type,
    }
    row["row_hash"] = _row_hash(row)
    return row


def _current_max_rss_kib() -> int | None:
    try:
        if resource is None:
            return None
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (AttributeError, OSError, ValueError):
        return None


def _rows_from_live_node(node: Mapping[str, Any], symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
    raw_rows = node.get("fact_dispatch")
    if not isinstance(raw_rows, (tuple, list)):
        return {symbol: _runner_error_row(symbol, "MISSING_FACT_DISPATCH") for symbol in symbols}

    indexed: dict[str, Mapping[str, Any]] = {}
    duplicates: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol") or "")
        if not symbol:
            continue
        if symbol in indexed:
            duplicates.add(symbol)
        indexed[symbol] = raw

    result: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        if symbol in duplicates:
            result[symbol] = _runner_error_row(symbol, "DUPLICATE_FACT_DISPATCH")
            continue
        raw = indexed.get(symbol)
        if raw is None:
            result[symbol] = _runner_error_row(symbol, "MISSING_FACT_DISPATCH_ROW")
            continue
        engine = raw.get("engine_shadow")
        engine_status = str(engine.get("status") or "UNKNOWN") if isinstance(engine, Mapping) else "UNKNOWN"
        engine_error_type = (
            str(engine.get("error_type"))
            if isinstance(engine, Mapping) and engine.get("error_type") is not None
            else None
        )
        engine_result = engine.get("result") if isinstance(engine, Mapping) else None
        strategy = (
            engine_result.get("strategy_result")
            if isinstance(engine_result, Mapping)
            else None
        )
        trace = strategy.get("trace") if isinstance(strategy, Mapping) else None
        if not isinstance(trace, Mapping):
            outcome = "LIVE_UNAVAILABLE" if engine_status == "UNAVAILABLE" else "RUNNER_ERROR"
            row = {
                "symbol": symbol,
                "row_outcome": outcome,
                "fact_status": "UNAVAILABLE" if outcome == "LIVE_UNAVAILABLE" else "UNKNOWN",
                "opening_fact": None,
                "field_status": None,
                "engine_status": engine_status,
                "engine_error_type": engine_error_type,
            }
            if outcome == "RUNNER_ERROR" and row["engine_error_type"] is None:
                row["engine_error_type"] = "MISSING_OPENING_TRACE"
        else:
            fact = trace.get("opening_fact")
            field_status = trace.get("opening_fact_field_status")
            outcome = "LIVE_FACT" if isinstance(fact, Mapping) else "NO_OPENING_FACT"
            row = {
                "symbol": symbol,
                "row_outcome": outcome,
                "fact_status": str(trace.get("fact_status") or "UNKNOWN"),
                "opening_fact": dict(fact) if isinstance(fact, Mapping) else None,
                "field_status": (
                    dict(field_status) if isinstance(field_status, Mapping) else None
                ),
                "engine_status": engine_status,
                "engine_error_type": engine_error_type,
            }
        row["row_hash"] = _row_hash(row)
        result[symbol] = row
    return result


def _run_one_node_batch(
    firing: Any,
    *,
    observed_at: datetime,
    trade_date: str,
    symbols: tuple[str, ...],
    projection: Any,
) -> tuple[dict[str, dict[str, Any]], int, tuple[str, ...]]:
    """Build a batch, falling back to isolated symbols if the batch call raises."""

    batch_error_types: set[str] = set()
    try:
        node = _run_live_node(
            firing,
            observed_at=observed_at,
            trade_date=trade_date,
            symbols=symbols,
            projection=projection,
        )
    except (MemoryError, OSError):
        # Resource and local I/O failures are transient; never checkpoint them
        # as durable per-symbol market outcomes.
        raise
    except Exception as batch_exc:
        batch_error_types.add(f"{type(batch_exc).__module__}.{type(batch_exc).__name__}")
        isolated: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            try:
                node = _run_live_node(
                    firing,
                    observed_at=observed_at,
                    trade_date=trade_date,
                    symbols=(symbol,),
                    projection=projection,
                )
            except (MemoryError, OSError):
                raise
            except Exception as exc:
                batch_error_types.add(f"{type(exc).__module__}.{type(exc).__name__}")
                isolated[symbol] = _runner_error_row(symbol, exc)
            else:
                isolated.update(_rows_from_live_node(node, (symbol,)))
        return isolated, len(symbols), tuple(sorted(batch_error_types))
    return _rows_from_live_node(node, symbols), 0, ()


def _run_live_node_rows_in_batches(
    firing: Any,
    *,
    observed_at: datetime,
    trade_date: str,
    symbols: Sequence[str],
    projection: Any,
    batch_size: int,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run all symbols through the unchanged live builder in deterministic batches."""

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    ordered = tuple(sorted(str(symbol) for symbol in symbols))
    if len(set(ordered)) != len(ordered):
        raise ValueError("symbols must be unique")
    rows: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    batch_count = (len(ordered) + batch_size - 1) // batch_size
    for batch_index, start in enumerate(range(0, len(ordered), batch_size)):
        batch = ordered[start : start + batch_size]
        batch_started = time.monotonic()
        batch_rows, fallback_count, batch_error_types = _run_one_node_batch(
            firing,
            observed_at=observed_at,
            trade_date=trade_date,
            symbols=batch,
            projection=projection,
        )
        if tuple(sorted(batch_rows)) != batch:
            raise ValueError("live node batch did not account for every requested symbol")
        rows.update(batch_rows)
        if progress is not None:
            progress(
                {
                    "event": "BATCH_COMPLETE",
                    "phase": "UNCHECKPOINTED_SYNTHETIC_CONTROL",
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "completed_symbols": len(rows),
                    "expected_symbols": len(ordered),
                    "batch_elapsed_seconds": round(time.monotonic() - batch_started, 3),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "batch_fallback_symbol_count": fallback_count,
                    "batch_error_types": list(batch_error_types),
                }
            )
    if tuple(sorted(rows)) != ordered:
        raise ValueError("batched live node run omitted expected symbols")
    return rows


def _execute_checkpointed_phase(
    checkpoint: BatchCheckpoint,
    *,
    firing: Any,
    observed_at: datetime,
    trade_date: str,
    projection: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    ordered = checkpoint.expected_symbols
    rows = dict(checkpoint.completed_rows)
    total_batches = (len(ordered) + checkpoint.batch_size - 1) // checkpoint.batch_size
    phase_started = time.monotonic()
    for batch_index in range(checkpoint.batch_count, total_batches):
        start = batch_index * checkpoint.batch_size
        batch = ordered[start : start + checkpoint.batch_size]
        batch_started = time.monotonic()
        batch_rows, fallback_count, batch_error_types = _run_one_node_batch(
            firing,
            observed_at=observed_at,
            trade_date=trade_date,
            symbols=batch,
            projection=projection,
        )
        if tuple(sorted(batch_rows)) != batch:
            raise ValueError("checkpoint batch failed to account for every requested symbol")
        batch_elapsed_seconds = time.monotonic() - batch_started
        max_rss_kib = _current_max_rss_kib()
        checkpoint.commit_batch(
            batch_index,
            batch,
            batch_rows,
            metrics={
                "batch_elapsed_seconds": round(batch_elapsed_seconds, 3),
                "max_rss_kib": max_rss_kib,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "fallback_symbol_count": fallback_count,
                "batch_error_types": list(batch_error_types),
            },
        )
        rows.update(batch_rows)
        print(
            json.dumps(
                {
                    "event": "BATCH_CHECKPOINTED",
                    "phase": checkpoint.phase,
                    "batch_index": batch_index,
                    "batch_count": total_batches,
                    "completed_symbols": len(rows),
                    "expected_symbols": len(ordered),
                    "batch_elapsed_seconds": round(batch_elapsed_seconds, 3),
                    "elapsed_seconds": round(time.monotonic() - phase_started, 3),
                    "max_rss_kib": max_rss_kib,
                    "batch_fallback_symbol_count": fallback_count,
                    "batch_error_types": list(batch_error_types),
                    "record_hash": checkpoint.last_record_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
    if tuple(sorted(rows)) != ordered:
        raise ValueError("checkpointed phase omitted expected symbols")
    checkpoint.mark_complete()
    execution = checkpoint.execution_summary()
    return rows, execution


def _maps_from_rows(
    rows: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str], dict[str, Mapping[str, Any]]]:
    facts = {
        symbol: row["opening_fact"]
        for symbol, row in rows.items()
        if row.get("row_outcome") == "LIVE_FACT"
        and isinstance(row.get("opening_fact"), Mapping)
    }
    fact_statuses = {symbol: str(row.get("fact_status") or "UNKNOWN") for symbol, row in rows.items()}
    field_statuses = {
        symbol: row["field_status"]
        for symbol, row in rows.items()
        if isinstance(row.get("field_status"), Mapping)
    }
    return facts, fact_statuses, field_statuses


def _select_repeat_symbols(
    expected_symbols: Sequence[str],
    rows: Mapping[str, Mapping[str, Any]],
    *,
    stale_symbols: Sequence[str],
    difference_symbols: Sequence[str],
    projection_content_hash: str,
    sample_size: int,
) -> dict[str, Any]:
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 0:
        raise ValueError("sample_size must be a non-negative integer")
    ordered = tuple(sorted(str(symbol) for symbol in expected_symbols))
    if set(rows) != set(ordered):
        raise ValueError("repeat planning requires a complete first-pass row map")
    partial = {symbol for symbol, row in rows.items() if row.get("fact_status") == "PARTIAL"}
    stale = set(str(symbol) for symbol in stale_symbols)
    differing = set(str(symbol) for symbol in difference_symbols)
    mandatory = (partial | stale | differing) & set(ordered)
    seed = f"{projection_content_hash}:task008-live-node-repeat-v1"
    candidates = set(ordered) - mandatory
    ranked = sorted(
        (
            hashlib.sha256(f"{seed}\0{symbol}".encode("utf-8")).hexdigest(),
            symbol,
        )
        for symbol in candidates
    )
    sampled = tuple(symbol for _, symbol in ranked[: min(sample_size, len(ranked))])
    selected = tuple(sorted(mandatory | set(sampled)))
    return {
        "selection_rule": "MANDATORY_UNION_DETERMINISTIC_HASH_SAMPLE",
        "deterministic_repeat_scope": "SAMPLED",
        "sampling_seed": seed,
        "requested_additional_sample_size": sample_size,
        "mandatory_counts": {
            "partial": len(partial & set(ordered)),
            "stale": len(stale & set(ordered)),
            "first_pass_difference": len(differing & set(ordered)),
            "union": len(mandatory),
        },
        "mandatory_symbols": sorted(mandatory),
        "sampled_symbols": list(sampled),
        "sampled_symbols_hash": semantic_hash(sampled),
        "symbols": list(selected),
        "symbols_hash": semantic_hash(selected),
    }


def _compare_rows_to_core(
    core_facts: Mapping[str, Any],
    core_field_status: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
    *,
    detail_limit: int,
) -> dict[str, Any]:
    live_facts, fact_statuses, field_statuses = _maps_from_rows(rows)
    fact_comparison = compare_opening_fact_maps(
        core_facts, live_facts, detail_limit=detail_limit
    )
    status_comparison = compare_opening_fact_maps(
        core_field_status, field_statuses, detail_limit=detail_limit
    )
    difference_symbols = sorted(
        set(fact_comparison["difference_symbols"])
        | set(status_comparison["difference_symbols"])
        | {symbol for symbol, row in rows.items() if row.get("row_outcome") == "RUNNER_ERROR"}
    )
    return {
        "live_facts": live_facts,
        "fact_statuses": fact_statuses,
        "field_statuses": field_statuses,
        "fact_comparison": fact_comparison,
        "status_comparison": status_comparison,
        "difference_symbols": difference_symbols,
    }


def _core_stale_symbols(core_facts: Mapping[str, Any], *, evaluation_time_ms: int, stale_after_ms: Any) -> list[str]:
    if isinstance(stale_after_ms, bool) or not isinstance(stale_after_ms, int):
        return []
    stale: list[str] = []
    for symbol, fact in core_facts.items():
        timestamp_ms = fact.get("timestamp_ms") if isinstance(fact, Mapping) else None
        if (
            isinstance(timestamp_ms, int)
            and not isinstance(timestamp_ms, bool)
            and evaluation_time_ms - timestamp_ms > stale_after_ms
        ):
            stale.append(str(symbol))
    return sorted(stale)


def _implementation_fingerprints() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[1]
    source_roots = (project_root / "src" / "engine_core", project_root / "examples")
    paths = sorted(
        path
        for root in source_roots
        for path in root.rglob("*.py")
        if path.is_file()
    )
    file_hashes = {
        path.relative_to(project_root).as_posix(): _file_sha256(path) for path in paths
    }
    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "src/engine_core",
            "examples",
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
    ).stdout
    diff_head = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", "src/engine_core", "examples"],
        cwd=project_root,
        check=True,
        capture_output=True,
    ).stdout
    return {
        "contract": "Task008ImplementationFingerprintV2",
        "file_count": len(file_hashes),
        "files": file_hashes,
        "workspace_status_sha256": hashlib.sha256(status).hexdigest(),
        "workspace_diff_head_sha256": hashlib.sha256(diff_head).hexdigest(),
    }


def _git_head() -> str:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _explicit_python_hash_seed() -> str:
    value = os.environ.get("PYTHONHASHSEED")
    if value is None or not value.isdigit() or int(value) > 4_294_967_295:
        raise ValueError("set PYTHONHASHSEED to an explicit integer in [0, 4294967295]")
    return value


def _load_run_context(
    *,
    q2frame_path: Path,
    core_report_path: Path,
    expected_q2frame_sha256: str,
    expected_core_report_sha256: str,
) -> dict[str, Any]:
    q2_sha = _file_sha256(q2frame_path)
    core_sha = _file_sha256(core_report_path)
    if q2_sha != expected_q2frame_sha256:
        raise ValueError("Q2Frame SHA-256 does not match the expected pin")
    if core_sha != expected_core_report_sha256:
        raise ValueError("Core replay report SHA-256 does not match the expected pin")

    core_report = _load_json_object(core_report_path)
    trade_date = core_report.get("trade_date")
    q2_evidence = core_report.get("q2frame")
    ordered = core_report.get("ordered")
    opening_nodes = ordered.get("opening_evidence") if isinstance(ordered, Mapping) else None
    core_opening = opening_nodes.get("OPENING_0932") if isinstance(opening_nodes, Mapping) else None
    if not isinstance(trade_date, str) or not isinstance(q2_evidence, Mapping):
        raise ValueError("Core report is missing trade_date or Q2Frame provenance")
    if not isinstance(core_opening, Mapping):
        raise ValueError("Core report is missing OPENING_0932 evidence")
    if q2_evidence.get("sha256") != q2_sha:
        raise ValueError("Q2Frame bytes do not match the Core report input identity")
    evaluation_time_ms = core_opening.get("evaluation_time_ms")
    core_facts = core_opening.get("facts_by_symbol")
    core_field_status = core_opening.get("opening_fact_field_status_by_symbol")
    if (
        isinstance(evaluation_time_ms, bool)
        or not isinstance(evaluation_time_ms, int)
        or not isinstance(core_facts, Mapping)
        or not isinstance(core_field_status, Mapping)
    ):
        raise ValueError("Core report opening facts or evaluation time are malformed")
    expected_symbols = tuple(sorted(str(symbol) for symbol in core_facts))
    if (
        not expected_symbols
        or len(set(expected_symbols)) != len(expected_symbols)
        or any(len(symbol) != 6 or not symbol.isdigit() for symbol in expected_symbols)
    ):
        raise ValueError("Core opening facts must have unique six-digit symbol keys")
    if core_opening.get("expected_symbol_count") != len(expected_symbols):
        raise ValueError("Core report expected symbol count differs from fact keys")

    projection, projection_inventory = _read_projection_through_cutoff(
        q2frame_path,
        trade_date=trade_date,
        expected_symbols=expected_symbols,
        evaluation_time_ms=evaluation_time_ms,
    )
    if tuple(projection.expected_symbols) != expected_symbols:
        raise ValueError("reconstructed Q2 projection universe differs from Core report")
    calendar, firing = _node_timer_firing(trade_date, evaluation_time_ms)
    observed_at = datetime.fromtimestamp(evaluation_time_ms / 1000.0, tz=timezone.utc)
    policy = core_opening.get("freshness_policy")
    stale_after_ms = policy.get("stale_after_ms") if isinstance(policy, Mapping) else None
    return {
        "q2frame_path": q2frame_path,
        "core_report_path": core_report_path,
        "q2frame_sha256": q2_sha,
        "core_report_sha256": core_sha,
        "core_report": core_report,
        "core_opening": core_opening,
        "core_facts": core_facts,
        "core_field_status": core_field_status,
        "trade_date": trade_date,
        "expected_symbols": expected_symbols,
        "evaluation_time_ms": evaluation_time_ms,
        "projection": projection,
        "projection_inventory": projection_inventory,
        "calendar": calendar,
        "firing": firing,
        "observed_at": observed_at,
        "stale_after_ms": stale_after_ms,
        "stale_symbols": _core_stale_symbols(
            core_facts, evaluation_time_ms=evaluation_time_ms, stale_after_ms=stale_after_ms
        ),
    }


def _run_fingerprint(
    context: Mapping[str, Any],
    *,
    batch_size: int,
    repeat_sample_size: int,
    detail_limit: int,
    primary_hash_seed: str,
) -> dict[str, Any]:
    symbols = tuple(context["expected_symbols"])
    return {
        "fingerprint_contract": "Task008RunFingerprintV2",
        "q2frame_path": str(context["q2frame_path"]),
        "q2frame_sha256": context["q2frame_sha256"],
        "core_report_path": str(context["core_report_path"]),
        "core_report_sha256": context["core_report_sha256"],
        "trade_date": context["trade_date"],
        "evaluation_time_ms": context["evaluation_time_ms"],
        "projection_content_hash": context["projection"].content_hash,
        "expected_symbol_count": len(symbols),
        "expected_symbols_hash": semantic_hash(symbols),
        "freshness_policy": {
            "stale_after_ms": 60_000,
            "max_future_skew_ms": 999,
        },
        "batch_size": batch_size,
        "repeat_sample_size": repeat_sample_size,
        "detail_limit": detail_limit,
        "primary_python_hash_seed": primary_hash_seed,
        "python_version": platform.python_version(),
        "git_head": _git_head(),
        "implementation": _implementation_fingerprints(),
    }


def _assert_implementation_fingerprint_unchanged(
    fingerprint: Mapping[str, Any],
) -> None:
    if _git_head() != fingerprint.get("git_head"):
        raise ValueError("Git HEAD changed while parity computation was running")
    if _implementation_fingerprints() != fingerprint.get("implementation"):
        raise ValueError("Core/examples source tree changed while parity computation was running")


def _ensure_run_manifest(run_dir: Path, fingerprint: Mapping[str, Any], *, resume: bool) -> dict[str, Any]:
    path = run_dir / "run_manifest.json"
    if resume:
        if not path.is_file():
            raise FileNotFoundError("run_manifest.json is missing; cannot resume")
        manifest = _load_json_object(path)
        if manifest.get("fingerprint") != dict(fingerprint):
            raise ValueError("run manifest fingerprint differs from current inputs/code/configuration")
        return dict(manifest)
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "contract_version": "Task008LiveNodeReplayRunV1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "fingerprint": dict(fingerprint),
    }
    _write_json_atomic_exclusive(path, manifest)
    return manifest


def _summary_from_first_pass(
    context: Mapping[str, Any],
    *,
    rows: Mapping[str, Mapping[str, Any]],
    comparison: Mapping[str, Any],
    repeat_plan: Mapping[str, Any],
    run_manifest: Mapping[str, Any],
    execution: Mapping[str, Any],
    detail_limit: int,
) -> dict[str, Any]:
    fact_statuses = [str(row.get("fact_status") or "UNKNOWN") for row in rows.values()]
    outcomes = [str(row.get("row_outcome") or "UNKNOWN") for row in rows.values()]
    live_facts = comparison["live_facts"]
    return {
        "contract_version": "Task008LiveNodeReplayParityV2",
        "phase_status": "FIRST_PASS_COMPLETE_REPEAT_PENDING",
        "comparison_is_a_gate": False,
        "trade_date": context["trade_date"],
        "run_manifest": dict(run_manifest),
        "inputs": {
            "q2frame_path": str(context["q2frame_path"]),
            "q2frame_sha256": context["q2frame_sha256"],
            "core_report_path": str(context["core_report_path"]),
            "core_report_sha256": context["core_report_sha256"],
            "core_report_contract_version": context["core_report"].get("contract_version"),
        },
        "calendar": {
            "status": "CONFIGURATION_ONLY_NOT_OBSERVED_MARKET_DATA",
            "semantic_hash": context["calendar"].semantic_hash,
            "session_plan_hash": context["firing"].session_plan_hash,
        },
        "observation": {
            "timer_id": context["firing"].timer_id,
            "business_anchor_time_ms": context["firing"].scheduled_time_ms,
            "evaluation_time_ms": context["evaluation_time_ms"],
            "fired_time_ms": context["firing"].fired_time_ms,
            "late_by_ms": context["firing"].late_by_ms,
            "opening_settling_delay_ms": OPENING_0932_EVALUATION_DELAY_MS,
        },
        "projection": {
            **context["projection_inventory"],
            "status": context["projection"].status.value,
            "coverage": context["projection"].coverage,
            "expected_symbol_count": len(context["projection"].expected_symbols),
            "quote_count": len(context["projection"].quotes),
            "stale_symbol_count": len(context["projection"].stale_symbols),
            "content_hash": context["projection"].content_hash,
            "source_id": "t1_v2_q2frame",
        },
        "core_replay": {
            "opening_fact_count": len(context["core_facts"]),
            "reported_fact_status_counts": context["core_opening"].get("fact_status_counts"),
            "reported_field_status_counts": context["core_opening"].get(
                "opening_fact_field_status_counts"
            ),
            "facts_hash": semantic_hash(context["core_facts"]),
            "field_status_hash": semantic_hash(context["core_field_status"]),
        },
        "live_node_first_pass": {
            "builder": "run_live_morning_shadow.build_node_evidence",
            "scope": "ALL_EXPECTED_SYMBOLS_ONCE",
            "expected_symbol_count": len(context["expected_symbols"]),
            "completed_symbol_count": len(rows),
            "opening_fact_count": len(live_facts),
            "fact_status_counts": dict(sorted(Counter(fact_statuses).items())),
            "row_outcome_counts": dict(sorted(Counter(outcomes).items())),
            "per_symbol_rows_aggregate_hash": _aggregate_row_hash(rows),
            "aggregate_hash_contract": (
                "sorted (symbol, per_symbol_row_hash) aggregate; not the live node's whole-node semantic_hash"
            ),
            "execution": dict(execution),
            "side_effect_boundary": "sealed Q2Frame file + in-memory Engine; no Redis/TD/Rabbit/production I/O",
        },
        "comparison": {
            "opening_facts": comparison["fact_comparison"],
            "opening_fact_field_status": comparison["status_comparison"],
            "core_stale_symbol_count": len(context["stale_symbols"]),
            "live_partial_symbol_count": sum(status == "PARTIAL" for status in fact_statuses),
            "stale_partial_overlap_count": len(
                set(context["stale_symbols"])
                & {symbol for symbol, row in rows.items() if row.get("fact_status") == "PARTIAL"}
            ),
            "stale_symbols_sample": list(context["stale_symbols"][:detail_limit]),
            "difference_symbols_count": len(comparison["difference_symbols"]),
            "difference_symbols_hash": semantic_hash(tuple(comparison["difference_symbols"])),
            "difference_symbols_sample": list(comparison["difference_symbols"][:detail_limit]),
            "differences_are_descriptive_not_gates": True,
        },
        "determinism_repeat_plan": dict(repeat_plan),
        "limits": [
            "Q2Frame event-time replay is not Rabbit delivery or arrival-order replay.",
            "The live wall-clock shell, Redis reader, TD reader, retry loop, and external effects were not executed.",
            "Calendar dates are local configuration used only to calculate the session timer.",
            "A one-date match does not establish NORMAL acceptance, historical available_at, or full-market coverage beyond the captured Q2Frame cohort.",
            "Deterministic repeat is sampled and must not be described as full-cohort deterministic verification.",
            "The run does not change TASK-008 PARTIAL_EVIDENCE or M3-1/TD health status.",
        ],
    }


def _publish_final_evidence(
    final_report: Mapping[str, Any],
    output: Path,
) -> None:
    """Publish final report/checksums idempotently across a crash boundary."""

    if output.exists():
        existing = _load_json_object(output)
        if canonical_json(existing) != canonical_json(final_report):
            raise ValueError("existing final report differs from recomputed evidence")
    else:
        _write_json_atomic_exclusive(output, final_report)

    checksum_path = output.parent / "sha256sums.txt"
    entries = []
    for path in sorted(output.parent.rglob("*")):
        if (
            not path.is_file()
            or path == checksum_path
            or ".tmp-" in path.name
            or path.name.endswith("_progress.json")
        ):
            continue
        relative = path.relative_to(output.parent).as_posix()
        entries.append(f"{_file_sha256(path)}  {relative}\n")
    expected_checksums = "".join(entries)
    if checksum_path.exists():
        actual_checksums = checksum_path.read_text(encoding="utf-8")
        if actual_checksums != expected_checksums:
            raise ValueError("existing sha256sums.txt differs from the current evidence tree")
    else:
        _write_bytes_atomic_exclusive(checksum_path, expected_checksums.encode("utf-8"))


def _run_first_pass(
    *,
    context: Mapping[str, Any],
    output: Path,
    batch_size: int,
    repeat_sample_size: int,
    detail_limit: int,
    resume: bool,
) -> dict[str, Any]:
    primary_seed = _explicit_python_hash_seed()
    fingerprint = _run_fingerprint(
        context,
        batch_size=batch_size,
        repeat_sample_size=repeat_sample_size,
        detail_limit=detail_limit,
        primary_hash_seed=primary_seed,
    )
    run_manifest = _ensure_run_manifest(output.parent, fingerprint, resume=resume)
    checkpoint = BatchCheckpoint(
        output.parent / "checkpoint" / "first_pass",
        phase="first_pass",
        manifest={"fingerprint": fingerprint},
        expected_symbols=context["expected_symbols"],
        batch_size=batch_size,
        python_hash_seed=primary_seed,
        resume=resume,
    )
    rows, execution = _execute_checkpointed_phase(
        checkpoint,
        firing=context["firing"],
        observed_at=context["observed_at"],
        trade_date=context["trade_date"],
        projection=context["projection"],
    )
    comparison = _compare_rows_to_core(
        context["core_facts"],
        context["core_field_status"],
        rows,
        detail_limit=detail_limit,
    )
    _assert_implementation_fingerprint_unchanged(fingerprint)
    repeat_plan = _select_repeat_symbols(
        context["expected_symbols"],
        rows,
        stale_symbols=context["stale_symbols"],
        difference_symbols=comparison["difference_symbols"],
        projection_content_hash=context["projection"].content_hash,
        sample_size=repeat_sample_size,
    )
    summary = _summary_from_first_pass(
        context,
        rows=rows,
        comparison=comparison,
        repeat_plan=repeat_plan,
        run_manifest=run_manifest,
        execution=execution,
        detail_limit=detail_limit,
    )
    summary_path = output.parent / "first_pass_summary.json"
    if summary_path.exists():
        existing = _load_json_object(summary_path)
        if existing != summary:
            raise ValueError("existing first_pass_summary.json differs from resumed calculation")
    else:
        _write_json_atomic_exclusive(summary_path, summary)
    print(
        json.dumps(
            {
                "event": "FIRST_PASS_COMPLETE",
                "summary_path": str(summary_path),
                "expected_symbols": len(context["expected_symbols"]),
                "completed_symbols": len(rows),
                "repeat_symbols": len(repeat_plan["symbols"]),
                "row_aggregate_hash": _aggregate_row_hash(rows),
                "status": summary["phase_status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return summary


def _run_repeat_pass(
    *,
    context: Mapping[str, Any],
    output: Path,
    batch_size: int,
    repeat_sample_size: int,
    detail_limit: int,
    resume: bool,
) -> dict[str, Any]:
    summary_path = output.parent / "first_pass_summary.json"
    if not summary_path.is_file():
        raise FileNotFoundError("first_pass_summary.json is missing; run the full first pass first")
    summary = _load_json_object(summary_path)
    fingerprint = summary.get("run_manifest", {}).get("fingerprint")
    if not isinstance(fingerprint, Mapping):
        raise ValueError("first pass summary has no run fingerprint")
    repeat_plan = summary.get("determinism_repeat_plan")
    if not isinstance(repeat_plan, Mapping):
        raise ValueError("first pass summary has no deterministic repeat plan")
    if (
        fingerprint.get("batch_size") != batch_size
        or fingerprint.get("repeat_sample_size") != repeat_sample_size
        or fingerprint.get("detail_limit") != detail_limit
    ):
        raise ValueError("repeat command configuration differs from the first pass")

    context_fingerprint = _run_fingerprint(
        context,
        batch_size=batch_size,
        repeat_sample_size=repeat_sample_size,
        detail_limit=detail_limit,
        primary_hash_seed=str(fingerprint.get("primary_python_hash_seed")),
    )
    if dict(fingerprint) != context_fingerprint:
        raise ValueError("current inputs/code differ from the completed first pass")
    primary_seed = str(fingerprint["primary_python_hash_seed"])
    repeat_seed = _explicit_python_hash_seed()
    if repeat_seed == primary_seed:
        raise ValueError("repeat process must use a different PYTHONHASHSEED from first pass")

    primary_checkpoint = BatchCheckpoint(
        output.parent / "checkpoint" / "first_pass",
        phase="first_pass",
        manifest={"fingerprint": dict(fingerprint)},
        expected_symbols=context["expected_symbols"],
        batch_size=batch_size,
        python_hash_seed=primary_seed,
        resume=True,
    )
    if set(primary_checkpoint.completed_rows) != set(context["expected_symbols"]):
        raise ValueError("first pass checkpoint does not cover every expected symbol")
    if primary_checkpoint.process_instance_id in primary_checkpoint.process_instance_ids:
        raise ValueError("repeat must execute in a process independent from the first pass")

    repeat_symbols = tuple(str(symbol) for symbol in repeat_plan.get("symbols", ()))
    if repeat_symbols != tuple(sorted(set(repeat_symbols))):
        raise ValueError("repeat symbol list is not unique and sorted")
    repeat_manifest = {
        "first_pass_fingerprint": dict(fingerprint),
        "repeat_symbols_hash": semantic_hash(repeat_symbols),
        "primary_python_hash_seed": primary_seed,
        "repeat_python_hash_seed": repeat_seed,
    }
    repeat_checkpoint = BatchCheckpoint(
        output.parent / "checkpoint" / "repeat_pass",
        phase="repeat_pass",
        manifest=repeat_manifest,
        expected_symbols=repeat_symbols,
        batch_size=batch_size,
        python_hash_seed=repeat_seed,
        resume=resume,
    )
    repeated_rows, repeat_execution = _execute_checkpointed_phase(
        repeat_checkpoint,
        firing=context["firing"],
        observed_at=context["observed_at"],
        trade_date=context["trade_date"],
        projection=context["projection"],
    )
    _assert_implementation_fingerprint_unchanged(fingerprint)
    primary_rows = primary_checkpoint.completed_rows
    independent_process = set(primary_checkpoint.process_instance_ids).isdisjoint(
        repeat_checkpoint.process_instance_ids
    )
    if not independent_process:
        raise ValueError("repeat checkpoint contains a process instance used by the first pass")
    mismatches = [
        {
            "symbol": symbol,
            "first_row_hash": primary_rows[symbol]["row_hash"],
            "repeat_row_hash": repeated_rows[symbol]["row_hash"],
        }
        for symbol in repeat_symbols
        if primary_rows[symbol]["row_hash"] != repeated_rows[symbol]["row_hash"]
    ]

    final_report = dict(summary)
    final_report["phase_status"] = "COMPLETE"
    final_report["live_node_first_pass"] = dict(summary["live_node_first_pass"])
    final_report["determinism_repeat"] = {
        "deterministic_repeat_scope": "SAMPLED",
        "repeat_symbol_count": len(repeat_symbols),
        "repeat_symbol_hash": semantic_hash(repeat_symbols),
        "repeat_selection_rule": repeat_plan.get("selection_rule"),
        "sampling_seed": repeat_plan.get("sampling_seed"),
        "additional_sample_count": len(repeat_plan.get("sampled_symbols", ())),
        "additional_sample_hash": repeat_plan.get("sampled_symbols_hash"),
        "mandatory_counts": repeat_plan.get("mandatory_counts"),
        "primary_python_hash_seed": primary_seed,
        "repeat_python_hash_seed": repeat_seed,
        "independent_process": independent_process,
        "primary_process_ids": sorted(set(primary_checkpoint.process_ids)),
        "primary_process_instance_ids": sorted(
            set(primary_checkpoint.process_instance_ids)
        ),
        "repeat_process_ids": sorted(set(repeat_checkpoint.process_ids)),
        "repeat_process_instance_ids": sorted(
            set(repeat_checkpoint.process_instance_ids)
        ),
        "repeat_row_aggregate_hash": _aggregate_row_hash(repeated_rows),
        "primary_selected_row_aggregate_hash": _aggregate_row_hash(
            {symbol: primary_rows[symbol] for symbol in repeat_symbols}
        ),
        "deterministic_for_sample": not mismatches,
        "mismatch_count": len(mismatches),
        "mismatch_symbols_sample": [item["symbol"] for item in mismatches[:detail_limit]],
        "mismatch_details_sample": mismatches[:detail_limit],
        "execution": repeat_execution,
        "not_a_full_cohort_repeat": True,
    }
    final_report["run_cost_observation"] = {
        "first_pass_active_batch_elapsed_seconds": summary["live_node_first_pass"][
            "execution"
        ].get("active_batch_elapsed_seconds"),
        "repeat_active_batch_elapsed_seconds": repeat_execution.get(
            "active_batch_elapsed_seconds"
        ),
        "measurement": "sum of per-batch wall time recorded in durable checkpoints",
        "note": "Elapsed time is recorded as cost evidence and is not a data-quality pass/fail gate.",
    }
    final_report["completion"] = {
        "expected_symbols": len(context["expected_symbols"]),
        "first_pass_rows": len(primary_rows),
        "repeat_rows": len(repeated_rows),
        "all_expected_symbols_accounted_for": set(primary_rows)
        == set(context["expected_symbols"]),
        "comparison_is_a_gate": False,
    }
    final_report["live_node_first_pass"]["determinism_contract_note"] = (
        "Per-symbol sorted row-hash aggregate; it is not the batch-dependent live node semantic_hash."
    )
    _publish_final_evidence(final_report, output)
    output_sha256 = _file_sha256(output)
    print(
        json.dumps(
            {
                "event": "REPEAT_PASS_COMPLETE",
                "report_path": str(output),
                "report_sha256": output_sha256,
                "phase_status": final_report["phase_status"],
                "core_vs_live_status": final_report.get("comparison", {}).get(
                    "opening_facts", {}
                ).get("comparison_status"),
                "sample_repeat_deterministic": final_report["determinism_repeat"][
                    "deterministic_for_sample"
                ],
                "repeat_mismatch_count": len(mismatches),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return final_report


def run_live_node_replay_parity(
    *,
    q2frame_path: Path,
    core_report_path: Path,
    expected_q2frame_sha256: str,
    expected_core_report_sha256: str,
    output: Path | None = None,
    detail_limit: int = DETAIL_LIMIT,
    batch_size: int = 100,
    repeat_sample_size: int = 300,
    resume: bool = False,
) -> dict[str, Any]:
    """Run the full first pass and save resumable evidence; repeat in a second CLI process."""

    if detail_limit < 0:
        raise ValueError("detail_limit must be non-negative")
    context = _load_run_context(
        q2frame_path=q2frame_path,
        core_report_path=core_report_path,
        expected_q2frame_sha256=expected_q2frame_sha256,
        expected_core_report_sha256=expected_core_report_sha256,
    )
    if output is None:
        raise ValueError("output path is required so the first pass can persist checkpoints")
    return _run_first_pass(
        context=context,
        output=output,
        batch_size=batch_size,
        repeat_sample_size=repeat_sample_size,
        detail_limit=detail_limit,
        resume=resume,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q2frame", type=Path, required=True)
    parser.add_argument("--expected-q2frame-sha256", required=True)
    parser.add_argument("--core-report", type=Path, required=True)
    parser.add_argument("--expected-core-report-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--detail-limit", type=int, default=DETAIL_LIMIT)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--repeat-sample-size", type=int, default=300)
    parser.add_argument(
        "--repeat-only",
        action="store_true",
        help="run the planned deterministic sample in a separate process after first pass",
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if args.batch_size <= 0 or args.repeat_sample_size < 0 or args.detail_limit < 0:
        parser.error("batch size must be positive; repeat sample and detail limits must be non-negative")
    if args.repeat_only and not args.output.parent.is_dir():
        parser.error("repeat-only requires the existing output directory from a completed first pass")
    if not args.repeat_only and args.resume and not args.output.parent.is_dir():
        parser.error("resume requires the existing output directory")
    if not args.repeat_only and not args.resume and args.output.parent.exists():
        parser.error("output directory already exists; choose a fresh validation directory")
    if args.repeat_only and args.output.exists() and not args.resume:
        parser.error("final output already exists; use --resume to verify/finalize without overwriting")

    try:
        context = _load_run_context(
            q2frame_path=args.q2frame,
            core_report_path=args.core_report,
            expected_q2frame_sha256=args.expected_q2frame_sha256,
            expected_core_report_sha256=args.expected_core_report_sha256,
        )
        if args.repeat_only:
            _run_repeat_pass(
                context=context,
                output=args.output,
                batch_size=args.batch_size,
                repeat_sample_size=args.repeat_sample_size,
                detail_limit=args.detail_limit,
                resume=args.resume,
            )
        else:
            _run_first_pass(
                context=context,
                output=args.output,
                batch_size=args.batch_size,
                repeat_sample_size=args.repeat_sample_size,
                detail_limit=args.detail_limit,
                resume=args.resume,
            )
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
