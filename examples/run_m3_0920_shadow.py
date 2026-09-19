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
from datetime import date, datetime, time, timezone
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
NORMAL_CAPTURE_WINDOW_START = time(9, 15, 0)
# Source reads are admitted only once the 09:20 timer is due.  The wider
# capture window remains useful for static preflight, but it must not cause a
# one-shot runner to consume Redis before the evaluation node can fire.
NORMAL_TIMER_DUE_TIME = time(9, 20, 0)
# The bounded NORMAL admission window is closed on both ends:
# [09:15:00, 09:21:00] in Asia/Shanghai.  The exact 09:21:00 boundary is
# retained for the existing runbook contract; 09:21:00.001 is outside it.
NORMAL_CAPTURE_WINDOW_END = time(9, 21, 0)


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


def _normal_capture_window_reason(as_of: datetime) -> str | None:
    """Return a fail-closed reason when NORMAL is outside its capture window."""

    local_time = as_of.astimezone(LOCAL_TZ).time()
    if local_time < NORMAL_CAPTURE_WINDOW_START:
        return "normal_capture_window_not_started"
    if local_time > NORMAL_CAPTURE_WINDOW_END:
        return "normal_capture_window_expired"
    return None


def _normal_preflight_reason(as_of: datetime) -> str | None:
    """Return the admission reason before any live source read occurs."""

    reason = _normal_capture_window_reason(as_of)
    if reason is not None:
        return reason
    if as_of.astimezone(LOCAL_TZ).time() < NORMAL_TIMER_DUE_TIME:
        return "timer_not_due_preflight"
    return None


