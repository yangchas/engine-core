"""Read-only real auction-reference preparation and startup readiness probe.

The command reuses the verified Redis cache layouts and TD daily-kline query
shape.  It never repairs a cache, calls a network fallback, writes storage,
consumes RabbitMQ, or dispatches an effect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    AUCTION_REFERENCE_FUNCTION_ORDER,
    canonical_daily_kline_cache_payload_hash,
    DataContext,
    FreshnessPolicy,
    HotPlatesFunction,
    PreviousDayLimitPoolFunction,
    PreviousDayStatsFunction,
    RedisHotPlatesProvider,
    RedisPreviousDayLimitPoolProvider,
    RedisPreviousDayStatsProvider,
    RedisQ2ProjectionAdapter,
    TDPreviousDayStatsProvider,
    TradingCalendarSnapshot,
    assess_startup_readiness,
    build_a_share_session_plan,
    prepare_auction_references,
)
from examples.run_real_hot_plates import _read_redis_rows as _read_hot_plates  # noqa: E402
from examples.run_real_previous_day_limit_pool import (  # noqa: E402
    _read_redis_rows as _read_limit_pool,
)
from examples.run_real_previous_day_stats import _fetch_td_rows  # noqa: E402
from examples.run_startup_readiness_probe import _load_calendar  # noqa: E402


def run_real_auction_reference_readiness(
    *,
    client: Any,
    trade_date: str,
    calendar: TradingCalendarSnapshot,
    observed_at: datetime,
    symbols: tuple[str, ...],
    stale_after_ms: int,
    td_kwargs: dict[str, Any],
    fetch_td_rows_override: Callable[[str, tuple[str, ...]], list[dict[str, Any]]] | None = None,
    _return_context: bool = False,
) -> dict[str, Any]:
    """Read and assess the current real reference sources exactly once.

    The normal return value remains the JSON-safe audit payload.  The private
    ``_return_context`` path is for a same-process live shadow coordinator: it
    exposes the already-prepared immutable objects so the caller can bind the
    exact results to an Engine-owned evaluation without issuing a second
    provider read.  It is intentionally private and does not alter the CLI
    contract.
    """

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if not symbols:
        raise ValueError("a bounded symbol set is required for the TD read probe")
    observed_at_ms = int(observed_at.timestamp() * 1000)
    previous_trade_date = calendar.previous_trade_day(trade_date).isoformat()
    limit_rows, limit_summary = _read_limit_pool(client, previous_trade_date)
    hot_rows, hot_summary = _read_hot_plates(client, trade_date)

    def td_rows(previous: str, requested: tuple[str, ...]):
        if fetch_td_rows_override is not None:
            return fetch_td_rows_override(previous, requested)
        return _fetch_td_rows(previous, requested, **td_kwargs)

    def redis_kline_rows(
        previous: str,
        requested: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        key = "cache:kline_ready:" + previous
        values = client.hmget(key, requested)
        rows = []
        for symbol, raw in zip(requested, values):
            if raw is None:
                continue
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError("Redis daily-kline row must be an object")
            row = dict(row)
            row.setdefault("symbol", symbol)
            if row.get("symbol") != symbol:
                raise ValueError("Redis daily-kline symbol does not match hash field")
            if row.get("trade_date") != previous:
                raise ValueError("Redis daily-kline trade_date does not match key")
            rows.append(row)
        return rows

    def redis_kline_available_at(previous: str) -> int | None:
        """Return sidecar availability only when it matches the full cache."""
        try:
            metadata_raw = client.get("cache:kline_ready_meta:" + previous)
            if metadata_raw is None:
                return None
            metadata = json.loads(metadata_raw)
            if not isinstance(metadata, dict):
                return None
            available = metadata.get("available_at_ms")
            if (
                metadata.get("schema_version") != "DailyKlineRuntimeCacheV1"
                or metadata.get("trade_date") != previous
                or metadata.get("source") != "baostock"
                or metadata.get("success") is not True
                or isinstance(available, bool)
                or not isinstance(available, int)
                or available <= 0
            ):
                return None
            raw_payload = client.hgetall("cache:kline_ready:" + previous) or {}
            if not isinstance(raw_payload, dict):
                return None
            rows = []
            for field, raw_value in raw_payload.items():
                value = json.loads(raw_value) if isinstance(raw_value, (str, bytes)) else raw_value
                if not isinstance(value, dict) or str(value.get("symbol") or "") != str(field):
                    return None
                if value.get("trade_date") != previous:
                    return None
                rows.append(dict(value))
            if not rows or canonical_daily_kline_cache_payload_hash(rows) != metadata.get("payload_sha256"):
                return None
            return available
        except (TypeError, ValueError, OverflowError):
            return None

    # This is an explicit audit-source choice, not a semantic fallback engine:
    # TD remains first.  Only a successful empty TD read selects the existing
    # Redis runtime view.  Access errors remain TD errors and are not hidden.
    try:
        td_candidate_rows = tuple(td_rows(previous_trade_date, symbols))
        td_error: Exception | None = None
    except Exception as exc:  # preserved by the selected TD provider below
        td_candidate_rows = ()
        td_error = exc

    if td_error is not None:
        def selected_td_rows(previous: str, requested: tuple[str, ...]):
            raise td_error

        previous_stats_provider = TDPreviousDayStatsProvider(
            selected_td_rows,
            observed_at_ms=lambda: observed_at_ms,
            available_at_ms=None,
            source_id="tdengine_daily_kline",
            source_schema="daily_kline",
            evidence_ref="td://market_data1/daily_kline/" + previous_trade_date,
        )
        previous_day_stats_source_selection = "td_error_no_redis_selection"
    elif td_candidate_rows:
        previous_stats_provider = TDPreviousDayStatsProvider(
            lambda previous, requested: td_candidate_rows,
            observed_at_ms=lambda: observed_at_ms,
            available_at_ms=None,
            source_id="tdengine_daily_kline",
            source_schema="daily_kline",
            evidence_ref="td://market_data1/daily_kline/" + previous_trade_date,
        )
        previous_day_stats_source_selection = "td_daily_kline"
    else:
        redis_available_at_ms = redis_kline_available_at(previous_trade_date)
        previous_stats_provider = RedisPreviousDayStatsProvider(
            redis_kline_rows,
            observed_at_ms=lambda: observed_at_ms,
            available_at_ms=lambda: redis_available_at_ms,
            source_id="redis_daily_kline_cache",
            source_schema="cache:kline_ready",
            evidence_ref="redis://cache:kline_ready/" + previous_trade_date,
        )
        previous_day_stats_source_selection = "redis_kline_ready_after_td_empty"

    previous_stats = PreviousDayStatsFunction(
        previous_stats_provider,
        calendar,
    )
    limit_pool = PreviousDayLimitPoolFunction(
        RedisPreviousDayLimitPoolProvider(
            lambda date: limit_rows,
            observed_at_ms=lambda: observed_at_ms,
            metadata=lambda date: limit_summary.get("meta"),
            evidence_ref="redis://cache:yest_limit_pool/" + previous_trade_date,
        ),
        calendar,
    )
    hot_plates = HotPlatesFunction(
        RedisHotPlatesProvider(
            lambda date: hot_rows,
            observed_at_ms=lambda: observed_at_ms,
            metadata=lambda date: hot_summary.get("meta"),
            evidence_ref="redis://cache:hot_plates/" + trade_date,
        ),
        calendar,
    )
    prepared = prepare_auction_references(
        trade_date=trade_date,
        knowledge_as_of_ms=observed_at_ms,
        context=DataContext(
            "real-auction-reference:" + trade_date,
            "LIVE_SHADOW",
            observed_at_ms,
        ),
        calendar=calendar,
        previous_day_stats=previous_stats,
        previous_day_limit_pool=limit_pool,
        hot_plates=hot_plates,
        symbols=tuple(sorted(set(symbols))),
    )
    q2 = RedisQ2ProjectionAdapter(client).read(
        trade_date,
        observed_at,
        freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
    )
    plan = build_a_share_session_plan(trade_date, calendar)
    readiness = assess_startup_readiness(
        trade_date,
        observed_at_ms,
        calendar,
        plan,
        q2=q2,
        required_reference_functions=AUCTION_REFERENCE_FUNCTION_ORDER,
        reference_results=prepared.as_mapping(),
        previous_time_ms=None,
        origin="RECOVERY_CATCHUP",
    )
    payload = {
        "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "observed_at": observed_at.isoformat(),
        "calendar_semantic_hash": calendar.semantic_hash,
        "session_plan_hash": plan.content_hash,
        "reference_preparation_hash": prepared.content_hash,
        "reference_results": {
            function_id: {
                "status": result.status.value,
                "actual_source": result.actual_source,
                "actual_trade_date": result.actual_trade_date,
                "available_at_ms": result.available_at_ms,
                "observed_at_ms": result.observed_at_ms,
                "completeness": result.completeness,
                "missing_fields": result.missing_fields,
                "missing_symbols": result.missing_symbols,
                "content_hash": result.content_hash,
            }
            for function_id, result in prepared.results
        },
        "q2": {
            "status": q2.status.value,
            "coverage": q2.coverage,
            "expected_count": len(q2.expected_symbols),
            "record_count": len(q2.quotes),
            "stale_count": len(q2.stale_symbols),
            "oldest_source_time_ms": q2.oldest_source_time_ms,
            "newest_source_time_ms": q2.newest_source_time_ms,
            "content_hash": q2.content_hash,
        },
        "readiness": {
            "status": readiness.status,
            "phase": readiness.phase,
            "reference_statuses": readiness.reference_statuses,
            "actions": readiness.actions,
            "reasons": readiness.reasons,
            "content_hash": readiness.content_hash,
        },
        "redis": {
            "previous_day_limit_pool": limit_summary,
            "hot_plates": hot_summary,
        },
        "previous_day_stats_source_selection": previous_day_stats_source_selection,
        "read_only": True,
        "side_effect_boundary": (
            "Redis SMEMBERS/HMGET/HGETALL/TYPE/HLEN/HSCAN/GET and bounded TD SELECT only; "
            "no Redis/TD write, Rabbit consume/ACK, repair, network fallback, or effect"
        ),
    }
    if _return_context:
        return {
            "audit": payload,
            "preparation": prepared,
            "q2": q2,
            "readiness_object": readiness,
        }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
    observed_at = datetime.now(timezone.utc)
    try:
        payload = run_real_auction_reference_readiness(
            client=client,
            trade_date=args.trade_date,
            calendar=_load_calendar(args.calendar),
            observed_at=observed_at,
            symbols=tuple(item.strip() for item in args.symbols.split(",") if item.strip()),
            stale_after_ms=args.stale_after_ms,
            td_kwargs={
                "host": os.environ.get("TDENGINE_HOST", "127.0.0.1"),
                "port": int(os.environ.get("TDENGINE_PORT", "6030")),
                "user": os.environ.get("TDENGINE_USER", "root"),
                "password": os.environ.get("TDENGINE_PASSWORD", "taosdata"),
                "database": os.environ.get("TDENGINE_DATABASE", "market_data1"),
                "config": os.environ.get("TDENGINE_CONFIG", "/etc/taos"),
                "timezone_name": os.environ.get("TDENGINE_TIMEZONE", "Asia/Shanghai"),
            },
        )
    finally:
        client.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="") as output:
        json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        output.write("\n")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
