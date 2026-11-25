"""Run the date-bound Kaipan hot-plate cache through the core boundary.

The Redis path is explicitly read-only.  A cache update timestamp is retained
as observation metadata but is never promoted to historical availability.
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
    LEGACY_KAIPAN_OBSERVED_FIELD_UNITS,
    LiveInputReader,
    probe_rows_and_summary,
    DataContext,
    DataRequest,
    HotPlatesFunction,
    RedisHotPlatesProvider,
    build_calendar_snapshot,
    canonical_json,
)


def _date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("date must be strict YYYY-MM-DD")
    return value


def _read_redis_rows(
    client: Any,
    trade_date: str,
) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    reader = LiveInputReader(client, now_ms=lambda: int(time.time() * 1000))
    return probe_rows_and_summary(reader.read("hot_plates", trade_date=trade_date))


def run_real_hot_plates(
    *,
    trade_date: str,
    observed_at: datetime,
    redis_kwargs: dict[str, Any],
    fetch_rows_override: Any = None,
    redis_summary_override: dict[str, Any] | None = None,
    calendar_trading_dates: Iterable[str] | None = None,
    calendar_source_id: str = "explicit_probe_calendar",
    calendar_evidence_ref: str | None = None,
    temporal_mode: str = "HISTORICAL",
    observed_units: bool = False,
) -> dict[str, Any]:
    trade_date = _date(trade_date)
    observed_at_ms = int(observed_at.timestamp() * 1000)
    calendar_dates = tuple(
        _date(value) for value in (calendar_trading_dates or (trade_date,))
    )
    if trade_date not in calendar_dates:
        raise ValueError("trade date is absent from supplied calendar")
    calendar_bounds = tuple(sorted(set(calendar_dates)))
    calendar = build_calendar_snapshot(
        calendar_bounds,
        version="real-hot-plates-probe-" + trade_date,
        declared_valid_from=calendar_bounds[0],
        declared_valid_to=calendar_bounds[-1],
        source_guard_valid_from=calendar_bounds[0],
        source_guard_valid_to=calendar_bounds[-1],
        source_id=calendar_source_id,
        observed_at_ms=observed_at_ms,
        evidence_ref=calendar_evidence_ref or "probe://explicit-hot-plate-trade-date",
    )
    redis_summary: dict[str, Any] = redis_summary_override or {}

    def fetch_rows(date: str) -> Iterable[Mapping[str, Any]]:
        nonlocal redis_summary
        if fetch_rows_override is not None:
            return fetch_rows_override(date)
        import redis  # type: ignore[import-not-found]

        client = redis.Redis(**redis_kwargs)
        client.ping()
        try:
            rows, redis_summary = _read_redis_rows(client, date)
            return rows
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    def fetch_metadata(date: str) -> Mapping[str, Any] | None:
        metadata = redis_summary.get("meta")
        return metadata if isinstance(metadata, Mapping) else None

    provider = RedisHotPlatesProvider(
        fetch_rows,
        observed_at_ms=lambda: observed_at_ms,
        metadata=fetch_metadata,
        source_id="redis_hot_plates",
        source_schema="HotPlatesV1",
        evidence_ref="redis://cache:hot_plates/" + trade_date,
        verified_field_units=(
            dict(LEGACY_KAIPAN_OBSERVED_FIELD_UNITS) if observed_units else None
        ),
    )
    request = DataRequest(
        request_id="real-hot-plates-" + trade_date,
        function_id="hot_plates",
        trade_date=trade_date,
        effective_as_of_ms=observed_at_ms,
        knowledge_as_of_ms=observed_at_ms,
        purpose="read_only_m2_probe",
        temporal_mode=temporal_mode,
    )
    result = HotPlatesFunction(provider, calendar).execute(
        DataContext("real-hot-plates", "READ_ONLY", observed_at_ms),
        request,
    )
    return {
        "trade_date": trade_date,
        "result_status": result.status,
        "actual_source": result.actual_source,
        "actual_trade_date": result.actual_trade_date,
        "requested_trade_date": result.requested_trade_date,
        "completeness": result.completeness,
        "missing_fields": result.missing_fields,
        "data": json.loads(canonical_json(result.data)) if result.data is not None else None,
        "available_at_ms": result.available_at_ms,
        "observed_at_ms": result.observed_at_ms,
        "content_hash": result.content_hash,
        "calendar_semantic_hash": calendar.semantic_hash,
        "provenance": json.loads(canonical_json(result.provenance)),
        "redis": redis_summary,
        "read_only": True,
        "side_effect_boundary": "Redis TYPE/HLEN/HSCAN/GET only; no Redis/TD write/repair/notification",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.environ.get("REDIS_DB", "0")))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--temporal-mode", choices=("HISTORICAL", "REPLAY", "LIVE"), default="LIVE",
        help="LIVE accepts the adapter-completion exception (a read made now); "
        "HISTORICAL requires a verified available_at",
    )
    parser.add_argument(
        "--observed-units", action="store_true",
        help="apply the units established from the legacy Kaipan path "
        "(strength/hot=score, net_inflow_yi=yi) instead of masking them",
    )
    args = parser.parse_args()
    observed_at = datetime.now(timezone.utc)
    result = run_real_hot_plates(
        trade_date=args.trade_date,
        observed_at=observed_at,
        redis_kwargs={
            "host": args.redis_host,
            "port": args.redis_port,
            "db": args.redis_db,
            "password": os.environ.get("REDIS_PASSWORD"),
        },
        temporal_mode=args.temporal_mode,
        observed_units=args.observed_units,
    )
    result["observed_at"] = observed_at.isoformat()
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
