"""Run the previous-limit feedback fact against bounded real Redis/TD reads.

This command is an acceptance probe, not a production service.  It reuses the
existing read-only Redis previous-limit runner and the existing TD
``auction_snapshot_v2`` query helper.  It never repairs a cache, writes Redis
or TDengine, consumes RabbitMQ, sends notifications, or applies a strategy.

The current anchor adapter deliberately exposes only explicit ``change_pct``
percentage points and ``source_record_time_ms``.  If the previous pool has no
verified historical availability, the fact remains ``UNAVAILABLE`` even when
the Redis rows are physically present.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    DataResult,
    DataStatus,
    PreviousDayLimitFeedbackFact,
    build_previous_day_limit_feedback,
    canonical_json,
)
from engine_core.contracts import Provenance  # noqa: E402

try:
    from run_real_auction_shadow import query_rows  # noqa: E402
    from run_real_previous_day_limit_pool import run_real_previous_day_limit_pool  # noqa: E402
except ModuleNotFoundError:
    from examples.run_real_auction_shadow import query_rows  # noqa: E402
    from examples.run_real_previous_day_limit_pool import run_real_previous_day_limit_pool  # noqa: E402


def _strict_date(value: str, *, field: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be strict YYYY-MM-DD")
    return value


def _strict_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
    if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
        raise ValueError("symbols must be a comma-separated list of six-digit codes")
    return symbols


def _epoch_ms(value: Any, *, timezone_name: str) -> int | None:
    if not isinstance(value, datetime):
        return None
    aware = value if value.tzinfo is not None and value.utcoffset() is not None else value.replace(
        tzinfo=ZoneInfo(timezone_name)
    )
    return int(aware.astimezone(timezone.utc).timestamp() * 1000)


def _rebuild_previous_result(raw: Mapping[str, Any]) -> DataResult:
    status_text = str(raw.get("result_status") or "")
    # ``json.dumps(default=str)`` may serialize a Python Enum as
    # ``DataStatus.UNAVAILABLE`` in older runner artifacts; accept that
    # representation only at this adapter boundary.
    if "." in status_text:
        status_text = status_text.rsplit(".", 1)[-1]
    provenance = tuple(
        Provenance(
            source_id=str(item.get("source_id") or "unknown"),
            source_kind=str(item.get("source_kind") or "unknown"),
            source_schema=str(item.get("source_schema") or "unknown"),
            source_trade_date=item.get("source_trade_date"),
            effective_at_ms=item.get("effective_at_ms"),
            observed_at_ms=item.get("observed_at_ms"),
            evidence_ref=item.get("evidence_ref"),
            notes=tuple(item.get("notes") or ()),
        )
        for item in raw.get("provenance") or ()
        if isinstance(item, Mapping)
    )
    return DataResult(
        request_id="real-previous-day-limit-feedback-rebuilt",
        function_id="previous_day_limit_pool",
        status=DataStatus(status_text),
        data=raw.get("data"),
        actual_source=raw.get("actual_source"),
        requested_trade_date=str(raw.get("requested_trade_date")),
        actual_trade_date=raw.get("actual_trade_date"),
        effective_at_ms=raw.get("effective_at_ms"),
        available_at_ms=raw.get("available_at_ms"),
        observed_at_ms=int(raw["observed_at_ms"]),
        schema_version=1,
        completeness=float(raw.get("completeness") or 0.0),
        missing_fields=tuple(raw.get("missing_fields") or ()),
        missing_symbols=tuple(raw.get("missing_symbols") or ()),
        provenance=provenance,
        temporal_mode=str(raw.get("temporal_mode") or "HISTORICAL"),
        fetch_completed_at_ms=raw.get("fetch_completed_at_ms", raw.get("observed_at_ms")),
    )


def _load_current_0925_rows(
    *,
    trade_date: str,
    symbols: tuple[str, ...],
    td_kwargs: Mapping[str, Any],
    timezone_name: str,
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    compact_date = trade_date.replace("-", "")
    for symbol in symbols:
        try:
            raw_rows = query_rows(trade_date=trade_date, symbol=symbol, **dict(td_kwargs))
        except Exception:
            # A bounded probe records unavailable symbols through the normal
            # fact join; it does not retry or widen the TD query.
            continue
        for raw_row in raw_rows:
            if isinstance(raw_row, Mapping):
                row = dict(raw_row)
            else:
                names = (
                    "ts", "px_milli", "chg_bp", "match_amt_yuan",
                    "rest_bid_amt_yuan", "rest_ask_amt_yuan", "limit_state",
                    "symbol", "trade_date", "auction_tag",
                )
                if len(raw_row) != len(names):
                    continue
                row = dict(zip(names, raw_row))
            if str(row.get("auction_tag") or row.get("tag") or "") != "0925":
                continue
            source_time_ms = _epoch_ms(row.get("ts") or row.get("timestamp"), timezone_name=timezone_name)
            change_bp = row.get("chg_bp")
            change_pct = None
            if isinstance(change_bp, (int, float)) and not isinstance(change_bp, bool):
                change_pct = float(change_bp) / 100.0
            rows.append(
                {
                    "trade_date": compact_date,
                    "tag": "0925",
                    "symbol": str(row.get("symbol") or symbol),
                    "change_pct": change_pct,
                    "auction_amount_yuan": row.get("match_amt_yuan", row.get("auction_amount_yuan")),
                    "source_record_time_ms": source_time_ms,
                }
            )
    # The fact contract expects dashed business dates; keep this correction at
    # the adapter boundary rather than teaching the pure fact about TD dialect.
    return tuple({**row, "trade_date": trade_date} for row in rows)


def run_real_feedback(
    *,
    trade_date: str,
    previous_trade_date: str,
    symbols: tuple[str, ...],
    redis_kwargs: Mapping[str, Any],
    td_kwargs: Mapping[str, Any],
    timezone_name: str = "Asia/Shanghai",
    temporal_mode: str = "HISTORICAL",
) -> Mapping[str, Any]:
    trade_date = _strict_date(trade_date, field="trade_date")
    previous_trade_date = _strict_date(previous_trade_date, field="previous_trade_date")
    if previous_trade_date >= trade_date:
        raise ValueError("previous_trade_date must be earlier than trade_date")
    observed_at = datetime.now(timezone.utc)
    pool_raw = run_real_previous_day_limit_pool(
        trade_date=trade_date,
        previous_trade_date=previous_trade_date,
        observed_at=observed_at,
        redis_kwargs=dict(redis_kwargs),
        temporal_mode=temporal_mode,
    )
    previous_result = _rebuild_previous_result(pool_raw)
    current_rows = _load_current_0925_rows(
        trade_date=trade_date,
        symbols=symbols,
        td_kwargs=td_kwargs,
        timezone_name=timezone_name,
    )
    fact: PreviousDayLimitFeedbackFact = build_previous_day_limit_feedback(
        previous_result,
        current_rows,
        current_trade_date=trade_date,
    )
    return {
        "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "symbols": symbols,
        "previous_result_status": previous_result.status,
        "temporal_mode": previous_result.temporal_mode,
        "current_0925_row_count": len(current_rows),
        "fact": json.loads(canonical_json(fact.as_mapping())),
        "read_only": True,
        "side_effect_boundary": "Redis TYPE/HLEN/HSCAN/GET and TD SELECT only; no write/repair/Rabbit/effect",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-trade-date", required=True)
    parser.add_argument("--symbols", default="600519,000001,000002")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.environ.get("REDIS_DB", "0")))
    parser.add_argument("--td-host", default=os.environ.get("TDENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--td-port", type=int, default=int(os.environ.get("TDENGINE_PORT", "6030")))
    parser.add_argument("--td-user", default=os.environ.get("TDENGINE_USER", "root"))
    parser.add_argument("--td-password", default=os.environ.get("TDENGINE_PASSWORD", "taosdata"))
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    parser.add_argument(
        "--temporal-mode",
        choices=("HISTORICAL", "REPLAY", "LIVE"),
        default="HISTORICAL",
        help="Historical/replay require verified availability; LIVE is for an actual pre-node prefetch.",
    )
    args = parser.parse_args()
    result = run_real_feedback(
        trade_date=args.trade_date,
        previous_trade_date=args.previous_trade_date,
        symbols=_strict_symbols(args.symbols),
        redis_kwargs={
            "host": args.redis_host,
            "port": args.redis_port,
            "db": args.redis_db,
            "password": os.environ.get("REDIS_PASSWORD"),
        },
        td_kwargs={
            "host": args.td_host,
            "port": args.td_port,
            "user": args.td_user,
            "password": args.td_password,
            "database": args.td_database,
        },
        temporal_mode=args.temporal_mode,
    )
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
