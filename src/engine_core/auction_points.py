"""Normalize and archive the bounded auction point hashes emitted by t1-v2."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from .data_pipeline.storage import ArtifactCatalog
from .data_pipeline.storage import canonical_json


def _json_field(fields: Mapping[str, Any], name: str, default: Any) -> Any:
    value = fields.get(name)
    if value in (None, ""):
        return default
    try:
        return json.loads(value) if isinstance(value, (str, bytes)) else value
    except (ValueError, TypeError):
        return default


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
        return int(number) if number.is_finite() and number == number.to_integral_value() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _milli_yuan(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value)) * 1000
        return int(number) if number.is_finite() and number == number.to_integral_value() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def normalize_auction_capture(trade_date: str, captures: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return one row per source/rank row; unknown fields remain null, never zero-filled."""
    day = trade_date.replace("-", "")
    output: list[dict[str, Any]] = []
    for capture in captures:
        key = str(capture.get("key") or "")
        parts = key.split(":")
        if (len(parts) == 3 and parts[0] == "a2" and parts[1] == day):
            source, tag = "a2", parts[2]
        elif (len(parts) == 4 and parts[:2] == ["market", "auction"] and parts[2] == day):
            source, tag = "market:auction", parts[3]
        else:
            continue
        if tag not in {"0920", "0924", "0925", "latest"}:
            continue
        fields = capture.get("fields") if isinstance(capture.get("fields"), Mapping) else {}
        meta = _json_field(fields, "meta", {})
        summary = _json_field(fields, "summary", {})
        point_ts = _integer(meta.get("ts")) if isinstance(meta, Mapping) else None
        common = {"trade_date": f"{day[:4]}-{day[4:6]}-{day[6:8]}", "tag": tag,
                  "point_ts": point_ts, "captured_at": capture.get("fetched_at"),
                  "source_key": source, "source_redis_key": key}
        if source == "market:auction":
            if isinstance(summary, Mapping):
                output.append({**common, "symbol": "__MARKET__", "row_kind": "summary",
                               "total_stocks": _integer(summary.get("total_stocks")),
                               "valid_stock_count": _integer(summary.get("valid_stock_count")),
                               "unavailable_stock_count": _integer(summary.get("unavailable_stock_count")),
                               "total_auction_amount_yuan": _integer(summary.get("total_auction_amount_yuan")),
                               "total_limit_up_bid_amount_yuan": _integer(summary.get("total_limit_up_bid_amount_yuan"))})
            ranks = [("top_amount", _json_field(fields, "top_amount", []))]
        else:
            ranks = [(name, _json_field(fields, name, [])) for name in ("top_chg", "top_br", "top_amt")]
        for rank_name, ranked in ranks:
            if not isinstance(ranked, list):
                continue
            for rank, item in enumerate(ranked, 1):
                if not isinstance(item, Mapping):
                    continue
                symbol = str(item.get("symbol") or item.get("s") or "").strip()
                if not symbol.isdigit() or len(symbol) > 6:
                    continue
                symbol = symbol.zfill(6)
                price = _milli_yuan(item.get("price")) if "price" in item else _integer(item.get("px"))
                change = None
                if item.get("change_pct") not in (None, ""):
                    try:
                        raw_change = Decimal(str(item["change_pct"])) * 10000
                        change = int(raw_change) if raw_change == raw_change.to_integral_value() else None
                    except (InvalidOperation, ValueError, TypeError):
                        pass
                else:
                    change = _integer(item.get("chg"))
                output.append({**common, "symbol": symbol, "row_kind": "rank", "rank_list": rank_name,
                               "rank": rank, "price_milli": price, "change_bp": change,
                               "auction_amount_yuan": _integer(item.get("auction_amount_yuan", item.get("am"))),
                               "rest_bid_amount_yuan": _integer(item.get("bid_amount_yuan", item.get("br"))),
                               "rest_ask_amount_yuan": _integer(item.get("ask_amount_yuan", item.get("ar"))),
                               "limit_state": _integer(item.get("ls"))})
    return sorted(output, key=lambda row: (row["tag"], row["source_key"], row["symbol"],
                                           row.get("rank_list", ""), row.get("rank", 0)))


def import_raw_auction_captures(trade_date: str, *, raw_root: str | Path | None = None,
                                core_root: str | Path | None = None,
                                tags: Sequence[str] | None = None) -> dict[str, Any]:
    raw_root = Path(raw_root or Path.home() / "core-data" / "auction_points_raw")
    core_root = Path(core_root or Path.home() / "core-data")
    day = trade_date.replace("-", "")
    manifest_path = ArtifactCatalog(core_root).locate("auction_points", trade_date)
    if manifest_path:
        return {"status": "ALREADY_EXISTS", "manifest": str(manifest_path)}
    raw_day = raw_root / f"trade_date={trade_date}"
    if not raw_day.is_dir():
        raw_day = raw_root / f"trade_date={day}"
    wanted = set(tags or ("0920", "0924", "0925"))
    captures = []
    for path in sorted(raw_day.glob("*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                item = json.load(handle)
        except (OSError, ValueError):
            continue
        key = str(item.get("key") or "")
        if key.endswith(tuple(f":{tag}" for tag in wanted)):
            captures.append(item)
    rows = normalize_auction_capture(trade_date, captures)
    if not rows:
        raise ValueError(f"no_auction_points:{trade_date}:{','.join(sorted(wanted))}")
    catalog = ArtifactCatalog(core_root)
    manifest = {"schema": "auction_points_v1", "trade_date": trade_date, "source": "t1_v2_redis_archive",
                "available_at": max((str(row.get("captured_at") or "") for row in rows), default=None),
                "as_of": max((row.get("point_ts") or 0 for row in rows), default=0),
                "quality": "PARTIAL_RANKED_HASHES", "business_status": "PARTIAL",
                "source_keys": sorted({row["source_redis_key"] for row in rows}),
                "captured_rows": len(captures), "tag_scope": sorted(wanted)}
    committed = catalog.commit("auction_points", trade_date, rows, manifest, source="t1_v2_redis_archive")
    _publish_redis(trade_date, committed, rows)
    committed["status"] = "IMPORTED"
    return committed


def _publish_redis(trade_date: str, manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    """Publish the manifest and row payload under the guarded Core namespace only."""
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return
    payload = canonical_json({"version": manifest["version"], "sha256": manifest["sha256"], "rows": rows})
    if len(payload) > 64 * 1024 * 1024:
        raise ValueError("auction_points_redis_payload_over_64MiB")
    try:
        import redis
        client = redis.Redis.from_url(redis_url, decode_responses=True)
        client.set(f"core:cache:data:auction_points:{trade_date}", payload.decode("utf-8"), ex=604800)
    except ImportError as exc:
        raise RuntimeError("redis_package_required_for_publish") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import archived t1-v2 auction Redis point hashes")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--tag", action="append", choices=("0920", "0924", "0925"))
    parser.add_argument("--raw-root")
    parser.add_argument("--core-root")
    args = parser.parse_args(argv)
    result = import_raw_auction_captures(args.date, raw_root=args.raw_root, core_root=args.core_root, tags=args.tag)
    print(json.dumps({key: result.get(key) for key in ("status", "version", "row_count", "sha256", "quality", "manifest_uri")},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
