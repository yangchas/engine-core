"""Run a bounded, read-only Core morning shadow on a real trading day.

This is an execution shell, not a new scheduler.  It reuses the existing
``SessionPlan``/``SessionTimer`` calculation and captures the two Core-owned
morning nodes at the instant they become due.  ``engine-next`` and ``t1-v2``
remain the production owners; this process never consumes Rabbit, sends an
effect, or writes Redis/TD.

The command is intentionally bounded to a small symbol list.  Each node is
queried at its own observation time; a later query must not be used as a
retroactive 09:26/09:32 observation.  The output directory is write-once so
that a rerun cannot silently overwrite evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, time as clock_time, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    FreshnessPolicy,
    SessionPlan,
    TimerFiring,
    TimerSpec,
    TradingCalendarSnapshot,
    assess_startup_readiness,
    build_a_share_session_plan,
    due_timer_firings,
    semantic_hash,
)

try:  # Script execution resolves sibling examples directly.
    from run_engine_next_auction_loader_probe import probe as probe_legacy_auction_loader
    from run_morning_vertical_slice_shadow import (
        CORE_TIMER_SPECS,
        _load_calendar,
        _redis_projection,
        dispatch_morning_fact_nodes,
    )
    from run_real_auction_shadow import query_rows
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.run_engine_next_auction_loader_probe import probe as probe_legacy_auction_loader
    from examples.run_morning_vertical_slice_shadow import (
        CORE_TIMER_SPECS,
        _load_calendar,
        _redis_projection,
        dispatch_morning_fact_nodes,
    )
    from examples.run_real_auction_shadow import query_rows


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
LIVE_SHADOW_CONTRACT_VERSION = "LiveMorningShadowV1"


def _strict_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
    if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
        raise ValueError("symbols must be comma-separated six-digit codes")
    return symbols


def _parse_local_time(value: str) -> clock_time:
    parsed = datetime.strptime(value, "%H:%M:%S")
    return parsed.time()


def _epoch_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def _json_ready(value: Any) -> Any:
    """Convert dataclasses/tuples for evidence JSON without changing hashes."""

    if hasattr(value, "__dataclass_fields__"):
        return _json_ready(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value


def _atomic_write_once(path: Path, payload: Mapping[str, Any]) -> str:
    """Write one evidence file without overwrite and return its SHA identity."""

    import hashlib

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    if path.exists() or temp.exists():
        raise FileExistsError("evidence path already exists: " + str(path))
    encoded = (
        json.dumps(
            _json_ready(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        ).encode("utf-8")
        + b"\n"
    )
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
    os.replace(temp, path)
    return hashlib.sha256(encoded).hexdigest()


def _timer_payload(firing: TimerFiring) -> dict[str, Any]:
    return {
        "timer_id": firing.timer_id,
        "scheduled_time_ms": firing.scheduled_time_ms,
        "fired_time_ms": firing.fired_time_ms,
        "trigger_basis": firing.trigger_basis,
        "origin": firing.origin,
        "late_by_ms": firing.late_by_ms,
        "session_plan_hash": firing.session_plan_hash,
        "content_hash": firing.content_hash,
    }


def build_node_evidence(
    firing: TimerFiring,
    *,
    observed_at: datetime,
    trade_date: str,
    symbols: Sequence[str],
    td_rows_by_symbol: Mapping[str, Sequence[Sequence[Any] | Mapping[str, Any]]],
    projection: Any = None,
    legacy_loader: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one node's deterministic evidence from already-read inputs.

    All I/O happens before this function.  ``observed_at`` and each source
    record's timestamp are retained as evidence; no timestamp is rewritten to
    the business timer.
    """

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if firing.timer_id not in {item.timer_id for item in CORE_TIMER_SPECS}:
        raise ValueError("unsupported Core morning timer: " + firing.timer_id)
    ordered_symbols = tuple(sorted(symbols))
    if any(len(symbol) != 6 or not symbol.isdigit() for symbol in ordered_symbols):
        raise ValueError("symbols must be six-digit codes")
    dispatch_rows: list[dict[str, Any]] = []
    for symbol in ordered_symbols:
        rows = tuple(td_rows_by_symbol.get(symbol, ()))
        facts = dispatch_morning_fact_nodes(
            (_timer_payload(firing),),
            projection=projection,
            auction_rows=rows,
            symbol=symbol,
        )
        dispatch_rows.append(
            {
                "symbol": symbol,
                "td_row_count": len(rows),
                "facts": facts,
            }
        )
    source_ranges = []
    for symbol in ordered_symbols:
        for row in td_rows_by_symbol.get(symbol, ()):
            if isinstance(row, Mapping):
                source_ranges.append(row.get("ts"))
            elif row:
                source_ranges.append(row[0])
    result = {
        "contract_version": LIVE_SHADOW_CONTRACT_VERSION,
        "timer": _timer_payload(firing),
        "trade_date": trade_date,
        "business_anchor_time": firing.scheduled_time_ms,
        "observed_at_ms": _epoch_ms(observed_at),
        "observed_at": observed_at.isoformat(),
        "symbols": ordered_symbols,
        "td_rows_by_symbol": {
            symbol: tuple(td_rows_by_symbol.get(symbol, ())) for symbol in ordered_symbols
        },
        "source_record_time_values": tuple(source_ranges),
        "legacy_loader": legacy_loader or {"status": "NOT_CONFIGURED"},
        "fact_dispatch": tuple(dispatch_rows),
        "read_only": True,
        "side_effect_boundary": (
            "Redis SMEMBERS/HGETALL and TD SELECT only; legacy loader is GuardRedis-read-only; "
            "no Rabbit ACK/publish, Redis/TD write, recovery, notification or effect"
        ),
    }
    result["semantic_hash"] = semantic_hash(
        {
            "contract_version": LIVE_SHADOW_CONTRACT_VERSION,
            "timer": result["timer"],
            "trade_date": trade_date,
            "business_anchor_time": firing.scheduled_time_ms,
            "symbols": ordered_symbols,
            "fact_dispatch": tuple(dispatch_rows),
        }
    )
    return result


