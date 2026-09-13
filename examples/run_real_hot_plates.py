"""Run the date-bound Kaipan hot-plate cache through the core boundary.

The Redis path is explicitly read-only.  A cache update timestamp is retained
as observation metadata but is never promoted to historical availability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from engine_core import (
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


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _read_redis_rows(
    client: Any,
    trade_date: str,
) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    key = "cache:hot_plates:" + trade_date
    meta_key = "cache:hot_plates_meta:" + trade_date
    key_type = _text(client.type(key))
    if key_type != "hash":
        return [], {
            "key": key,
            "meta_key": meta_key,
            "redis_type": key_type,
            "row_count": 0,
            "scan_consistent": True,
            "meta": None,
        }

    hlen_before = int(client.hlen(key))
    raw_items = list(client.hscan_iter(key))
    hlen_after = int(client.hlen(key))
    rows: list[Mapping[str, Any]] = []
    payload_hash = hashlib.sha256()
    decode_errors = 0
    for field, raw in sorted(raw_items, key=lambda item: _text(item[0])):
        field_text = _text(field)
        raw_text = _text(raw)
        payload_hash.update(field_text.encode("utf-8"))
        payload_hash.update(b"\0")
        payload_hash.update(raw_text.encode("utf-8"))
        payload_hash.update(b"\n")
        try:
            value = json.loads(raw_text)
        except (TypeError, ValueError):
            decode_errors += 1
            continue
        if isinstance(value, Mapping):
            rows.append(value)
        else:
            decode_errors += 1

    raw_meta = client.get(meta_key)
    if isinstance(raw_meta, bytes):
        raw_meta = raw_meta.decode("utf-8")
    try:
        meta = json.loads(raw_meta) if raw_meta else None
    except (TypeError, ValueError):
        meta = {"valid_json": False}
    return rows, {
        "key": key,
        "meta_key": meta_key,
        "redis_type": key_type,
        "hlen_before_scan": hlen_before,
        "hlen_after_scan": hlen_after,
        "scan_item_count": len(raw_items),
        "scan_consistent": hlen_before == hlen_after == len(raw_items),
        "row_count": len(rows),
        "decode_errors": decode_errors,
        "payload_value_sha256": payload_hash.hexdigest(),
        "meta": meta,
    }


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
    )
    request = DataRequest(
        request_id="real-hot-plates-" + trade_date,
        function_id="hot_plates",
        trade_date=trade_date,
        effective_as_of_ms=observed_at_ms,
        knowledge_as_of_ms=observed_at_ms,
        purpose="read_only_m2_probe",
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
