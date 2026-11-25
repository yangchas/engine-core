"""Run the previous-day limit-up pool through the core fact boundary.

The Redis access is deliberately read-only and date-explicit.  The payload is
validated before it enters ``PreviousDayLimitPoolFunction``; no missing date,
source timestamp or universe denominator is invented.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (
    LiveInputReader,
    probe_rows_and_summary,
    DataContext,
    DataRequest,
    PreviousDayLimitPoolFunction,
    RedisPreviousDayLimitPoolProvider,
    build_calendar_snapshot,
    build_previous_day_limit_structure,
    canonical_json,
)


def _date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("date must be strict YYYY-MM-DD")
    return value


def _read_redis_rows(
    client: Any,
    previous_trade_date: str,
) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    reader = LiveInputReader(client, now_ms=lambda: int(time.time() * 1000))
    read = reader.read(
        "yest_limit_pool", trade_date=previous_trade_date,
        previous_trade_date=previous_trade_date,
    )
    return probe_rows_and_summary(read)


def run_real_previous_day_limit_pool(
    *,
    trade_date: str,
    previous_trade_date: str,
    observed_at: datetime,
    redis_kwargs: dict[str, Any],
    temporal_mode: str = "HISTORICAL",
    fetch_rows_override: Any = None,
    redis_summary_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trade_date = _date(trade_date)
    previous_trade_date = _date(previous_trade_date)
    if previous_trade_date >= trade_date:
        raise ValueError("previous trade date must be earlier than trade date")
    observed_at_ms = int(observed_at.timestamp() * 1000)
    calendar = build_calendar_snapshot(
        [previous_trade_date, trade_date],
        version="real-limit-pool-probe-" + trade_date,
        declared_valid_from=trade_date,
        declared_valid_to=trade_date,
        source_guard_valid_from=previous_trade_date,
        source_guard_valid_to=trade_date,
        source_id="explicit_probe_calendar",
        observed_at_ms=observed_at_ms,
        evidence_ref="probe://explicit-trade-date-pair",
    )
    redis_summary: dict[str, Any] = redis_summary_override or {}

    def fetch_rows(previous: str) -> Iterable[Mapping[str, Any]]:
        nonlocal redis_summary
        if fetch_rows_override is not None:
            return fetch_rows_override(previous)
        import redis  # type: ignore[import-not-found]

        client = redis.Redis(**redis_kwargs)
        client.ping()
        try:
            rows, redis_summary = _read_redis_rows(client, previous)
            return rows
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    def fetch_metadata(previous: str) -> Mapping[str, Any] | None:
        metadata = redis_summary.get("meta")
        return metadata if isinstance(metadata, Mapping) else None

    provider = RedisPreviousDayLimitPoolProvider(
        fetch_rows,
        observed_at_ms=lambda: observed_at_ms,
        metadata=fetch_metadata,
        source_id="redis_yest_limit_pool",
        source_schema="PreviousDayLimitPoolV1",
        evidence_ref="redis://cache:yest_limit_pool/" + previous_trade_date,
    )
    request = DataRequest(
        request_id="real-previous-day-limit-pool-" + trade_date,
        function_id="previous_day_limit_pool",
        trade_date=trade_date,
        effective_as_of_ms=observed_at_ms,
        knowledge_as_of_ms=observed_at_ms,
        purpose="read_only_m2_probe",
        temporal_mode=temporal_mode,
    )
    result = PreviousDayLimitPoolFunction(provider, calendar).execute(
        DataContext("real-previous-day-limit-pool", "READ_ONLY", observed_at_ms),
        request,
    )
    structure = build_previous_day_limit_structure(result)
    return {
        "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "result_status": result.status,
        "actual_source": result.actual_source,
        "actual_trade_date": result.actual_trade_date,
        "requested_trade_date": result.requested_trade_date,
        "completeness": result.completeness,
        "missing_fields": result.missing_fields,
        "missing_symbols": result.missing_symbols,
        "data": json.loads(canonical_json(result.data)) if result.data is not None else None,
        "available_at_ms": result.available_at_ms,
        "observed_at_ms": result.observed_at_ms,
        "temporal_mode": result.temporal_mode,
        "fetch_completed_at_ms": result.fetch_completed_at_ms,
        "content_hash": result.content_hash,
        "structure_fact": json.loads(canonical_json(structure.as_mapping())),
        "calendar_semantic_hash": calendar.semantic_hash,
        "provenance": json.loads(canonical_json(result.provenance)),
        "redis": redis_summary,
        "read_only": True,
        "side_effect_boundary": "Redis TYPE/HLEN/HSCAN/GET only; no Redis/TD write/repair/notification",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-trade-date", required=True)
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.environ.get("REDIS_DB", "0")))
    parser.add_argument(
        "--temporal-mode",
        choices=("HISTORICAL", "REPLAY", "LIVE"),
        default="HISTORICAL",
        help="Historical/replay require verified availability; LIVE permits a completed prefetch before cutoff.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    observed_at = datetime.now(timezone.utc)
    result = run_real_previous_day_limit_pool(
        trade_date=args.trade_date,
        previous_trade_date=args.previous_trade_date,
        observed_at=observed_at,
        redis_kwargs={
            "host": args.redis_host,
            "port": args.redis_port,
            "db": args.redis_db,
            "password": os.environ.get("REDIS_PASSWORD"),
        },
        temporal_mode=args.temporal_mode,
    )
    result["observed_at"] = observed_at.isoformat()
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the bytes hashed by the caller identical on Windows and Linux;
    # default text mode would translate LF to CRLF on Windows.
    with args.output.open("x", encoding="utf-8", newline="") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
