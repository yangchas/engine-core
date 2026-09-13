"""Compare bounded Redis auction projections with TD auction rows.

This is an isolated, read-only migration audit tool.  It deliberately compares
only fields whose authority is documented by the existing t1-v2 writers:
Redis ``market:auction:anchor:{date}`` amount/bid/ask projections and TD
``auction_snapshot_v2`` match/resting amounts.  Missing fields are reported as
``NOT_COMPARABLE`` rather than inferred or zero-filled.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import semantic_hash


TAGS = ("0920", "0924", "0925")
ANCHOR_TAG = "0925"
SYMBOLS_DEFAULT = ("600519", "000001", "000002")


def _strict_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade-date must be strict YYYY-MM-DD")
    return value


def _strict_symbol(value: str) -> str:
    if len(value) != 6 or not value.isdigit():
        raise ValueError("symbol must be a six-digit code")
    return value


def _compact_date(value: str) -> str:
    return _strict_date(value).replace("-", "")


def _epoch_ms(value: Any) -> int | None:
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None and value.utcoffset() is not None else value.replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
        return int(aware.astimezone(timezone.utc).timestamp() * 1000)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        aware = parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else parsed.replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
        return int(aware.astimezone(timezone.utc).timestamp() * 1000)
    return None


def _parse_json(value: Any, *, field: str) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a JSON string")
    return json.loads(value)


def _read_redis(client: Any, trade_date: str, symbols: Sequence[str]) -> dict[str, Any]:
    date_tag = _compact_date(trade_date)
    selected = tuple(sorted({_strict_symbol(item) for item in symbols}))
    snapshots: dict[str, Any] = {}
    for tag in TAGS:
        key = f"market:auction:{date_tag}:{tag}"
        raw = dict(client.hgetall(key))
        snapshots[tag] = {
            "key": key,
            "meta": _parse_json(raw["meta"], field=f"{key}.meta") if raw.get("meta") else None,
            "summary": _parse_json(raw["summary"], field=f"{key}.summary") if raw.get("summary") else None,
            "top_amount": _parse_json(raw["top_amount"], field=f"{key}.top_amount") if raw.get("top_amount") else [],
        }
        snapshots[tag]["top_amount_count"] = len(snapshots[tag]["top_amount"])
    anchor_key = f"market:auction:anchor:{date_tag}"
    anchor_raw = client.get(anchor_key)
    anchor = _parse_json(anchor_raw, field=anchor_key) if anchor_raw else {}
    selected_anchor = {
        symbol: anchor.get(symbol)
        for symbol in selected
        if isinstance(anchor, Mapping) and symbol in anchor
    }
    return {
        "trade_date": trade_date,
        "symbols": selected,
        "snapshots": snapshots,
        "anchor": {"key": anchor_key, "rows": selected_anchor},
    }


def _read_td(connection: Any, trade_date: str, symbols: Sequence[str]) -> list[tuple[Any, ...]]:
    date_tag = _compact_date(trade_date)
    selected = tuple(sorted({_strict_symbol(item) for item in symbols}))
    quoted_symbols = ",".join(f'"{symbol}"' for symbol in selected)
    sql = (
        "SELECT ts, px_milli, match_amt_yuan, rest_bid_amt_yuan, rest_ask_amt_yuan, "
        "symbol, trade_date, auction_tag "
        "FROM market_data1.auction_snapshot_v2 "
        f'WHERE trade_date="{date_tag}" AND symbol IN ({quoted_symbols}) '
        'AND auction_tag IN ("0920","0924","0925") ORDER BY symbol,ts'
    )
    cursor = connection.cursor()
    cursor.execute(sql)
    return list(cursor.fetchall())


def _td_map(rows: Sequence[Sequence[Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if len(row) != 8:
            raise ValueError("TD row has an unexpected column count")
        ts, px, amount, bid, ask, symbol, trade_date, tag = row
        key = (_strict_symbol(str(symbol)), str(tag))
        if key in result:
            raise ValueError(f"duplicate TD row for {key[0]} {key[1]}")
        result[key] = {
            "source_record_time": str(ts),
            "source_record_time_ms": _epoch_ms(ts),
            "px_milli": px,
            "match_amt_yuan": amount,
            "rest_bid_amt_yuan": bid,
            "rest_ask_amt_yuan": ask,
            "symbol": key[0],
            "trade_date": str(trade_date),
            "auction_tag": key[1],
        }
    return result


def _top_map(redis_data: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for tag, payload in redis_data["snapshots"].items():
        for row in payload.get("top_amount", []):
            symbol = str(row.get("symbol") or "").strip()
            if symbol:
                result[(symbol, tag)] = row
    return result


def compare_projections(
    redis_data: Mapping[str, Any],
    td_rows: Sequence[Sequence[Any]],
) -> dict[str, Any]:
    """Compare only shared, writer-documented fields; never infer absent data."""

    symbols = tuple(redis_data["symbols"])
    td = _td_map(td_rows)
    top = _top_map(redis_data)
    anchor_rows = redis_data["anchor"]["rows"]
    comparisons: list[dict[str, Any]] = []
    for symbol in symbols:
        anchor = anchor_rows.get(symbol)
        for tag in TAGS:
            td_row = td.get((symbol, tag))
            top_row = top.get((symbol, tag))
            if td_row is None:
                comparisons.append({
                    "symbol": symbol,
                    "tag": tag,
                    "status": "TD_MISSING",
                    "timestamp_status": "NOT_COMPARABLE",
                })
                continue
            redis_row: Mapping[str, Any] | None
            redis_source: str
            comparability_reason: str
            if tag == ANCHOR_TAG and isinstance(anchor, Mapping):
                redis_row = anchor
                redis_source = "redis_anchor"
                comparability_reason = ""
                redis_fields = {
                    "match_amt_yuan": redis_row.get("amount"),
                    "rest_bid_amt_yuan": redis_row.get("bid_amount"),
                    "rest_ask_amt_yuan": redis_row.get("ask_amount"),
                }
            elif isinstance(top_row, Mapping):
                redis_row = top_row
                redis_source = "redis_top_amount"
                comparability_reason = ""
                redis_fields = {
                    "match_amt_yuan": redis_row.get("auction_amount_yuan"),
                    "rest_bid_amt_yuan": redis_row.get("bid_amount_yuan"),
                    "rest_ask_amt_yuan": redis_row.get("ask_amount_yuan"),
                }
            else:
                redis_row = None
                redis_source = "unavailable"
                snapshot = redis_data["snapshots"].get(tag, {})
                top_rows = snapshot.get("top_amount", []) if isinstance(snapshot, Mapping) else []
                top_count = snapshot.get("top_amount_count", len(top_rows)) if isinstance(snapshot, Mapping) else 0
                comparability_reason = (
                    "symbol_outside_redis_top_amount_window"
                    if top_count
                    else "redis_top_amount_unavailable"
                )
                redis_fields = {}
            redis_meta = redis_data["snapshots"].get(tag, {}).get("meta")
            redis_meta_ts = redis_meta.get("ts") if isinstance(redis_meta, Mapping) else None
            td_time_ms = td_row.get("source_record_time_ms")
            redis_time_ms = _epoch_ms(redis_meta_ts)
            if redis_time_ms is None or td_time_ms is None:
                timestamp_status = "NOT_COMPARABLE"
            else:
                timestamp_status = "MATCH" if redis_time_ms == td_time_ms else "MISMATCH"
            fields: dict[str, Any] = {}
            for field, td_field in (
                ("match_amt_yuan", "match_amt_yuan"),
                ("rest_bid_amt_yuan", "rest_bid_amt_yuan"),
                ("rest_ask_amt_yuan", "rest_ask_amt_yuan"),
            ):
                redis_value = redis_fields.get(field)
                td_value = td_row.get(td_field)
                if redis_value is None:
                    if redis_row is None:
                        field_reason = comparability_reason
                    elif redis_source == "redis_anchor":
                        field_reason = "redis_anchor_field_absent"
                    else:
                        field_reason = "redis_top_amount_field_absent"
                    fields[field] = {"status": "NOT_COMPARABLE", "redis": None, "td": td_value}
                    fields[field]["reason"] = field_reason
                else:
                    fields[field] = {
                        "status": "MATCH" if redis_value == td_value else "MISMATCH",
                        "redis": redis_value,
                        "td": td_value,
                    }
            statuses = [item["status"] for item in fields.values()]
            if timestamp_status == "MISMATCH" or "MISMATCH" in statuses:
                status = "MISMATCH"
            elif "MATCH" not in statuses:
                status = "NOT_COMPARABLE"
            elif "NOT_COMPARABLE" in statuses:
                status = "PARTIAL_COMPARABLE"
            else:
                status = "MATCH"
            comparisons.append({
                "symbol": symbol,
                "tag": tag,
                "redis_source": redis_source,
                "comparability_reason": comparability_reason,
                "td_source": "td:market_data1.auction_snapshot_v2",
                "td_source_record_time": td_row["source_record_time"],
                "redis_snapshot_time_ms": redis_time_ms,
                "td_source_record_time_ms": td_time_ms,
                "timestamp_status": timestamp_status,
                "redis_row": redis_row,
                "td_row": td_row,
                "fields": fields,
                "status": status,
            })
    summary = {
        "match": sum(item["status"] == "MATCH" for item in comparisons),
        "partial_comparable": sum(item["status"] == "PARTIAL_COMPARABLE" for item in comparisons),
        "not_comparable": sum(item["status"] in {"NOT_COMPARABLE", "TD_MISSING"} for item in comparisons),
        "mismatch": sum(item["status"] == "MISMATCH" for item in comparisons),
        "timestamp_mismatch": sum(item["timestamp_status"] == "MISMATCH" for item in comparisons),
    }
    payload = {
        "comparison_contract": "RedisTDSharedAuctionFieldsV1",
        "trade_date": redis_data["trade_date"],
        "symbols": symbols,
        "comparisons": comparisons,
        "summary": summary,
    }
    payload["semantic_hash"] = semantic_hash(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbols", default=",".join(SYMBOLS_DEFAULT))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trade_date = _strict_date(args.trade_date)
    symbols = tuple(_strict_symbol(item.strip()) for item in args.symbols.split(",") if item.strip())
    if not symbols:
        parser.error("symbols must not be empty")
    import redis  # type: ignore[import-not-found]
    import taos  # type: ignore[import-not-found]

    redis_client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    td_connection = taos.connect(
        host=os.environ.get("TDENGINE_HOST", "127.0.0.1"),
        port=int(os.environ.get("TDENGINE_PORT", "6030")),
        user=os.environ.get("TDENGINE_USER", "root"),
        password=os.environ.get("TDENGINE_PASSWORD", "taosdata"),
        database=os.environ.get("TDENGINE_DATABASE", "market_data1"),
    )
    try:
        redis_data = _read_redis(redis_client, trade_date, symbols)
        td_rows = _read_td(td_connection, trade_date, symbols)
        result = compare_projections(redis_data, td_rows)
        result.update({
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "side_effect_boundary": "Redis HGETALL/GET + TD SELECT only; no writes, ACK, repair or effect",
        })
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    finally:
        td_connection.close()
        redis_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
