#!/usr/bin/env python3
"""Late rebuild of auction products from retained TD snapshots (never a live capture)."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src"))

TZ = ZoneInfo("Asia/Shanghai")


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
        snaps = _query(cursor,
            "SELECT ts,symbol,px_milli AS price_milli,chg_bp AS change_bp,"
            "match_amt_yuan AS auction_amount_yuan,trade_date,auction_tag "
            f'FROM auction_snapshot_v2 WHERE trade_date="{compact}" AND ts<="{day} 09:26:00"')
        return snaps, []
    finally:
        connection.close()


def _products(day: str, item: str, root: Path, snapshots, ticks):
    from engine_core.auction_stock_facts import build_auction_stock_facts, store_auction_stock_facts
    cutoff = f"{day}T09:26:00+08:00"
    read_at = datetime.now(TZ).isoformat()
    final_snapshots = [row for row in snapshots if str(row.get("auction_tag") or "") == "0925"]
    facts = build_auction_stock_facts(day, cutoff, final_snapshots, ticks,
                                     now=read_at, read_at=read_at)
    if item == "auction_stock_facts":
        stored = store_auction_stock_facts(facts, artifact_root=root)
        return {"dataset": item, "version": stored.get("version"), "rows": len(facts["rows"])}
    from engine_core.data_pipeline.storage import ArtifactCatalog
    catalog = ArtifactCatalog(root)
    if item == "auction_points":
        rows = []
        for tag in sorted({str(row.get("auction_tag") or "") for row in snapshots}):
            tagged = [row for row in snapshots if str(row.get("auction_tag") or "") == tag]
            tagged.sort(key=lambda row: float(row.get("auction_amount_yuan") or 0), reverse=True)
            rows.extend({"trade_date": day, "tag": tag, "rank_name": "amount", "rank": rank,
                         "symbol": str(row.get("symbol") or "").zfill(6),
                         "auction_amount_yuan": row.get("auction_amount_yuan"),
                         "pct_change": (float(row["change_bp"]) / 100 if row.get("change_bp") is not None else None),
                         "source": "TD auction_snapshot_v2", "late_rebuild": True}
                        for rank, row in enumerate(tagged[:100], 1))
        available_at = datetime.now(TZ).isoformat()
        manifest = catalog.commit(item, day, rows,
            {"schema": "auction_points_late_rebuild_v1", "trade_date": day,
             "as_of": cutoff, "available_at": available_at, "late_rebuild": True,
             "not_a_0925_live_capture": True, "source": "TD auction_snapshot_v2"},
            source="TD auction_snapshot_v2 late rebuild")
        return {"dataset": item, "version": manifest.get("version"), "rows": len(rows)}
    if item == "auction_panorama":
        from engine_core.auction_panorama import build_auction_panorama, store_auction_panorama
        product = build_auction_panorama(facts, plate_members=[], yesterday_pool=[],
                                         previous_trade_date=None)
        product["late_rebuild"] = True
        product["capture_semantics"] = "late TD auction_snapshot_v2 rebuild; not 09:25 live product"
        stored = store_auction_panorama(product, artifact_root=root)
        return {"dataset": item, "version": stored.get("version"), "rows": 1}
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
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