def _read_td_rows(
    symbols: Sequence[str],
    *,
    trade_date: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> dict[str, list[tuple[Any, ...]]]:
    return {
        symbol: query_rows(
            trade_date=trade_date,
            symbol=symbol,
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
        )
        for symbol in symbols
    }


def _legacy_loader_evidence(
    legacy_root: Path | None,
    *,
    trade_date: str,
    symbols: Sequence[str],
) -> dict[str, Any]:
    if legacy_root is None:
        return {"status": "NOT_CONFIGURED"}
    return probe_legacy_auction_loader(
        legacy_root=legacy_root,
        trade_date=trade_date,
        tags=("0920", "0924", "0925"),
        symbols=tuple(symbols),
    )


def _capture_node(
    firing: TimerFiring,
    *,
    observed_at: datetime,
    trade_date: str,
    symbols: Sequence[str],
    stale_after_ms: int,
    legacy_root: Path | None,
    td_config: Mapping[str, Any],
) -> dict[str, Any]:
    td_rows = _read_td_rows(symbols, trade_date=trade_date, **td_config)
    legacy = _legacy_loader_evidence(
        legacy_root,
        trade_date=trade_date,
        symbols=symbols,
    )
    projection = None
    if firing.timer_id == "OPENING_0932":
        projection = _redis_projection(
            trade_date=trade_date,
            observed_at=observed_at,
            stale_after_ms=stale_after_ms,
        )
    return build_node_evidence(
        firing,
        observed_at=observed_at,
        trade_date=trade_date,
        symbols=symbols,
        td_rows_by_symbol=td_rows,
        projection=projection,
        legacy_loader=legacy,
    )


def _startup_evidence(
    *,
    trade_date: str,
    calendar: TradingCalendarSnapshot,
    plan: SessionPlan,
    observed_at: datetime,
    stale_after_ms: int,
) -> dict[str, Any]:
    projection = _redis_projection(
        trade_date=trade_date,
        observed_at=observed_at,
        stale_after_ms=stale_after_ms,
    )
    readiness = assess_startup_readiness(
        trade_date=trade_date,
        as_of_ms=_epoch_ms(observed_at),
        calendar=calendar,
        session_plan=plan,
        q2=projection,
        timer_specs=CORE_TIMER_SPECS,
        origin="NORMAL",
    )
    return {
        "observed_at": observed_at.isoformat(),
        "observed_at_ms": _epoch_ms(observed_at),
        "readiness": asdict(readiness),
        "q2": {
            "status": projection.status,
            "consistency_status": projection.consistency_status,
            "coverage": projection.coverage,
            "oldest_source_time_ms": projection.oldest_source_time_ms,
            "newest_source_time_ms": projection.newest_source_time_ms,
            "content_hash": projection.content_hash,
        },
        "read_only": True,
    }


def run_live_morning_shadow(
    *,
    trade_date: str,
    calendar: TradingCalendarSnapshot,
    output_dir: Path,
    symbols: Sequence[str],
    stale_after_ms: int,
    td_config: Mapping[str, Any],
    legacy_root: Path | None = None,
    now_fn=_now_local,
    sleep_fn=time.sleep,
    poll_seconds: float = 0.25,
    start_at: clock_time = clock_time(9, 15),
    stop_at: clock_time = clock_time(9, 33),
) -> dict[str, Any]:
    """Run the bounded live shell; clock/sleep are injectable for tests."""

    if stale_after_ms < 0:
        raise ValueError("stale_after_ms must be non-negative")
    if poll_seconds < 0:
        raise ValueError("poll_seconds must be non-negative")
    ordered_symbols = _strict_symbols(",".join(symbols))
    plan = build_a_share_session_plan(trade_date, calendar)
    first_now = now_fn()
    if first_now.tzinfo is None or first_now.utcoffset() is None:
        raise ValueError("now_fn must return timezone-aware datetime")
    local_first = first_now.astimezone(LOCAL_TZ)
    if local_first.date().isoformat() != trade_date:
        raise ValueError("clock date does not match trade_date")
    start_dt = datetime.combine(local_first.date(), start_at, tzinfo=LOCAL_TZ)
    stop_dt = datetime.combine(local_first.date(), stop_at, tzinfo=LOCAL_TZ)
    if stop_dt <= start_dt:
        raise ValueError("stop_at must be later than start_at")
    if local_first > stop_dt:
        raise ValueError("live shadow start is after stop_at; use a new output directory")

    output_dir.mkdir(parents=True, exist_ok=False)
    startup = _startup_evidence(
        trade_date=trade_date,
        calendar=calendar,
        plan=plan,
        observed_at=first_now,
        stale_after_ms=stale_after_ms,
    )
    startup_path = output_dir / "startup.json"
    startup_sha = _atomic_write_once(startup_path, startup)

    late_start = local_first > start_dt
    previous_ms = None if late_start else _epoch_ms(first_now)
    origin = "RECOVERY_CATCHUP" if late_start else "NORMAL"
    fired_ids: set[str] = set()
    nodes: list[dict[str, Any]] = []
    node_file_shas: dict[str, str] = {}
    while True:
        now = now_fn()
        local_now = now.astimezone(LOCAL_TZ)
        if local_now > stop_dt:
            break
        current_ms = _epoch_ms(now)
        firings = due_timer_firings(
            plan,
            CORE_TIMER_SPECS,
            previous_time_ms=previous_ms,
            current_time_ms=current_ms,
            already_fired=tuple(sorted(fired_ids)),
            origin=origin,
        )
        for firing in firings:
            if firing.timer_id in fired_ids:
                continue
            node = _capture_node(
                firing,
                observed_at=now,
                trade_date=trade_date,
                symbols=ordered_symbols,
                stale_after_ms=stale_after_ms,
                legacy_root=legacy_root,
                td_config=td_config,
            )
            node_path = output_dir / (firing.timer_id + ".json")
            node_file_shas[firing.timer_id] = _atomic_write_once(node_path, node)
            nodes.append(node)
            fired_ids.add(firing.timer_id)
        if len(fired_ids) == len(CORE_TIMER_SPECS):
            break
        previous_ms = current_ms
        if poll_seconds:
            sleep_fn(poll_seconds)

    manifest = {
        "contract_version": LIVE_SHADOW_CONTRACT_VERSION,
        "trade_date": trade_date,
        "calendar_semantic_hash": calendar.semantic_hash,
        "session_plan_hash": plan.content_hash,
        "symbols": ordered_symbols,
        "startup_file_sha256": startup_sha,
        "node_file_sha256": node_file_shas,
        "node_timer_ids": tuple(item["timer"]["timer_id"] for item in nodes),
        "node_count": len(nodes),
        "origin": origin,
        "read_only": True,
        "safety": {
            "new_rabbit_consumer": 0,
            "rabbit_ack_or_publish": 0,
            "redis_write": 0,
            "td_write": 0,
            "notification_or_effect": 0,
            "production_restart": 0,
        },
        "notes": (
            "engine-next and t1-v2 remain production owners",
            "node input is queried at actual observation time",
            "TD rows do not establish Rabbit batch membership",
        ),
    }
    manifest["semantic_hash"] = semantic_hash(manifest)
    _atomic_write_once(output_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--calendar-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--symbols", default="600519")
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--poll-ms", type=int, default=250)
    parser.add_argument("--start-at", default="09:15:00")
    parser.add_argument("--stop-at", default="09:33:00")
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--td-host", default=os.environ.get("TDENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--td-port", type=int, default=int(os.environ.get("TDENGINE_PORT", "6030")))
    parser.add_argument("--td-user", default=os.environ.get("TDENGINE_USER", "root"))
    parser.add_argument("--td-password", default=os.environ.get("TDENGINE_PASSWORD", "taosdata"))
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    args = parser.parse_args()
    if args.poll_ms < 0:
        parser.error("poll-ms must be non-negative")
    try:
        symbols = _strict_symbols(args.symbols)
        start_at = _parse_local_time(args.start_at)
        stop_at = _parse_local_time(args.stop_at)
        calendar = _load_calendar(args.calendar_file, trade_date=args.trade_date)
        manifest = run_live_morning_shadow(
            trade_date=args.trade_date,
            calendar=calendar,
            output_dir=args.output_dir,
            symbols=symbols,
            stale_after_ms=args.stale_after_ms,
            legacy_root=args.legacy_root,
            td_config={
                "host": args.td_host,
                "port": args.td_port,
                "user": args.td_user,
                "password": args.td_password,
                "database": args.td_database,
            },
            poll_seconds=args.poll_ms / 1000.0,
            start_at=start_at,
            stop_at=stop_at,
        )
    except (FileExistsError, OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(_json_ready(manifest), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
