#!/usr/bin/env python3
"""Rebuild registry artifacts from retained Core catalog inputs (dry-run by default)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TZ = ZoneInfo("Asia/Shanghai")


def _manifest(catalog, dataset, day):
    entries = catalog.read().get("datasets", {}).get(dataset, {}).get("dates", {}).get(day, [])
    for entry in reversed(entries):
        try:
            path = Path(entry["manifest"])
            value = json.loads(path.read_text(encoding="utf-8"))
            quality = value.get("quality")
            status = quality.get("status") if isinstance(quality, dict) else quality
            if status != "FAILED":
                return path, value
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _load(catalog, dataset, day):
    found = _manifest(catalog, dataset, day)
    return None if found is None else (catalog.read_rows(found[0]), found[1])


def _previous(root, day):
    from engine_core.prep_trade_day import previous_trade_date
    return previous_trade_date(root, day)


def fetch_item(item: str, day: str, artifact_root: str | Path, *, execute: bool = False):
    from engine_core.data_pipeline.storage import ArtifactCatalog
    from engine_core.data_pipeline.ops import derive_stock_day_facts
    root = Path(artifact_root).expanduser()
    catalog = ArtifactCatalog(root)
    if item not in {"stock_day_facts", "stock_theme_assignment"}:
        return {"status": "failed", "error": "unsupported_item", "retryable": False}
    if item == "stock_day_facts":
        source = _load(catalog, "daily_kline", day)
        if source is None:
            return {"status": "failed", "error": "daily_kline_missing", "retryable": True}
        rows, source_manifest = source
        facts, _quality = derive_stock_day_facts(
            day, rows, {"source_version": source_manifest.get("version")})
        available_at = datetime.now(TZ).isoformat()
        manifest = {"trade_date": day, "as_of": available_at, "available_at": available_at,
                    "source_dataset": "daily_kline", "source_version": source_manifest.get("version"),
                    "quality": {"status": "READY"}, "business_status": "READY"}
        result = {"item": item, "trade_date": day, "rows": len(facts), "read_only": not execute,
                  "source_version": source_manifest.get("version"), "available_at": available_at,
                  "quality": "CLOSE_PRICE_PROXY", "early_fields": "missing"}
        if execute:
            committed = catalog.commit(item, day, facts, manifest, source="registry_item_fetch")
            result["version"] = committed["version"]
        return {"status": "ok", **result}

    previous = _previous(root, day)
    if not previous:
        return {"status": "failed", "error": "trading_calendar_missing", "retryable": True}
    required = {"limit_up_pool": previous, "hot_plates": previous,
                "plate_members": previous}
    loaded = {name: _load(catalog, name, part) for name, part in required.items()}
    missing = [name for name, data in loaded.items() if data is None]
    registry = _manifest(catalog, "plate_registry", day)
    if registry is None:
        dates = catalog.read().get("datasets", {}).get("plate_registry", {}).get("dates", {})
        candidates = sorted(d for d in dates if d <= day)
        registry = next((_manifest(catalog, "plate_registry", d) for d in reversed(candidates)
                         if _manifest(catalog, "plate_registry", d)), None)
    if registry is None:
        missing.append("plate_registry")
    if missing:
        return {"status": "failed", "error": "inputs_missing:" + ",".join(missing), "retryable": True}
    from engine_core.prep_trade_day import build_and_commit_stock_theme_assignment
    pool_rows, pool_manifest = loaded["limit_up_pool"]
    hot_rows, hot_manifest = loaded["hot_plates"]
    member_rows, member_manifest = loaded["plate_members"]
    registry_rows = catalog.read_rows(registry[0])
    names = {str(r.get("plate_code", r.get("code", ""))): str(r.get("name", "")) for r in registry_rows}
    from engine_core.data_pipeline.trade_calendar import TradeCalendarStore
    from engine_core.slices import _pit_rows
    try:
        calendar = TradeCalendarStore(root)
        calendar_start = (datetime.fromisoformat(previous) - timedelta(days=90)).date().isoformat()
        trading_dates = [str(d) for d in calendar.trading_dates(calendar_start, previous)]
    except ValueError:
        return {"status": "failed", "error": "trading_calendar_history_missing", "retryable": True}
    reasons, reason_versions = [], {}
    reason_dates = catalog.read().get("datasets", {}).get("limit_reasons", {}).get("dates", {})
    for reason_day in sorted(d for d in reason_dates if d <= day):
        source = _load(catalog, "limit_reasons", reason_day)
        if source is None:
            continue
        source_rows, source_manifest = source
        for row in source_rows:
            row.setdefault("available_at", source_manifest.get("available_at"))
        visible, _ = _pit_rows(source_rows, day + "T09:08:22+08:00")
        reasons.extend(visible)
        reason_versions[reason_day] = source_manifest.get("version")
    if not reason_versions:
        return {"status": "failed", "error": "inputs_missing:limit_reasons", "retryable": True}
    pool_rows = _pit_rows(pool_rows, day + "T09:08:22+08:00")[0]
    hot_rows = _pit_rows(hot_rows, day + "T09:08:22+08:00")[0]
    member_rows = _pit_rows(member_rows, day + "T09:08:22+08:00")[0]
    registry_rows = _pit_rows(registry_rows, day + "T09:08:22+08:00")[0]
    members = {}
    for row in member_rows:
        sym = str(row.get("symbol", row.get("stock_code", row.get("code", ""))))[-6:].zfill(6)
        code = str(row.get("plate_code", row.get("plateid", row.get("plate", ""))))
        if sym.strip("0") and code:
            members.setdefault(sym, set()).add(code)
    pool_labels = {}
    from engine_core.limit_reason_theme import _pool_rows, _pool_values
    for row in _pool_rows(pool_rows):
        sym, label, topics, code, _tier = _pool_values(row)
        candidates = ([{"id": code, "name": names.get(code, label or code)}] if code else [])
        candidates.extend({"id": topic, "name": topic} for topic in topics if topic)
        if sym and candidates:
            pool_labels[sym] = candidates
    hot_codes = {}
    for row in hot_rows:
        code = str(row.get("plate_code", row.get("code", "")) or "").strip()
        name = str(row.get("name", row.get("plate_name", "")) or "").strip()
        if code and name:
            hot_codes[code] = {"id": code, "name": name}
    hot_by_symbol = {sym: [hot_codes[code] for code in sorted(codes) if code in hot_codes]
                     for sym, codes in members.items()}
    available_at = datetime.now(TZ).isoformat()
    manifest_fields = {"late_audit_rebuild": True,
                       "authority": "NON_AUTHORITATIVE_AUDIT_ONLY",
                       "report_consumed": False}
    source_versions = {"pool": pool_manifest.get("version"), "hot": hot_manifest.get("version"),
                       "members": member_manifest.get("version"), "reasons": reason_versions,
                       "registry": registry[1].get("version")}
    committed = build_and_commit_stock_theme_assignment(
        catalog, trade_date=day, previous_trade_date=previous,
        pool_labels=pool_labels, reason_rows=reasons,
        static_themes={s: [{"id": c, "name": names[c]} for c in cs if c in names]
                       for s, cs in members.items()},
        hot_plate_names=hot_by_symbol,
        members={s: sorted(cs) for s, cs in members.items()},
        trading_dates=trading_dates, available_at=available_at,
        source_versions=source_versions, as_of=previous + "T23:59:59+08:00",
        row_metadata={"late_audit_rebuild": True,
                      "authority": "NON_AUTHORITATIVE_AUDIT_ONLY"},
        manifest_metadata=manifest_fields, commit_catalog=execute,
        source="late_audit_rebuild")
    row_count = committed["row_count"]
    result = {"item": item, "trade_date": day, "rows": row_count, "read_only": not execute,
              "late_audit_rebuild": True, "authority": "NON_AUTHORITATIVE_AUDIT_ONLY"}
    if execute:
        result["version"] = committed["version"]
    return {"status": "ok", **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item", required=True, choices=["stock_day_facts", "stock_theme_assignment"])
    parser.add_argument("--date", required=True)
    parser.add_argument("--artifact-root", default="~/core-data")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    result = fetch_item(args.item, args.date, args.artifact_root, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
