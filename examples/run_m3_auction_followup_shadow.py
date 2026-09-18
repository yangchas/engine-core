"""Run one bounded 09:24/09:25 Core auction-node shadow.

This is deliberately narrower than a scheduler.  It validates that a source
projection captured at one business anchor can be admitted to the existing
session/timer boundary and consumed by one in-memory Engine instance.  Earlier
auction projections are supplied by the caller; this function never backfills
them from a later Redis read and never writes or repairs any source.
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
    MarketStateReducer,
    ProbeStrategy,
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
FOLLOWUP_SPECS = {
    "0924": TimerSpec("AUCTION_0924", "09:24:00"),
    "0925": TimerSpec("AUCTION_0925", "09:25:00"),
}
FOLLOWUP_WINDOWS = {
    "0924": (time(9, 24, 0), time(9, 25, 0)),
    "0925": (time(9, 25, 0), time(9, 26, 0)),
}
REQUIRED_PRIOR_TAGS = {"0924": ("0920",), "0925": ("0920", "0924")}


def _strict_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    return value


def _strict_tag(value: str) -> str:
    if value not in FOLLOWUP_SPECS:
        raise ValueError("node_tag must be 0924 or 0925")
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


def _normal_window_valid(node_tag: str, as_of: datetime) -> bool:
    local_time = as_of.astimezone(LOCAL_TZ).time()
    start_time, end_time = FOLLOWUP_WINDOWS[node_tag]
    return start_time <= local_time < end_time


def _source_time_ms(projection: Any) -> int | None:
    value = projection.meta.get("ts") if isinstance(projection.meta, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _blocked(
    *,
    trade_date: str,
    symbol: str,
    node_tag: str,
    origin: str,
    reason: str,
    side_effect_boundary: str,
) -> Mapping[str, Any]:
    return {
        "contract_version": "M3_AuctionFollowupShadowV1",
        "trade_date": trade_date,
        "symbol": symbol,
        "node_tag": node_tag,
        "trigger_id": FOLLOWUP_SPECS[node_tag].timer_id,
        "origin": origin,
        "read_only": True,
        "side_effect_boundary": side_effect_boundary,
        "preflight_gate": "BLOCKED",
        "preflight_failure_is_fail_closed": True,
        "startup_self_check": {"status": "BLOCKED", "reasons": (reason,)},
        "node_dispatched": False,
        "engine": None,
    }


def run_m3_auction_followup_shadow(
    *,
    calendar: Any,
    current_projection: Any,
    prior_projections: Mapping[str, Any],
    trade_date: str,
    symbol: str,
    node_tag: str,
    observed_at: datetime,
    as_of: datetime,
    origin: str = "NORMAL",
) -> Mapping[str, Any]:
    """Admit one already-read 0924/0925 projection to the Core timer seam.

    ``prior_projections`` is intentionally supplied by the caller.  A later
    observation cannot be used to reconstruct a missing earlier business
    anchor.  Q2 is optional for these source-owned auction nodes; no Q2 read
    is performed here.
    """

    trade_date = _strict_date(trade_date)
    node_tag = _strict_tag(node_tag)
    if origin not in {"NORMAL", "RECOVERY_CATCHUP"}:
        raise ValueError("origin must be NORMAL or RECOVERY_CATCHUP")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if as_of < observed_at:
        raise ValueError("as_of cannot precede observed_at")
    if current_projection.trade_date != trade_date:
        raise ValueError("current projection trade_date does not match trade_date")
    if current_projection.tag != node_tag:
        raise ValueError("current projection tag does not match node_tag")

    if origin == "NORMAL" and not _normal_window_valid(node_tag, as_of):
        return _blocked(
            trade_date=trade_date,
            symbol=symbol,
            node_tag=node_tag,
            origin=origin,
            reason="normal_capture_window_invalid",
            side_effect_boundary="no source read; no Engine dispatch",
        )

    required = REQUIRED_PRIOR_TAGS[node_tag]
    missing_prior = tuple(tag for tag in required if tag not in prior_projections)
    if missing_prior:
        return _blocked(
            trade_date=trade_date,
            symbol=symbol,
            node_tag=node_tag,
            origin=origin,
            reason="missing_prior_projection:" + ",".join(missing_prior),
            side_effect_boundary="already-read projections only; no backfill",
        )

    for tag in required:
        if prior_projections[tag].tag != tag:
            return _blocked(
                trade_date=trade_date,
                symbol=symbol,
                node_tag=node_tag,
                origin=origin,
                reason=f"prior_projection_tag_mismatch:{tag}",
                side_effect_boundary="already-read projections only; no backfill",
            )

    plan = build_a_share_session_plan(trade_date, calendar)
    coordinator = SessionRuntimeCoordinator(
        trade_date=trade_date,
        calendar=calendar,
        session_plan=plan,
        timer_specs=(FOLLOWUP_SPECS[node_tag],),
        q2_optional_timer_ids=(FOLLOWUP_SPECS[node_tag].timer_id,),
    )
    as_of_ms = _epoch_ms(as_of)
    poll = coordinator.poll(as_of_ms=as_of_ms, q2=None, origin=origin)
    firing = next(iter(poll.dispatchable_firings), None)
    if firing is None:
        return _blocked(
            trade_date=trade_date,
            symbol=symbol,
            node_tag=node_tag,
            origin=origin,
            reason="timer_not_dispatchable",
            side_effect_boundary="no Engine dispatch",
        )

    cutoff_ms = firing.scheduled_time_ms if origin == "RECOVERY_CATCHUP" else firing.fired_time_ms
    projections = [current_projection, *(prior_projections[tag] for tag in required)]
    for projection in projections:
        if projection.trade_date != trade_date:
            return _blocked(
                trade_date=trade_date,
                symbol=symbol,
                node_tag=node_tag,
                origin=origin,
                reason="projection_trade_date_mismatch",
                side_effect_boundary="already-read projections only; no backfill",
            )
        if projection.observed_at_ms > cutoff_ms:
            return _blocked(
                trade_date=trade_date,
                symbol=symbol,
                node_tag=node_tag,
                origin=origin,
                reason=f"projection_observed_after_{node_tag}_cutoff",
                side_effect_boundary="already-read projections only; no backfill",
            )
        source_time_ms = _source_time_ms(projection)
        if source_time_ms is not None and source_time_ms > cutoff_ms:
            return _blocked(
                trade_date=trade_date,
                symbol=symbol,
                node_tag=node_tag,
                origin=origin,
                reason=f"projection_source_time_after_{node_tag}_cutoff",
                side_effect_boundary="already-read projections only; no backfill",
            )

    projection = build_engine_projection(
        current_projection,
        trade_date=trade_date,
        symbol=symbol,
    )
    if projection.status in {DataStatus.MISSING, DataStatus.INVALID}:
        return _blocked(
            trade_date=trade_date,
            symbol=symbol,
            node_tag=node_tag,
            origin=origin,
            reason="auction_projection_status:" + projection.status.value,
            side_effect_boundary="already-read projections only; no backfill",
        )

    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction", 0, 10**15),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="AUCTION",
    )
    engine.submit(
        EngineSignal(
            f"m3-{node_tag}-market-{symbol}",
            firing.fired_time_ms,
            1,
            SignalKind.MARKET_UPDATE,
            projection,
        )
    )
    engine.submit(
        EngineSignal(
            f"m3-{node_tag}-timer-{symbol}",
            firing.fired_time_ms,
            2,
            SignalKind.TIMER,
            {"trigger_id": firing.timer_id},
        )
    )
    engine_result = engine.run_until_empty()
    coordinator.acknowledge(firing)
    strategy_result = engine_result.strategy_results[-1]
    return {
        "contract_version": "M3_AuctionFollowupShadowV1",
        "trade_date": trade_date,
        "symbol": symbol,
        "node_tag": node_tag,
        "trigger_id": firing.timer_id,
        "origin": origin,
        "read_only": True,
        "side_effect_boundary": "already-read Redis projection + in-memory Core only",
        "startup_self_check": {
            "status": poll.readiness.status,
            "node_readiness": "DISPATCHABLE",
            "q2_policy": "OPTIONAL_FOR_SOURCE_OWNED_AUCTION_NODE",
            "reasons": poll.readiness.reasons,
            "actions": poll.readiness.actions,
            "calendar_hash": calendar.semantic_hash,
            "session_plan_hash": plan.content_hash,
        },
        "projection": {
            "status": current_projection.status,
            "observed_at_ms": current_projection.observed_at_ms,
            "source_time_min_ms": projection.oldest_source_time_ms,
            "source_time_max_ms": projection.newest_source_time_ms,
            "content_hash": current_projection.content_hash,
        },
        "timer": {
            "scheduled_time_ms": firing.scheduled_time_ms,
            "fired_time_ms": firing.fired_time_ms,
            "origin": firing.origin,
            "content_hash": firing.content_hash,
        },
        "node_dispatched": True,
        "preflight_gate": "PASS",
        "engine": {
            "same_engine_instance": True,
            "processed_signals": engine_result.processed_signals,
            "strategy_result_count": len(engine_result.strategy_results),
            "strategy_result_hash": strategy_result.content_hash,
            "snapshot_hash": strategy_result.trace["snapshot_hash"],
            "fact_status": "NODE_ONLY",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--node-tag", choices=("0924", "0925"), required=True)
    parser.add_argument("--calendar-file", type=Path, required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--origin", choices=("NORMAL", "RECOVERY_CATCHUP"), default="NORMAL")
    args = parser.parse_args()
    trade_date = _strict_date(args.trade_date)
    observed_at = _parse_datetime(args.observed_at, trade_date=trade_date)
    as_of = _parse_datetime(args.as_of, trade_date=trade_date)
    calendar = _load_calendar(args.calendar_file, trade_date=trade_date)

    # Keep the normal capture guard ahead of the Redis client/read.  A late
    # invocation must not even observe a source and then label the result as a
    # normal node.  Recovery remains explicit and fail-closed at the function
    # boundary below.
    if args.origin == "NORMAL" and not _normal_window_valid(args.node_tag, as_of):
        result = _blocked(
            trade_date=trade_date,
            symbol=args.symbol,
            node_tag=args.node_tag,
            origin=args.origin,
            reason="normal_capture_window_invalid",
            side_effect_boundary="no source read; no Engine dispatch",
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(canonical_json(result))
        print(canonical_json(result))
        return 0

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
        tags = (*REQUIRED_PRIOR_TAGS[args.node_tag], args.node_tag)
        projections = read_redis_auction_projection(
            client,
            trade_date=trade_date,
            observed_at_ms=_epoch_ms(observed_at),
            tags=tags,
            symbols=(args.symbol,),
        )
        by_tag = {item.tag: item for item in projections}
        result = run_m3_auction_followup_shadow(
            calendar=calendar,
            current_projection=by_tag[args.node_tag],
            prior_projections={tag: by_tag[tag] for tag in REQUIRED_PRIOR_TAGS[args.node_tag]},
            trade_date=trade_date,
            symbol=args.symbol,
            node_tag=args.node_tag,
            observed_at=observed_at,
            as_of=as_of,
            origin=args.origin,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(canonical_json(result))
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