def run_m3_0920_shadow(
    *,
    client: Any,
    calendar: Any,
    auction_projection: Any,
    trade_date: str,
    symbol: str,
    observed_at: datetime | None,
    as_of: datetime | None,
    stale_after_ms: int,
    origin: str = "NORMAL",
    q2_snapshot: Any | None = None,
    preflight_at: datetime | None = None,
) -> Mapping[str, Any]:
    """Run one 0920 node only after a truthful preflight."""

    trade_date = _strict_date(trade_date)
    for name, value in (
        ("observed_at", observed_at),
        ("as_of", as_of),
        ("preflight_at", preflight_at),
    ):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{name} must be timezone-aware")
        if value is not None and value.astimezone(LOCAL_TZ).date().isoformat() != trade_date:
            raise ValueError(f"{name} local date does not match trade_date")
    if observed_at is not None and as_of is not None and as_of < observed_at:
        raise ValueError("as_of cannot precede observed_at")
    admission_at = preflight_at or as_of or observed_at
    if admission_at is None:
        raise ValueError("one of preflight_at, as_of, or observed_at is required")
    if origin not in {"NORMAL", "RECOVERY_CATCHUP"}:
        raise ValueError("origin must be NORMAL or RECOVERY_CATCHUP")

    # The normal 09:20 acceptance is a live observation window, not a label
    # that may be applied to a post-market rerun.  The runbook keeps the
    # bounded window at 09:15-09:21; enforce its end here as well so callers
    # cannot accidentally turn a late observation into NORMAL evidence.
    normal_window_reason = (
        _normal_preflight_reason(admission_at) if origin == "NORMAL" else None
    )
    if normal_window_reason is not None:
        # Return the same fail-closed evidence shape as other preflight
        # failures, but do not read Q2 or dispatch an Engine node.
        return {
            "contract_version": "M3_0920_ShadowV1",
            "trade_date": trade_date,
            "symbol": symbol,
            "origin": origin,
            "read_only": True,
            "prefetch_calls": 0,
            "side_effect_boundary": "no source read; no Engine dispatch",
            "startup_self_check": {
                "trade_date": trade_date,
                "status": "BLOCKED",
                "reasons": (normal_window_reason,),
                "actions": (),
            },
            "q2": None,
            "timer": {"due": False, "readiness_dispatchable": False, "deferred_timer_ids": (), "fired": None},
            "node_dispatched": False,
            "engine": None,
            "preflight_gate": "BLOCKED",
            "preflight_failure_is_fail_closed": True,
        }

    # Exactly one Q2 prefetch.  The adapter is the existing verified Redis
    # path; this function never repairs, writes, or retries another source.
    # Live callers may pass the already-read snapshot so the timestamp in the
    # result is the actual completion time of the source read, not a timestamp
    # captured before the read started.
    if q2_snapshot is None:
        q2 = RedisQ2ProjectionAdapter(client).read(
            trade_date,
            observed_at,
            freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
        )
    else:
        q2 = q2_snapshot
    evaluation_at = as_of or datetime.now(LOCAL_TZ)
    if evaluation_at.tzinfo is None or evaluation_at.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if evaluation_at.astimezone(LOCAL_TZ).date().isoformat() != trade_date:
        raise ValueError("as_of local date does not match trade_date")
    plan = build_a_share_session_plan(trade_date, calendar)
    coordinator = SessionRuntimeCoordinator(
        trade_date=trade_date,
        calendar=calendar,
        session_plan=plan,
        timer_specs=(AUCTION_0920_SPEC,),
    )
    as_of_ms = _epoch_ms(evaluation_at)
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
        "timing": {
            "preflight_at_ms": _epoch_ms(admission_at),
            "evaluation_at_ms": _epoch_ms(evaluation_at),
        },
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
        base["startup_self_check"]["reasons"] = tuple(
            list(base["startup_self_check"]["reasons"]) + ["timer_not_due"]
        )
        return base

    if origin == "NORMAL":
        post_read_window_reason = _normal_capture_window_reason(evaluation_at)
        if post_read_window_reason is not None:
            base["preflight_gate"] = "BLOCKED"
            base["preflight_failure_is_fail_closed"] = True
            base["startup_self_check"]["reasons"] = tuple(
                list(base["startup_self_check"]["reasons"])
                + ["normal_capture_window_expired_after_read"]
            )
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

    if auction_projection is None:
        base["preflight_gate"] = "BLOCKED"
        base["preflight_failure_is_fail_closed"] = True
        base["startup_self_check"]["reasons"] = tuple(
            list(base["startup_self_check"]["reasons"])
            + ["auction_projection_missing"]
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
    parser.add_argument(
        "--observed-at",
        help="deterministic preflight time for fixtures; live runs capture source-read times",
    )
    parser.add_argument(
        "--as-of",
        help="deterministic evaluation time for fixtures; live runs use post-read time",
    )
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--origin", choices=("NORMAL", "RECOVERY_CATCHUP"), default="NORMAL")
    args = parser.parse_args()
    trade_date = _strict_date(args.trade_date)
    if bool(args.observed_at) != bool(args.as_of):
        parser.error("--observed-at and --as-of must be supplied together")
    explicit_observed_at = (
        _parse_datetime(args.observed_at, trade_date=trade_date)
        if args.observed_at
        else None
    )
    explicit_as_of = (
        _parse_datetime(args.as_of, trade_date=trade_date) if args.as_of else None
    )
    preflight_at = explicit_observed_at or explicit_as_of or datetime.now(LOCAL_TZ)
    as_of = explicit_as_of or preflight_at
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
        # Do not even read the projection when NORMAL is outside the bounded
        # trading-day capture window.  The library function also enforces this
        # guard, but the CLI must make the no-source-read boundary explicit.
        projection = None
        preflight_window_reason = (
            _normal_preflight_reason(preflight_at)
            if args.origin == "NORMAL"
            else None
        )
        if preflight_window_reason is None:
            projections = read_redis_auction_projection(
                client,
                trade_date=trade_date,
                observed_at_ms=None,
                tags=("0920",),
                symbols=(args.symbol,),
            )
            projection = projections[0]
            q2_snapshot = RedisQ2ProjectionAdapter(client).read(
                trade_date,
                None,
                freshness_policy=FreshnessPolicy(stale_after_ms=args.stale_after_ms),
            )
            # Evaluation cutoff is captured only after both source reads have
            # completed.  It must not be confused with either source's own
            # observation timestamp.
            as_of = explicit_as_of or datetime.now(LOCAL_TZ)
        else:
            q2_snapshot = None
        result = run_m3_0920_shadow(
            client=client,
            calendar=calendar,
            auction_projection=projection,
            trade_date=trade_date,
            symbol=args.symbol,
            observed_at=preflight_at,
            as_of=as_of,
            stale_after_ms=args.stale_after_ms,
            origin=args.origin,
            q2_snapshot=q2_snapshot,
            preflight_at=preflight_at,
        )
    finally:
        client.close()
    rendered = canonical_json(result)
    print(rendered)
    # A NORMAL invocation before 09:20 is a preflight observation only.  It
    # must not consume the write-once final artifact path; the runbook starts
    # the source-reading command at the timer boundary.
    timer_not_due = any(
        str(reason).startswith("timer_not_due")
        for reason in result.get("startup_self_check", {}).get("reasons", ())
    )
    if args.origin == "NORMAL" and timer_not_due:
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
