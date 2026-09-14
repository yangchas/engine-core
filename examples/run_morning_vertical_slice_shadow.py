"""Run a bounded, read-only morning Core shadow.

The runner is deliberately a thin composition boundary.  It reuses the
existing Redis Q2 adapter, the read-only TD auction query shape, the existing
auction/opening fact wheels, and the pure SessionTimer calculation.  It does
not consume Rabbit, write Redis/TD, recover data, send notifications, or run a
strategy.

The output is evidence, not a production decision.  Missing reference-data
metadata is reported as ``UNAVAILABLE`` and never blocks the market/auction
fact portions that are independently available.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    FreshnessPolicy,
    RedisQ2ProjectionAdapter,
    SessionPlan,
    TimerSpec,
    TradingCalendarSnapshot,
    build_a_share_session_plan,
    build_opening_transition_fact,
    build_calendar_snapshot,
    due_timer_firings,
    normalize_auction_change_ratio,
    semantic_hash,
)

try:  # Script execution resolves sibling examples directly.
    from run_real_auction_shadow import build_shadow_from_rows, query_rows  # noqa: E402
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.run_real_auction_shadow import (  # noqa: E402
        build_shadow_from_rows,
        query_rows,
    )


CORE_TIMER_SPECS = (
    TimerSpec("AUCTION_0926", "09:26:00"),
    TimerSpec("OPENING_0932", "09:32:00"),
)


def _strict_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    return value


def _strict_symbol(value: str) -> str:
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("symbol must be a six-digit stock code")
    return value


def _strict_symbols(value: str) -> tuple[str, ...]:
    result = tuple(sorted({_strict_symbol(item.strip()) for item in value.split(",") if item.strip()}))
    if not result:
        raise ValueError("at least one symbol is required")
    return result


def _epoch_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _parse_datetime(value: str, *, default_date: str) -> datetime:
    text = value.strip()
    if len(text) == 8:
        text = f"{default_date}T{text}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


def _load_calendar(path: Path, *, trade_date: str) -> TradingCalendarSnapshot:
    """Load an existing immutable calendar evidence file."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    required = (
        "calendar_id",
        "version",
        "timezone",
        "declared_valid_from",
        "declared_valid_to",
        "source_guard_valid_from",
        "source_guard_valid_to",
        "trading_dates",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError("calendar evidence missing fields: " + ",".join(missing))
    snapshot = build_calendar_snapshot(
        payload["trading_dates"],
        calendar_id=payload["calendar_id"],
        version=payload["version"],
        timezone_name=payload["timezone"],
        declared_valid_from=payload["declared_valid_from"],
        declared_valid_to=payload["declared_valid_to"],
        source_guard_valid_from=payload["source_guard_valid_from"],
        source_guard_valid_to=payload["source_guard_valid_to"],
        source_id=payload.get("source_id"),
        observed_at_ms=payload.get("observed_at_ms"),
        evidence_ref=payload.get("evidence_ref"),
    )
    if not snapshot.is_trading_day(trade_date):
        raise ValueError("calendar does not mark trade_date as a trading day")
    return snapshot


def build_timer_evidence(
    plan: SessionPlan,
    *,
    current_time_ms: int,
    previous_time_ms: int | None = None,
    already_fired: Sequence[str] = (),
) -> dict[str, Any]:
    """Build normal and cold-start timer evidence for Core-owned nodes."""

    normal = due_timer_firings(
        plan,
        CORE_TIMER_SPECS,
        previous_time_ms=previous_time_ms,
        current_time_ms=current_time_ms,
        already_fired=already_fired,
        origin="NORMAL",
    )
    recovery = due_timer_firings(
        plan,
        CORE_TIMER_SPECS,
        previous_time_ms=None,
        current_time_ms=current_time_ms,
        already_fired=already_fired,
        origin="RECOVERY_CATCHUP",
    )
    return {
        "core_timer_ids": [item.timer_id for item in CORE_TIMER_SPECS],
        "source_freeze_owned_by": "t1-v2 (09:20/09:24/09:25)",
        "normal": [
            {
                "timer_id": item.timer_id,
                "scheduled_time_ms": item.scheduled_time_ms,
                "fired_time_ms": item.fired_time_ms,
                "origin": item.origin,
                "late_by_ms": item.late_by_ms,
                "content_hash": item.content_hash,
            }
            for item in normal
        ],
        "recovery_catchup": [
            {
                "timer_id": item.timer_id,
                "scheduled_time_ms": item.scheduled_time_ms,
                "fired_time_ms": item.fired_time_ms,
                "origin": item.origin,
                "late_by_ms": item.late_by_ms,
                "content_hash": item.content_hash,
            }
            for item in recovery
        ],
    }


