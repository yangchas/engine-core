"""Run the first migrated auction fact slice against real TD rows.

This is a bounded, read-only composition tool.  It uses the TDengine client
already present in the runtime environment and only reads
``auction_snapshot_v2``.  The calculation itself is delegated to the
engine_core fact wheels; no legacy strategy, Redis writer, Rabbit consumer,
repair path or notification component is imported.

The input table is an auction *projection*, not a raw tick stream.  The
result therefore proves the P/M/RB/RA fact path for the selected anchors, not
the producer batch membership or an arrival-order replay.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from engine_core import (
    EngineSnapshot,
    build_auction_fact_shadow,
    build_segment_frame,
    canonical_json,
    semantic_hash,
)


TD_FIELDS = (
    "ts",
    "px_milli",
    "chg_bp",
    "match_amt_yuan",
    "rest_bid_amt_yuan",
    "rest_ask_amt_yuan",
    "limit_state",
    "symbol",
    "trade_date",
    "auction_tag",
)
ANCHOR_CLOCKS = {"0920": "09:20:00", "0924": "09:24:00", "0925": "09:25:00"}
ANCHOR_ORDER = ("0920", "0924", "0925")
AUCTION_REQUIRED_FIELDS = (
    "price_milli",
    "auction_amount_yuan",
    "auction_bid_amount_yuan",
    "auction_ask_amount_yuan",
)


def _strict_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade-date must be strict YYYY-MM-DD")
    return value


def _strict_symbol(value: str) -> str:
    if len(value) != 6 or not value.isdigit():
        raise ValueError("symbol must be a six-digit code")
    return value


def _strict_identifier(value: str, field: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"{field} must be a simple SQL identifier")
    return value


def _epoch_ms(value: datetime, timezone_name: str) -> int:
    if value.tzinfo is not None and value.utcoffset() is not None:
        aware = value
    else:
        aware = value.replace(tzinfo=ZoneInfo(timezone_name))
    return int(aware.astimezone(timezone.utc).timestamp() * 1000)


def _tagged_rows(rows: Iterable[Sequence[Any] | Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalize taos rows without changing source values or filling nulls."""

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping):
            item = {name: row.get(name) for name in TD_FIELDS}
        else:
            if len(row) != len(TD_FIELDS):
                raise ValueError("TD row has an unexpected column count")
            item = dict(zip(TD_FIELDS, row))
        tag = str(item.get("auction_tag") or "").strip()
        if tag in ANCHOR_ORDER:
            if tag in result:
                raise ValueError("duplicate auction anchor row for tag %s" % tag)
            result[tag] = item
    return result


def _state_quality(state: Mapping[str, Any]) -> tuple[float, str]:
    """Derive snapshot quality from the fields actually used by the facts.

    A projection row can exist while one or more semantic auction fields are
    absent.  Do not label that row READY merely because the TD row itself was
    present; coverage and completeness describe the required business state.
    """

    present = sum(state.get(field) is not None for field in AUCTION_REQUIRED_FIELDS)
    if present == len(AUCTION_REQUIRED_FIELDS):
        return 1.0, "READY"
    if present == 0:
        return 0.0, "MISSING"
    return present / float(len(AUCTION_REQUIRED_FIELDS)), "PARTIAL"


def _segment_quality(previous: EngineSnapshot, current: EngineSnapshot) -> str:
    statuses = {previous.completeness, current.completeness}
    if statuses == {"READY"}:
        return "READY"
    if statuses == {"MISSING"}:
        return "MISSING"
    return "PARTIAL"


