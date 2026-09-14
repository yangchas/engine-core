"""Read-only startup readiness probe against the existing Redis Q2 path.

This is an operator/audit tool, not a production coordinator.  It reuses the
existing Redis Q2 adapter and the pure readiness wheel, and it never writes
Redis/TD, consumes RabbitMQ, prefetches data, or dispatches an effect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    FreshnessPolicy,
    RedisQ2ProjectionAdapter,
    TimerSpec,
    TradingCalendarSnapshot,
    assess_startup_readiness,
    build_a_share_session_plan,
)


def _load_calendar(path: Path) -> TradingCalendarSnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return TradingCalendarSnapshot(
        calendar_id=payload["calendar_id"],
        version=payload["version"],
        timezone_name=payload["timezone"],
        declared_valid_from=payload["declared_valid_from"],
        declared_valid_to=payload["declared_valid_to"],
        source_guard_valid_from=payload["source_guard_valid_from"],
        source_guard_valid_to=payload["source_guard_valid_to"],
        trading_dates=tuple(payload["trading_dates"]),
        source_id=payload.get("source_id"),
        observed_at_ms=payload.get("observed_at_ms"),
        evidence_ref=payload.get("evidence_ref"),
    )


def run_probe(
    *,
    client,
    trade_date: str,
    calendar: TradingCalendarSnapshot,
    observed_at: datetime,
    stale_after_ms: int,
    timer_specs: tuple[TimerSpec, ...],
) -> dict[str, object]:
    projection = RedisQ2ProjectionAdapter(client).read(
        trade_date,
        observed_at,
        freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
    )
    as_of_ms = int(observed_at.timestamp() * 1000)
    plan = build_a_share_session_plan(trade_date, calendar)
    readiness = assess_startup_readiness(
        trade_date,
        as_of_ms,
        calendar,
        plan,
        q2=projection,
        timer_specs=timer_specs,
        previous_time_ms=None,
        origin="RECOVERY_CATCHUP",
    )
    return {
        "trade_date": trade_date,
        "observed_at": observed_at.isoformat(),
        "as_of_ms": as_of_ms,
        "calendar_semantic_hash": calendar.semantic_hash,
        "session_plan_hash": plan.content_hash,
        "readiness": {
            "status": readiness.status,
            "phase": readiness.phase,
            "q2_status": readiness.q2_status,
            "q2_consistency_status": readiness.q2_consistency_status,
            "q2_content_hash": readiness.q2_content_hash,
            "q2_coverage": readiness.q2_coverage,
            "q2_oldest_source_time_ms": readiness.q2_oldest_source_time_ms,
            "q2_newest_source_time_ms": readiness.q2_newest_source_time_ms,
            "reference_statuses": readiness.reference_statuses,
            "due_timer_ids": readiness.due_timer_ids,
            "actions": readiness.actions,
            "reasons": readiness.reasons,
            "content_hash": readiness.content_hash,
        },
        "read_only": True,
        "side_effect_boundary": "Redis SMEMBERS/HGETALL only; no TD/Rabbit/write/effect",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stale-after-ms", type=int, required=True)
    args = parser.parse_args()
    if args.stale_after_ms < 0:
        parser.error("stale-after-ms must be non-negative")
    import redis  # Linux runtime dependency only; no import-time connection.

    client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    try:
        result = run_probe(
            client=client,
            trade_date=args.trade_date,
            calendar=_load_calendar(args.calendar),
            observed_at=datetime.now(timezone.utc),
            stale_after_ms=args.stale_after_ms,
            timer_specs=(
                TimerSpec("AUCTION_0926", "09:26:00"),
                TimerSpec("OPENING_0932", "09:32:00"),
            ),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str)
            + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
