"""Run the minimal M3 09:20 Core shadow with a real Redis read.

This is a bounded composition entrypoint, not a scheduler or a production
owner.  It performs one Q2 prefetch, validates the explicit calendar/session
identity through ``SessionRuntimeCoordinator``, and only then sends the
already-read 0920 auction projection through one in-memory Engine instance.
If the preflight or cutoff gate fails, no Engine node is dispatched and no
fallback/recovery path is attempted.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    DataStatus,
    DeterministicEngine,
    EngineSignal,
    FreshnessPolicy,
    MarketStateReducer,
    ProbeStrategy,
    RedisQ2ProjectionAdapter,
    SessionRuntimeCoordinator,
    SignalKind,
    TimerSpec,
    WindowManager,
    WindowSpec,
    build_a_share_session_plan,
    canonical_json,
    read_redis_auction_projection,
)

try:  # Script execution resolves sibling examples directly.
    from run_morning_vertical_slice_shadow import _load_calendar  # type: ignore
    from run_real_redis_auction_engine_shadow import build_engine_projection  # type: ignore
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.run_morning_vertical_slice_shadow import _load_calendar
    from examples.run_real_redis_auction_engine_shadow import build_engine_projection


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
AUCTION_0920_SPEC = TimerSpec("AUCTION_0920", "09:20:00")


def _strict_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    return value


def _epoch_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _parse_datetime(value: str, *, trade_date: str) -> datetime:
    text = value.strip()
    if len(text) == 8:
        text = f"{trade_date}T{text}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed


def run_m3_0920_shadow(
    *,
    client: Any,
    calendar: Any,
    auction_projection: Any,
    trade_date: str,
    symbol: str,
    observed_at: datetime,
    as_of: datetime,
    stale_after_ms: int,
    origin: str = "NORMAL",
) -> Mapping[str, Any]:
    """Run one 0920 node only after a truthful preflight."""

    trade_date = _strict_date(trade_date)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if as_of < observed_at:
        raise ValueError("as_of cannot precede observed_at")
    if origin not in {"NORMAL", "RECOVERY_CATCHUP"}:
        raise ValueError("origin must be NORMAL or RECOVERY_CATCHUP")

    # Exactly one Q2 prefetch.  The adapter is the existing verified Redis
    # path; this function never repairs, writes, or retries another source.
    q2 = RedisQ2ProjectionAdapter(client).read(
        trade_date,
        observed_at,
        freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
    )
    plan = build_a_share_session_plan(trade_date, calendar)
    coordinator = SessionRuntimeCoordinator(
        trade_date=trade_date,
        calendar=calendar,
        session_plan=plan,
        timer_specs=(AUCTION_0920_SPEC,),
    )
    as_of_ms = _epoch_ms(as_of)
    poll = coordinator.poll(
        as_of_ms=as_of_ms,
        q2=q2,
        origin=origin,
    )
    firing = next(
        (item for item in poll.dispatchable_firings if item.timer_id == "AUCTION_0920"),
        None,
    )

    base: dict[str, Any] = {
        "contract_version": "M3_0920_ShadowV1",
        "trade_date": trade_date,
        "symbol": symbol,
        "origin": origin,
        "read_only": True,
        "prefetch_calls": 1,
        "side_effect_boundary": (
            "Redis SMEMBERS/HGETALL + Redis auction HGETALL + in-memory Core only"
        ),
        "startup_self_check": {
            "trade_date": trade_date,
            "timezone": plan.timezone_name,
            "calendar_hash": calendar.semantic_hash,
            "session_plan_hash": plan.content_hash,
            "status": poll.readiness.status,
            "reasons": poll.readiness.reasons,
            "actions": poll.readiness.actions,
        },
        "q2": {
            "status": q2.status.value,
            "consistency_status": q2.consistency_status,
            "coverage": q2.coverage,
            "content_hash": q2.content_hash,
            "observed_at_ms": q2.envelope.observed_time_ms,
            "source_time_min_ms": q2.oldest_source_time_ms,
            "source_time_max_ms": q2.newest_source_time_ms,
        },
        "timer": {
            "due": firing is not None,
            "readiness_dispatchable": firing is not None,
            "deferred_timer_ids": poll.deferred_timer_ids,
            "fired": None,
        },
        "node_dispatched": False,
        "engine": None,
    }

    if firing is None:
        base["preflight_gate"] = "BLOCKED"
        base["preflight_failure_is_fail_closed"] = True
        return base

    # A recovery firing must not use a cohort observed after the business
    # anchor to reconstruct the old cutoff.  A normal firing uses its actual
    # observation boundary; a recovery firing is fail-closed unless the input
    # was already observed by the scheduled 09:20 anchor.
    cutoff_ms = (
        firing.scheduled_time_ms
        if firing.origin == "RECOVERY_CATCHUP"
        else firing.fired_time_ms
    )
    if q2.envelope.observed_time_ms > cutoff_ms:
        base["preflight_gate"] = "BLOCKED"
        base["preflight_failure_is_fail_closed"] = True
        base["timer"]["fired"] = {
            "scheduled_time_ms": firing.scheduled_time_ms,
            "fired_time_ms": firing.fired_time_ms,
            "origin": firing.origin,
            "content_hash": firing.content_hash,
        }
        base["startup_self_check"]["reasons"] = tuple(
            list(base["startup_self_check"]["reasons"])
            + ["q2_observed_after_0920_firing"]
        )
        return base

    if auction_projection.observed_at_ms > cutoff_ms:
        base["preflight_gate"] = "BLOCKED"
        base["preflight_failure_is_fail_closed"] = True
        base["startup_self_check"]["reasons"] = tuple(
            list(base["startup_self_check"]["reasons"])
            + ["auction_projection_observed_after_0920_firing"]
        )
        return base

    projection = build_engine_projection(
        auction_projection,
        trade_date=trade_date,
        symbol=symbol,
    )
    if projection.status in {DataStatus.MISSING, DataStatus.INVALID}:
        base["preflight_gate"] = "BLOCKED"
        base["preflight_failure_is_fail_closed"] = True
        base["startup_self_check"]["reasons"] = tuple(
            list(base["startup_self_check"]["reasons"])
            + ["auction_projection_status:" + projection.status.value]
        )
        return base
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction", 0, 10**15),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="AUCTION",
    )
    engine.submit(
        EngineSignal(
            "m3-0920-market-" + symbol,
            firing.fired_time_ms,
            1,
            SignalKind.MARKET_UPDATE,
            projection,
        )
    )
    engine.submit(
        EngineSignal(
            "m3-0920-timer-" + symbol,
            firing.fired_time_ms,
            2,
            SignalKind.TIMER,
            {"trigger_id": "AUCTION_0920"},
        )
    )
    engine_result = engine.run_until_empty()
    coordinator.acknowledge(firing)
    strategy_result = engine_result.strategy_results[-1]
    base.update(
        {
            "preflight_gate": "PASS",
            "node_dispatched": True,
            "timer": {
                "due": True,
                "readiness_dispatchable": True,
                "deferred_timer_ids": (),
                "fired": {
                    "scheduled_time_ms": firing.scheduled_time_ms,
                    "fired_time_ms": firing.fired_time_ms,
                    "origin": firing.origin,
                    "content_hash": firing.content_hash,
                },
            },
            "engine": {
                "same_engine_instance": True,
                "processed_signals": engine_result.processed_signals,
                "strategy_result_count": len(engine_result.strategy_results),
                "strategy_result_hash": strategy_result.content_hash,
                "snapshot_hash": strategy_result.trace["snapshot_hash"],
                "projection_status": projection.status.value,
                "projection_source_time_min_ms": projection.oldest_source_time_ms,
                "projection_source_time_max_ms": projection.newest_source_time_ms,
            },
        }
    )
    return base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--calendar-file", type=Path, required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--origin", choices=("NORMAL", "RECOVERY_CATCHUP"), default="NORMAL")
    args = parser.parse_args()
    trade_date = _strict_date(args.trade_date)
    observed_at = _parse_datetime(args.observed_at, trade_date=trade_date)
    as_of = _parse_datetime(args.as_of, trade_date=trade_date)
    calendar = _load_calendar(args.calendar_file, trade_date=trade_date)

    import redis  # type: ignore[import-not-found]

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
        projections = read_redis_auction_projection(
            client,
            trade_date=trade_date,
            observed_at_ms=_epoch_ms(observed_at),
            tags=("0920",),
            symbols=(args.symbol,),
        )
        result = run_m3_0920_shadow(
            client=client,
            calendar=calendar,
            auction_projection=projections[0],
            trade_date=trade_date,
            symbol=args.symbol,
            observed_at=observed_at,
            as_of=as_of,
            stale_after_ms=args.stale_after_ms,
            origin=args.origin,
        )
    finally:
        client.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(canonical_json(result))
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
