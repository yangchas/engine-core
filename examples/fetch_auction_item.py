#!/usr/bin/env python3
"""Late rebuild of auction products from retained TD snapshots (never a live capture)."""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src"))

TZ = ZoneInfo("Asia/Shanghai")


def _require_snapshot_coverage(symbols, universe_count=None):
    from engine_core.guardian.gates import FALLBACK_UNIVERSE_COUNT, SNAPSHOT_COVERAGE_THRESHOLD
    universe = int(universe_count or FALLBACK_UNIVERSE_COUNT)
    required = max(1, math.ceil(universe * SNAPSHOT_COVERAGE_THRESHOLD))
    count = len(set(symbols))
    if count < required:
        raise RuntimeError(f"SNAPSHOT_COVERAGE_LOW:{count}/{universe}:required={required}")
    return count


def _query(cursor, sql):
    cursor.execute(sql)
    names = [str(c[0]).lower().split(".")[-1] for c in (cursor.description or ())]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _source_rows(day: str):
    if not os.environ.get("TD_CONFIG"):
        raise RuntimeError("TD_CONFIG_missing")
    import taos
    connection = taos.connect(database="market_data1", config=os.environ["TD_CONFIG"])
    try:
        cursor = connection.cursor()
        compact = day.replace("-", "")
        now = datetime.now(TZ)
        live = now.date().isoformat() == day and clock_time(9, 25, 10) <= now.timetz().replace(tzinfo=None) < clock_time(9, 30)
        source_cutoff = now.strftime("%Y-%m-%d %H:%M:%S") if live else f"{day} 09:26:00"
        snaps = _query(cursor,
            "SELECT ts,symbol,px_milli AS price_milli,chg_bp AS change_bp,"
            "match_amt_yuan AS auction_amount_yuan,trade_date,auction_tag "
            f'FROM auction_snapshot_v2 WHERE trade_date="{compact}" AND ts<="{source_cutoff}" '
            "ORDER BY symbol, ts")
        return snaps, []
    finally:
        connection.close()


def _pit_rows(catalog, dataset: str, dates, cutoff: datetime, *, late_cutoff: datetime | None = None):
    """Read the newest catalog version whose availability proves PIT visibility.

    ``late_cutoff`` (late rebuilds only, prior-day content): a version stored after ``cutoff`` is still accepted when
    stored before ``late_cutoff``; it is reported with ``late_visible`` so consumers can see it was not live-visible."""
    data = catalog.read().get("datasets", {}).get(dataset, {})
    by_date = data.get("dates", {})
    for day in sorted((str(value) for value in dates if str(value) in by_date), reverse=True):
        for entry in reversed(by_date.get(day, [])):
            manifest_path = Path(entry.get("manifest", ""))
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                available = datetime.fromisoformat(str(manifest.get("available_at", "")).replace("Z", "+00:00"))
                if available.tzinfo is None or available > (late_cutoff or cutoff):
                    continue
                rows = catalog.read_rows(manifest_path)
                meta = {"dataset": dataset, "trade_date": day,
                        "version": manifest.get("version"), "available_at": manifest.get("available_at")}
                if available > cutoff:
                    meta["late_visible"] = True
                return rows, meta
            except (OSError, ValueError, TypeError, KeyError):
                continue
    return None, None


