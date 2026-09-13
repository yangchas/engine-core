"""Read-only inventory for legacy Redis cache dialects.

This is an evidence tool, not a runtime adapter.  It deliberately reads the
Redis key type before decoding a value, summarizes JSON payloads without
emitting their contents, and never calls a Redis write command.

Example (on Cobra):

    PYTHONPATH=src python examples/run_real_cache_inventory.py \
      --trade-date 2026-09-10 --previous-trade-date 2026-09-09 \
      --output /home/exedev/validation/cache-inventory.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CONTRACT_VERSION = "RealLegacyCacheInventoryV1"
MAX_UNIQUE_VALUES = 20


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _date_text(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("date must be strict YYYY-MM-DD")
    return value


def _json_summary(
    raw_items: Iterable[tuple[Any, Any]],
    expected_trade_date: str | None,
) -> dict[str, Any]:
    decoded = 0
    invalid = 0
    exact_date = 0
    trade_dates: set[str] = set()
    sources: set[str] = set()
    json_kinds: set[str] = set()
    field_names: set[str] = set()
    value_hash = hashlib.sha256()

    normalized_items = sorted(
        (_decode(field), _decode(raw)) for field, raw in raw_items
    )
    for field, text in normalized_items:
        value_hash.update(field.encode("utf-8"))
        value_hash.update(b"\0")
        value_hash.update(text.encode("utf-8"))
        value_hash.update(b"\n")
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            invalid += 1
            continue
        decoded += 1
        json_kinds.add(type(value).__name__)
        if not isinstance(value, dict):
            continue
        field_names.update(str(key) for key in value)
        payload_date = str(value.get("trade_date") or "").strip()
        if payload_date:
            trade_dates.add(payload_date)
            if expected_trade_date and payload_date == expected_trade_date:
                exact_date += 1
        source = str(value.get("source") or "").strip()
        if source:
            sources.add(source)

    return {
        "decoded_json_count": decoded,
        "invalid_json_count": invalid,
        "json_kinds": sorted(json_kinds),
        "exact_trade_date_count": exact_date,
        "trade_dates": sorted(trade_dates)[:MAX_UNIQUE_VALUES],
        "sources": sorted(sources)[:MAX_UNIQUE_VALUES],
        "field_names": sorted(field_names),
        "value_sha256": value_hash.hexdigest(),
    }


def _meta_summary(raw: Any, expected_trade_date: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    text = _decode(raw)
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"valid_json": False, "value_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    if not isinstance(value, dict):
        return {"valid_json": True, "json_kind": type(value).__name__}
    result = {
        "valid_json": True,
        "metadata_fields": sorted(str(key) for key in value),
        "trade_date": str(value.get("trade_date") or "").strip(),
        "trade_date_matches_request": (
            str(value.get("trade_date") or "").strip() == expected_trade_date
            if expected_trade_date
            else None
        ),
        "phase": str(value.get("phase") or "").strip(),
        "source": str(value.get("source") or "").strip(),
        "row_count": value.get("row_count"),
        "fetched_row_count": value.get("fetched_row_count"),
        "cache_row_count": value.get("cache_row_count"),
        "success": value.get("success"),
        "schema_version": value.get("schema_version"),
        "available_at_ms": value.get("available_at_ms"),
        "availability_basis": value.get("availability_basis"),
        "field_units": value.get("field_units") if isinstance(value.get("field_units"), dict) else None,
        "updated_at": value.get("updated_at"),
        "updated_at_ts": value.get("updated_at_ts"),
        "last_attempt_at": value.get("last_attempt_at"),
        "last_attempt_at_ts": value.get("last_attempt_at_ts"),
        "payload_sha256": value.get("payload_sha256"),
    }
    return result


def summarize_key(
    client: Any,
    key: str,
    expected_trade_date: str | None,
    *,
    meta_key: str = "",
) -> dict[str, Any]:
    """Summarize one key using only Redis reads."""

    key_type = _decode(client.type(key))
    result: dict[str, Any] = {
        "key": key,
        "redis_type": key_type,
        "exists": key_type != "none",
        "expected_trade_date": expected_trade_date,
    }
    if key_type == "hash":
        hlen_before = int(client.hlen(key))
        raw_items = list(client.hscan_iter(key))
        hlen_after = int(client.hlen(key))
        result["hlen"] = hlen_after
        result["hlen_before_scan"] = hlen_before
        result["scan_item_count"] = len(raw_items)
        result["scan_consistent"] = (
            hlen_before == hlen_after == len(raw_items)
        )
        result.update(_json_summary(raw_items, expected_trade_date))
    elif key_type == "string":
        raw = client.get(key)
        result["strlen"] = int(client.strlen(key))
        result["value_sha256"] = hashlib.sha256(_decode(raw).encode("utf-8")).hexdigest()
        result["meta_json"] = _meta_summary(raw, expected_trade_date)
    else:
        result["unsupported_type"] = key_type
    if meta_key:
        meta_type = _decode(client.type(meta_key))
        result["meta_key"] = meta_key
        result["meta_type"] = meta_type
        result["meta"] = (
            _meta_summary(client.get(meta_key), expected_trade_date)
            if meta_type == "string"
            else None
        )
    return result


def build_inventory(client: Any, trade_date: str, previous_trade_date: str) -> dict[str, Any]:
    trade_date = _date_text(trade_date)
    previous_trade_date = _date_text(previous_trade_date)
    if previous_trade_date >= trade_date:
        raise ValueError("previous trade date must be earlier than trade date")
    dated_keys = {
        f"cache:yest_limit_pool:{previous_trade_date}": (previous_trade_date, f"cache:yest_limit_pool_meta:{previous_trade_date}"),
        f"cache:hot_plates:{trade_date}": (trade_date, f"cache:hot_plates_meta:{trade_date}"),
        f"cache:hot_rank:{trade_date}": (trade_date, f"cache:hot_rank_meta:{trade_date}"),
        f"cache:limit_truth:{trade_date}": (trade_date, f"cache:limit_truth_meta:{trade_date}"),
        f"cache:stock_extra:{trade_date}": (trade_date, ""),
        f"cache:chip_peaks:{trade_date}": (trade_date, ""),
    }
    static_keys = {
        "config:plate_mapping:s2p": (None, ""),
        "config:plate_mapping:info": (None, ""),
        "config:plate_mapping:full_sync_info": (None, ""),
        "market:stock_plate": (None, ""),
        "market:stock_reason": (None, ""),
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "keys": {
            key: summarize_key(client, key, expected_date, meta_key=meta_key)
            for key, (expected_date, meta_key) in {**dated_keys, **static_keys}.items()
        },
        "side_effect_boundary": "Redis TYPE/EXISTS/HSCAN/HLEN/GET/STRLEN only; no Redis/TD/file writer",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-trade-date", required=True)
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.environ.get("REDIS_DB", "0")))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trade_date = _date_text(args.trade_date)
    previous_trade_date = _date_text(args.previous_trade_date)
    if previous_trade_date >= trade_date:
        raise SystemExit("previous trade date must be earlier than trade date")

    try:
        import redis
    except ImportError as exc:  # pragma: no cover - server dependency
        raise SystemExit(f"redis package is required: {exc}") from exc

    client = redis.Redis(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=os.environ.get("REDIS_PASSWORD"),
    )
    started = time.monotonic()
    client.ping()
    result = build_inventory(client, trade_date, previous_trade_date)
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