def build_shadow_from_rows(
    rows: Iterable[Sequence[Any] | Mapping[str, Any]],
    *,
    trade_date: str,
    symbol: str,
    timezone_name: str = "Asia/Shanghai",
    source_table: str = "market_data1.auction_snapshot_v2",
    source_semantics: str = "TD projection rows; no Rabbit arrival or batch ordering",
    evidence_ref_prefix: str = "td://market_data1/auction_snapshot_v2",
) -> dict[str, Any]:
    """Build adjacent 0920→0924 and 0924→0925 facts from TD projection rows.

    This pure helper is intentionally independent of taos.  Rows must contain
    the exact ``auction_snapshot_v2`` columns listed in :data:`TD_FIELDS`.
    ``source_record_time_ms`` preserves the actual TD timestamp while each
    segment retains its business anchor interval.
    """

    trade_date = _strict_date(trade_date)
    symbol = _strict_symbol(symbol)
    tagged = _tagged_rows(rows)
    missing = [tag for tag in ANCHOR_ORDER if tag not in tagged]
    if missing:
        raise ValueError("missing auction anchors: " + ",".join(missing))

    snapshots: dict[str, EngineSnapshot] = {}
    for tag in ANCHOR_ORDER:
        row = tagged[tag]
        row_symbol = str(row.get("symbol") or "").strip()
        if row_symbol != symbol:
            raise ValueError("TD row symbol does not match requested symbol")
        row_date = str(row.get("trade_date") or "").strip().replace("-", "")
        if row_date != trade_date.replace("-", ""):
            raise ValueError("TD row trade_date does not match requested date")
        source_time = row.get("ts")
        if not isinstance(source_time, datetime):
            raise ValueError("TD row ts must be datetime")
        source_record_time_ms = _epoch_ms(source_time, timezone_name)
        business_anchor_time_ms = _epoch_ms(
            datetime.combine(date.fromisoformat(trade_date), datetime.strptime(ANCHOR_CLOCKS[tag], "%H:%M:%S").time()),
            timezone_name,
        )
        state = {
            "price_milli": row.get("px_milli"),
            "auction_amount_yuan": row.get("match_amt_yuan"),
            "auction_bid_amount_yuan": row.get("rest_bid_amt_yuan"),
            "auction_ask_amount_yuan": row.get("rest_ask_amt_yuan"),
        }
        coverage, completeness = _state_quality(state)
        content = {
            "snapshot_id": f"{symbol}:{trade_date}:AUCTION_{tag}",
            "trigger_id": f"AUCTION_{tag}",
            "logical_time_ms": business_anchor_time_ms,
            "symbol": symbol,
            "state": state,
        }
        snapshots[tag] = EngineSnapshot(
            snapshot_id=content["snapshot_id"],
            trigger_id=content["trigger_id"],
            logical_time_ms=business_anchor_time_ms,
            session_id=trade_date,
            phase="AUCTION",
            market_state_revision=1,
            source_observation_metadata={
                "source_table": source_table,
                "business_anchor": content["trigger_id"],
                "business_anchor_time_ms": business_anchor_time_ms,
                "source_record_time_ms": source_record_time_ms,
                "source_time_timezone": timezone_name,
            },
            symbol_states={symbol: state},
            raw_market_cross_section={},
            raw_theme_cross_section={},
            windows={},
            coverage=coverage,
            completeness=completeness,
            content_hash=semantic_hash(content),
            evidence_refs=(
                f"{evidence_ref_prefix}/{trade_date}/{symbol}/{tag}",
            ),
        )

    segments = (
        build_segment_frame(
            f"auction_{trade_date}_{symbol}_0920_to_0924",
            snapshots["0920"],
            snapshots["0924"],
            scope_type="SYMBOL",
            scope_id=symbol,
            amount_semantics="OBSERVED_STATE",
            volume_semantics="UNKNOWN",
            coverage_status=_segment_quality(snapshots["0920"], snapshots["0924"]),
            observed_start_time_ms=snapshots["0920"].source_observation_metadata["source_record_time_ms"],
            observed_end_time_ms=snapshots["0924"].source_observation_metadata["source_record_time_ms"],
        ),
        build_segment_frame(
            f"auction_{trade_date}_{symbol}_0924_to_0925",
            snapshots["0924"],
            snapshots["0925"],
            scope_type="SYMBOL",
            scope_id=symbol,
            amount_semantics="OBSERVED_STATE",
            volume_semantics="UNKNOWN",
            coverage_status=_segment_quality(snapshots["0924"], snapshots["0925"]),
            observed_start_time_ms=snapshots["0924"].source_observation_metadata["source_record_time_ms"],
            observed_end_time_ms=snapshots["0925"].source_observation_metadata["source_record_time_ms"],
        ),
    )
    shadow = build_auction_fact_shadow(*segments)
    return {
        "trade_date": trade_date,
        "symbol": symbol,
        "source_table": source_table,
        "source_semantics": source_semantics,
        "anchors": {
            tag: {
                "business_anchor": f"AUCTION_{tag}",
                "business_anchor_time_ms": snapshots[tag].logical_time_ms,
                "source_record_time_ms": snapshots[tag].source_observation_metadata["source_record_time_ms"],
                "raw": tagged[tag],
            }
            for tag in ANCHOR_ORDER
        },
        "segments": [
            {
                "segment_id": item.segment_id,
                "business_start_ms": item.start_time_ms,
                "business_end_exclusive_ms": item.end_time_ms,
                "observed_start_ms": item.observed_start_time_ms,
                "observed_end_ms": item.observed_end_time_ms,
                "coverage_status": item.coverage_status,
                "content_hash": item.content_hash,
                "evidence_hash": item.evidence_hash,
            }
            for item in segments
        ],
        "shadow": json.loads(canonical_json(shadow.as_trace())),
    }


def query_rows(
    *,
    trade_date: str,
    symbol: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> list[tuple[Any, ...]]:
    """Read three bounded rows using the existing TD client configuration."""

    import taos  # type: ignore[import-not-found]

    trade_date = _strict_date(trade_date)
    symbol = _strict_symbol(symbol)
    database = _strict_identifier(database, "database")
    # Inputs are validated before interpolation; no arbitrary SQL is accepted.
    compact_date = trade_date.replace("-", "")
    sql = (
        "SELECT ts, px_milli, chg_bp, match_amt_yuan, rest_bid_amt_yuan, "
        "rest_ask_amt_yuan, limit_state, symbol, trade_date, auction_tag "
        f"FROM {database}.auction_snapshot_v2 "
        f"WHERE trade_date=\"{compact_date}\" AND symbol=\"{symbol}\" "
        'AND auction_tag IN ("0920","0924","0925") ORDER BY ts'
    )
    connection = taos.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
    )
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return list(cursor.fetchall())
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--td-host", default=os.environ.get("TDENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--td-port", type=int, default=int(os.environ.get("TDENGINE_PORT", "6030")))
    parser.add_argument("--td-user", default=os.environ.get("TDENGINE_USER", "root"))
    parser.add_argument("--td-password", default=os.environ.get("TDENGINE_PASSWORD", "taosdata"))
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    args = parser.parse_args()
    trade_date = _strict_date(args.trade_date)
    symbol = _strict_symbol(args.symbol)
    result = build_shadow_from_rows(
        query_rows(
            trade_date=trade_date,
            symbol=symbol,
            host=args.td_host,
            port=args.td_port,
            user=args.td_user,
            password=args.td_password,
            database=args.td_database,
        ),
        trade_date=trade_date,
        symbol=symbol,
    )
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    result["read_only"] = True
    result["side_effect_boundary"] = "TD SELECT only; no Redis/TD write, Rabbit, repair, notification or strategy effect"
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