def _products(day: str, item: str, root: Path, snapshots, ticks, *, source_catalog_root=None):
    from engine_core.auction_stock_facts import build_auction_stock_facts, store_auction_stock_facts
    read_at = datetime.now(TZ).isoformat()
    today = datetime.fromisoformat(read_at).date().isoformat() == day
    local_now = datetime.fromisoformat(read_at).timetz().replace(tzinfo=None)
    live = today and clock_time(9, 25, 10) <= local_now < clock_time(9, 30)
    capture_semantics = "live_0925_capture" if live else "late_td_rebuild"
    cutoff = f"{day}T09:25:10+08:00"
    cutoff_text = cutoff.replace("T", " ")[:19]
    visible_snapshots = [row for row in snapshots
                         if str(row.get("ts") or "")[:19] <= cutoff_text]
    final_snapshots = [row for row in visible_snapshots
                       if str(row.get("auction_tag") or "") == "0925"]
    if item == "auction_stock_facts":
        final_symbols = {str(row.get("symbol") or "").zfill(6) for row in final_snapshots}
        _require_snapshot_coverage(final_symbols)
    facts = build_auction_stock_facts(day, cutoff, final_snapshots, ticks,
                                     anchor_snapshots=visible_snapshots,
                                     now=read_at if live else None,
                                     read_at=read_at if live else None)
    if item == "auction_stock_facts":
        stored = store_auction_stock_facts(facts, artifact_root=root,
                                           capture_semantics=capture_semantics)
        return {"dataset": item, "version": stored.get("version"), "rows": len(facts["rows"]),
                "status": "ok", "capture_semantics": capture_semantics}
    from engine_core.data_pipeline.storage import ArtifactCatalog
    catalog = ArtifactCatalog(root)
    if item == "auction_points":
        rows = []
        for tag in sorted({str(row.get("auction_tag") or "") for row in visible_snapshots}):
            tagged = [row for row in visible_snapshots if str(row.get("auction_tag") or "") == tag]
            tagged.sort(key=lambda row: (-float(row.get("auction_amount_yuan") or 0),
                                         str(row.get("symbol") or "")))
            rows.extend({"trade_date": day, "tag": tag, "rank_name": "amount", "rank": rank,
                         "symbol": str(row.get("symbol") or "").zfill(6),
                         "auction_amount_yuan": row.get("auction_amount_yuan"),
                         "pct_change": (float(row["change_bp"]) / 100 if row.get("change_bp") is not None else None),
                         "source": "TD auction_snapshot_v2", "late_rebuild": not live}
                        for rank, row in enumerate(tagged[:100], 1))
        available_at = datetime.now(TZ).isoformat()
        manifest = catalog.commit(item, day, rows,
            {"schema": "auction_points_v1", "trade_date": day,
             "as_of": cutoff, "available_at": available_at,
             "late_rebuild": not live, "not_a_0925_live_capture": not live,
             "capture_semantics": capture_semantics,
             "quality": {"capture_semantics": capture_semantics},
             "source": "TD auction_snapshot_v2"},
            source="TD auction_snapshot_v2 late rebuild")
        return {"dataset": item, "version": manifest.get("version"), "rows": len(rows),
                "status": "ok", "capture_semantics": capture_semantics}
    if item == "auction_panorama":
        from engine_core.auction_panorama import build_auction_panorama, store_auction_panorama
        cutoff_dt = datetime.fromisoformat(cutoff)
        source_root = Path(source_catalog_root or os.environ.get("CORE_DATA_ROOT", root)).expanduser()
        source_catalog = ArtifactCatalog(source_root)
        plate_dates = source_catalog.read().get("datasets", {}).get("plate_members", {}).get("dates", {})
        pool_dates = source_catalog.read().get("datasets", {}).get("limit_up_pool", {}).get("dates", {})
        prior_plate_dates = (value for value in plate_dates if value < day)
        # Yesterday's pool must be the immediately preceding trade day: an older day is stale, never a stand-in.
        try:
            from engine_core.trade_day_context import previous_trade_date as context_previous
            previous_day = context_previous(source_root, day)
        except Exception:
            previous_day = None
        prior_pool_dates = [previous_day] if previous_day and previous_day in pool_dates else []
        plate_members, plate_meta = _pit_rows(source_catalog, "plate_members", prior_plate_dates, cutoff_dt)
        if plate_meta and previous_day and plate_meta["trade_date"] != previous_day:
            # Membership may legitimately be the newest visible older day, but never silently: say how stale it is.
            plate_meta["stale_vs_previous_trade_day"] = previous_day
        pool, pool_meta = _pit_rows(source_catalog, "limit_up_pool", prior_pool_dates, cutoff_dt,
                                    late_cutoff=None if live else datetime.now(cutoff_dt.tzinfo))
        missing = [name for name, value in (("plate_members", plate_members),
                                             ("yesterday_pool", pool)) if value is None]
        product = build_auction_panorama(facts, plate_members=plate_members,
                                         yesterday_pool=pool, previous_trade_date=None)
        product["late_rebuild"] = not live
        product["capture_semantics"] = capture_semantics
        product["input_quality"] = {"missing_inputs": missing,
                                    "plate_members": plate_meta, "yesterday_pool": pool_meta}
        if missing:
            product["status"] = "PARTIAL"
        stored = store_auction_panorama(product, artifact_root=root)
        return {"dataset": item, "version": stored.get("version"), "rows": 1,
                "status": "degraded" if missing else "ok", "missing_inputs": missing,
                "capture_semantics": capture_semantics}
    raise ValueError("unsupported_auction_item:" + item)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item", required=True,
                        choices=["auction_stock_facts", "auction_points", "auction_panorama"])
    parser.add_argument("--date", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    args = parser.parse_args(argv)
    snapshots, ticks = _source_rows(args.date)
    result = {"item": args.item, "trade_date": args.date, "snapshot_rows": len(snapshots),
              "tick_rows": len(ticks), "read_only": not args.execute,
              "capture_semantics": "late TD auction_snapshot_v2 rebuild; not 09:25 live product"}
    if args.execute:
        result.update(_products(args.date, args.item, Path(args.artifact_root).expanduser(), snapshots, ticks))
    payload = {"status": "ok", **result}
    if args.execute:
        payload.update({key: value for key, value in result.items() if key in {"status", "missing_inputs"}})
        if payload.get("status") == "ok" and result.get("missing_inputs"):
            payload["status"] = "degraded"
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