def _opening_fact_from_q2(projection: Any, *, symbol: str, auction_row: Mapping[str, Any]) -> dict[str, Any]:
    quote = projection.quotes.get(symbol)
    if quote is None:
        return {
            "status": "unavailable",
            "symbol": symbol,
            "reason": "symbol_not_in_q2_projection",
        }
    auction_ratio = normalize_auction_change_ratio(auction_row.get("chg_bp"))
    auction_change_pct = auction_ratio * 100.0 if auction_ratio is not None else None
    return build_opening_transition_fact(
        auction_change_pct,
        {
            "symbol": symbol,
            "timestamp_ms": quote.source_record_time_ms,
            "price_milli": quote.price_milli,
            "previous_close_milli": quote.pre_close_milli,
            "amount_2m_yuan": quote.amount_2m_yuan,
            "limit_state": quote.limit_state,
            "name": quote.name,
            "speed_1m": quote.speed_1m_bp,
        },
    )


def build_morning_shadow(
    *,
    trade_date: str,
    symbol: str,
    projection: Any,
    auction_rows: Sequence[Sequence[Any] | Mapping[str, Any]],
    calendar: TradingCalendarSnapshot,
    current_time_ms: int,
    previous_time_ms: int | None = None,
    already_fired: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose the minimal morning evidence bundle without Engine/strategy."""

    trade_date = _strict_date(trade_date)
    symbol = _strict_symbol(symbol)
    if projection.trade_date != trade_date:
        raise ValueError("Q2 projection trade_date does not match request")
    plan = build_a_share_session_plan(trade_date, calendar)
    timer_evidence = build_timer_evidence(
        plan,
        current_time_ms=current_time_ms,
        previous_time_ms=previous_time_ms,
        already_fired=already_fired,
    )

    tagged: dict[str, Mapping[str, Any]] = {}
    for row in auction_rows:
        if isinstance(row, Mapping):
            item = dict(row)
        else:
            names = (
                "ts", "px_milli", "chg_bp", "match_amt_yuan",
                "rest_bid_amt_yuan", "rest_ask_amt_yuan", "limit_state",
                "symbol", "trade_date", "auction_tag",
            )
            if len(row) != len(names):
                raise ValueError("auction row has an unexpected column count")
            item = dict(zip(names, row))
        tag = str(item.get("auction_tag") or "").strip()
        if tag in {"0920", "0924", "0925"}:
            tagged[tag] = item

    auction_result: dict[str, Any]
    if set(tagged) == {"0920", "0924", "0925"}:
        auction_result = build_shadow_from_rows(
            tuple(tagged.values()),
            trade_date=trade_date,
            symbol=symbol,
        )
    else:
        auction_result = {
            "status": "UNAVAILABLE",
            "reason": "missing_auction_anchors",
            "available_tags": tuple(sorted(tagged)),
            "required_tags": ("0920", "0924", "0925"),
            "semantic_hash": semantic_hash({"status": "UNAVAILABLE", "available_tags": tuple(sorted(tagged))}),
        }

    opening_row = tagged.get("0925", {})
    opening_fact = _opening_fact_from_q2(
        projection,
        symbol=symbol,
        auction_row=opening_row,
    ) if opening_row else {
        "status": "unavailable",
        "symbol": symbol,
        "reason": "auction_0925_missing",
    }

    reference_data = {
        "previous_day_stats": {
            "status": "UNAVAILABLE",
            "reason": "historical_available_at_unknown",
        },
        "hot_plates": {
            "status": "UNAVAILABLE",
            "reason": "metadata_contract_not_verified",
        },
    }
    # Raw TD rows remain evidence only.  In particular, taos may return a
    # naive datetime object; including it in a semantic hash would make the
    # result platform-dependent and would confuse source evidence with facts.
    auction_semantic = {
        "status": auction_result.get("status"),
        "shadow_content_hash": (
            auction_result.get("shadow", {}).get("content_hash")
            if isinstance(auction_result.get("shadow"), Mapping)
            else None
        ),
        "available_tags": auction_result.get("available_tags", ()),
    }
    semantic_payload = {
        "trade_date": trade_date,
        "symbol": symbol,
        "projection_hash": projection.content_hash,
        "projection_status": projection.status,
        "auction": auction_semantic,
        "opening": opening_fact,
        "timers": timer_evidence,
    }
    return {
        "contract_version": "MorningVerticalSliceShadowV1",
        "trade_date": trade_date,
        "symbol": symbol,
        "session_plan_hash": plan.content_hash,
        "q2": {
            "status": projection.status,
            "coverage": projection.coverage,
            "consistency_status": projection.consistency_status,
            "expected_symbol_count": len(projection.expected_symbols),
            "quote_count": len(projection.quotes),
            "missing_symbols": projection.missing_symbols,
            "stale_symbols": projection.stale_symbols,
            "oldest_source_time_ms": projection.oldest_source_time_ms,
            "newest_source_time_ms": projection.newest_source_time_ms,
            "content_hash": projection.content_hash,
        },
        "timers": timer_evidence,
        "auction": auction_result,
        "opening_transition": opening_fact,
        "reference_data": reference_data,
        "semantic_hash": semantic_hash(semantic_payload),
        "read_only": True,
        "side_effect_boundary": "Redis SMEMBERS/HGETALL and TD SELECT only; no writer, Rabbit ACK, recovery, notification, strategy or effect",
    }


def _redis_projection(
    *,
    trade_date: str,
    observed_at: datetime,
    stale_after_ms: int,
) -> Any:
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
        return RedisQ2ProjectionAdapter(client).read(
            trade_date,
            observed_at,
            freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
        )
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol", default="600519")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calendar-file", type=Path, required=True)
    parser.add_argument("--observed-at", help="aware ISO time or HH:MM:SS; defaults to now")
    parser.add_argument("--current-time", help="timer evaluation time; defaults to observed-at")
    parser.add_argument("--previous-time", help="normal timer frontier; omit for cold-start evidence")
    parser.add_argument("--already-fired", default="")
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--q2-file", type=Path, help="optional captured JSONL; otherwise read Redis")
    parser.add_argument("--td-host", default=os.environ.get("TDENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--td-port", type=int, default=int(os.environ.get("TDENGINE_PORT", "6030")))
    parser.add_argument("--td-user", default=os.environ.get("TDENGINE_USER", "root"))
    parser.add_argument("--td-password", default=os.environ.get("TDENGINE_PASSWORD", "taosdata"))
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    args = parser.parse_args()
    trade_date = _strict_date(args.trade_date)
    symbol = _strict_symbol(args.symbol)
    if args.stale_after_ms < 0:
        parser.error("stale-after-ms must be nonnegative")
    observed_at = _parse_datetime(args.observed_at, default_date=trade_date) if args.observed_at else datetime.now(timezone.utc)
    current_at = _parse_datetime(args.current_time, default_date=trade_date) if args.current_time else observed_at
    previous_at = _parse_datetime(args.previous_time, default_date=trade_date) if args.previous_time else None
    calendar = _load_calendar(args.calendar_file, trade_date=trade_date)
    projection = _redis_projection(
        trade_date=trade_date,
        observed_at=observed_at,
        stale_after_ms=args.stale_after_ms,
    ) if args.q2_file is None else _projection_from_jsonl(
        args.q2_file,
        trade_date=trade_date,
        observed_at=observed_at,
        stale_after_ms=args.stale_after_ms,
    )
    rows = query_rows(
        trade_date=trade_date,
        symbol=symbol,
        host=args.td_host,
        port=args.td_port,
        user=args.td_user,
        password=args.td_password,
        database=args.td_database,
    )
    result = build_morning_shadow(
        trade_date=trade_date,
        symbol=symbol,
        projection=projection,
        auction_rows=rows,
        calendar=calendar,
        current_time_ms=_epoch_ms(current_at),
        previous_time_ms=_epoch_ms(previous_at) if previous_at else None,
        already_fired=tuple(item for item in args.already_fired.split(",") if item),
    )
    result["observed_at"] = observed_at.isoformat()
    result["current_time"] = current_at.isoformat()
    result["calendar_semantic_hash"] = calendar.semantic_hash
    result["calendar_evidence_hash"] = calendar.evidence_hash
    result["td_row_count"] = len(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n")
    # Terminal rendering is evidence output and may contain provider-native
    # datetime values from taos.  The semantic hash above deliberately
    # excludes those raw rows; rendering must not make a successful read-only
    # run fail merely because the driver returned a naive datetime object.
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


def _projection_from_jsonl(
    path: Path,
    *,
    trade_date: str,
    observed_at: datetime,
    stale_after_ms: int,
) -> Any:
    """Build a projection from a captured one-row-per-symbol JSONL file."""

    raw_hashes: dict[str, Mapping[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, Mapping):
            raise ValueError("Q2 JSONL row must be an object")
        symbol = str(item.get("symbol") or "")
        if symbol:
            raw_hashes[symbol] = item
    return _build_projection_from_raw(
        trade_date=trade_date,
        observed_at=observed_at,
        raw_hashes=raw_hashes,
        stale_after_ms=stale_after_ms,
    )


def _build_projection_from_raw(
    *,
    trade_date: str,
    observed_at: datetime,
    raw_hashes: Mapping[str, Mapping[str, Any]],
    stale_after_ms: int,
) -> Any:
    from engine_core.q2 import build_q2_projection

    expected = tuple(sorted(raw_hashes))
    return build_q2_projection(
        trade_date,
        observed_at,
        expected,
        raw_hashes,
        freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
        source_id="q2frame_capture",
    )


if __name__ == "__main__":
    raise SystemExit(main())
