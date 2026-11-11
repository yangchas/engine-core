#!/usr/bin/env python3
"""Build auction products from validated Redis views with explicit TD fallback."""
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
from engine_core.data_pipeline.job_contract import contract_main  # noqa: E402

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


def _td_source_rows(day: str):
    from engine_core.data_pipeline.td_sink import connect_td
    connection = connect_td("market_data1")
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


class SnapshotRows(list):
    """List-compatible rows carrying acquisition quality through the product chain."""
    def __init__(self, rows, source_quality):
        super().__init__(rows)
        self.source_quality = source_quality


def _source_rows(day: str):
    from engine_core.data_pipeline.auction_redis_view import TAGS, ViewError, read_auction_view, redis_client
    rows, quality, failed = [], {}, {}
    try:
        client = redis_client()
    except Exception:
        client = None
    for tag in TAGS:
        try:
            if client is None:
                raise ViewError('redis_error')
            selected = read_auction_view(client, day, tag)
            rows.extend(selected)
            src_counts = {name: sum(row['src'] == name for row in selected)
                          for name in ('auction_match', 'indicative_bid_ask_mid', 'missing')}
            quality[tag] = {'source': 'redis_auction_full', 'reason': None,
                            'rows': len(selected), 'src_counts': src_counts}
        except ViewError as exc:
            failed[tag] = str(exc)
    if failed:
        td_rows, _ = _td_source_rows(day)
        for tag, reason in failed.items():
            selected = [dict(row, source='td_auction_snapshot_v2') for row in td_rows
                        if str(row.get('auction_tag') or '') == tag
                        and str(row.get('ts') or row.get('snapshot_cutoff') or '').replace('T', ' ')[:19] <= f'{day} 09:25:10']
            if not selected:
                raise RuntimeError(f'AUCTION_FALLBACK_EMPTY:{tag}:{reason}')
            if tag == '0925':
                _require_snapshot_coverage(str(row.get('symbol') or '').zfill(6) for row in selected)
            rows.extend(selected)
            quality[tag] = {'source': 'td_auction_snapshot_v2', 'reason': reason, 'rows': len(selected)}
    return SnapshotRows(rows, quality), []


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
    source_quality = getattr(snapshots, "source_quality", None)
    degraded = bool(source_quality and any(v["source"] == "td_auction_snapshot_v2" for v in source_quality.values()))
    # A Redis snapshot has no evidence of when its constituent ticks arrived.
    live = live and not source_quality
    capture_semantics = "live_0925_capture" if live else "historical_snapshot_reread"
    cutoff = f"{day}T09:25:10+08:00"
    cutoff_text = cutoff.replace("T", " ")[:19]
    visible_snapshots = [row for row in snapshots
                         if str(row.get("ts") or row.get("snapshot_cutoff") or "").replace("T", " ")[:19] <= cutoff_text]
    final_snapshots = [row for row in visible_snapshots
                       if str(row.get("auction_tag") or "") == "0925"]
    if not final_snapshots:
        raise RuntimeError("AUCTION_FINAL_SNAPSHOT_EMPTY")
    if item == "auction_stock_facts" and not source_quality:
        final_symbols = {str(row.get("symbol") or "").zfill(6) for row in final_snapshots}
        _require_snapshot_coverage(final_symbols)
    facts = build_auction_stock_facts(day, cutoff, final_snapshots, ticks,
                                     anchor_snapshots=visible_snapshots,
                                     now=read_at if live else None,
                                     read_at=read_at if live else None)
    facts["source_quality"] = source_quality or {}
    facts["input_status"] = "degraded" if degraded else "ok"
    if item == "auction_stock_facts":
        stored = store_auction_stock_facts(facts, artifact_root=root,
                                           capture_semantics=capture_semantics)
        return {"dataset": item, "version": stored.get("version"), "rows": len(facts["rows"]),
                "status": "degraded" if degraded else "ok", "source_quality": source_quality or {}, "capture_semantics": capture_semantics}
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
                         "source": row.get("source", "td_auction_snapshot_v2"), "src": row.get("src"),
                         "bid_amount_yuan": row.get("bid_amount_yuan"),
                         "source_rank_amount": row.get("source_rank_amount"), "late_rebuild": not live}
                        for rank, row in enumerate(tagged[:100], 1))
        available_at = datetime.now(TZ).isoformat()
        manifest = catalog.commit(item, day, rows,
            {"schema": "auction_points_v1", "trade_date": day,
             "as_of": cutoff, "available_at": available_at,
             "late_rebuild": not live, "not_a_0925_live_capture": not live,
             "capture_semantics": capture_semantics,
             "quality": {"capture_semantics": capture_semantics, "source_quality": source_quality or {},
                         "input_status": "degraded" if degraded else "ok"},
             "source": "auction_snapshot_inputs"},
            source="auction_snapshot_inputs")
        return {"dataset": item, "version": manifest.get("version"), "rows": len(rows),
                "status": "degraded" if degraded else "ok", "source_quality": source_quality or {}, "capture_semantics": capture_semantics}
    if item == "auction_panorama":
        from engine_core.auction_panorama import build_auction_panorama, store_auction_panorama
        cutoff_dt = datetime.fromisoformat(cutoff)
        source_root = Path(source_catalog_root or os.environ.get("CORE_DATA_ROOT", root)).expanduser()
        source_catalog = ArtifactCatalog(source_root)
        plate_dates = source_catalog.read().get("datasets", {}).get("plate_members", {}).get("dates", {})
        prior_plate_dates = (value for value in plate_dates if value < day)
        # Yesterday's pool must be the immediately preceding trade day: an older day is stale, never a stand-in.
        try:
            from engine_core.trade_day_context import previous_trade_date as context_previous
            previous_day = context_previous(source_root, day)
        except Exception:
            previous_day = None
        plate_members, plate_meta = _pit_rows(source_catalog, "plate_members", prior_plate_dates, cutoff_dt)
        if plate_meta and previous_day and plate_meta["trade_date"] != previous_day:
            # Membership may legitimately be the newest visible older day, but never silently: say how stale it is.
            plate_meta["stale_vs_previous_trade_day"] = previous_day
        from engine_core.data_pipeline.limit_pool_hot_view import read as read_pool
        if previous_day:
            pool, pool_meta = read_pool(previous_day, cutoff_dt.isoformat(), late_rebuild=not live,
                                        late_asof=None if live else datetime.now(cutoff_dt.tzinfo).isoformat())
            if pool_meta["status"] == "MISSING":
                pool = None
        else:
            pool, pool_meta = None, {"status": "MISSING", "reason": "previous_trade_date_unavailable"}
        missing = [name for name, value in (("plate_members", plate_members),
                                             ("yesterday_pool", pool)) if value is None]
        product = build_auction_panorama(facts, plate_members=plate_members,
                                         yesterday_pool=pool, previous_trade_date=None)
        product["late_rebuild"] = not live
        product["capture_semantics"] = capture_semantics
        product["input_quality"] = {"missing_inputs": missing,
                                    "plate_members": plate_meta, "yesterday_pool": pool_meta,
                                    "auction_sources": source_quality or {},
                                    "auction_status": "degraded" if degraded else "ok"}
        pool_degraded = pool_meta.get("status") != "READY"
        if missing or degraded or pool_degraded:
            product["status"] = "PARTIAL"
        stored = store_auction_panorama(product, artifact_root=root)
        return {"dataset": item, "version": stored.get("version"), "rows": 1,
                "status": "degraded" if missing or degraded or pool_degraded else "ok", "missing_inputs": missing,
                "source_quality": source_quality or {},
                "capture_semantics": capture_semantics}
    raise ValueError("unsupported_auction_item:" + item)


@contract_main("fetch_auction_item")
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
              "capture_semantics": "historical_snapshot_reread",
              "source_quality": snapshots.source_quality,
              "status": "degraded" if any(v["source"] == "td_auction_snapshot_v2" for v in snapshots.source_quality.values()) else "ok"}
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
