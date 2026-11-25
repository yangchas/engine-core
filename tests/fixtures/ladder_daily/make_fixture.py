#!/usr/bin/env python3
"""Extract the REAL fixture for the ladder_daily tests/checks (C577) from ~/core-data (read-only).

Usage: python make_fixture.py   -> writes real_ladder_inputs.json.gz next to this file (see PROVENANCE.md)
"""
import gzip
import json
from pathlib import Path

CORE = Path("/home/exedev/core-data")
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
        "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27"]


def best(catalog, dataset, day):
    entries = catalog["datasets"][dataset]["dates"].get(day) or []
    rank = {"FORMAL": 3, "PROVISIONAL": 2, "DEGRADED": 1}
    chosen = None
    for e in entries:
        if chosen is None or rank.get(e.get("business_status"), 2) >= rank.get(chosen.get("business_status"), 2):
            chosen = e
    return chosen


def rows_of(manifest_path):
    path = Path(manifest_path).with_name("rows.jsonl.gz")
    return [json.loads(line) for line in gzip.open(path, "rt", encoding="utf-8") if line.strip()]


def main():
    catalog = json.loads((CORE / "catalog.json").read_text())
    kline, pools = {}, {}
    for day in DAYS:
        e = best(catalog, "daily_kline", day)
        m = json.loads(Path(e["manifest"]).read_text())
        kline[day] = {"version": m["version"], "business_status": m.get("business_status") or e.get("business_status"),
                      "source": m.get("source"), "available_at": m.get("available_at"), "rows": rows_of(e["manifest"])}
        e = best(catalog, "limit_up_pool", day)
        if e:
            m = json.loads(Path(e["manifest"]).read_text())
            pools[day] = {"version": m["version"], "business_status": m.get("business_status"),
                          "source": m.get("source"), "available_at": m.get("available_at"), "rows": rows_of(e["manifest"])}
    hot = {r["symbol"] for p in pools.values() for r in p["rows"]}
    for part in kline.values():
        hot |= {r["symbol"] for r in part["rows"] if (r.get("pct_chg") or 0) >= 9.4}
    universe = sorted(r["symbol"] for r in kline[DAYS[-1]]["rows"])
    hot |= set(universe[::40]) | {"002724", "600838", "000001", "600519", "300750", "688981"}
    for part in list(kline.values()):
        part["rows"] = [r for r in part["rows"] if r["symbol"] in hot]
    out = {"days": DAYS, "kline": kline, "pools": pools, "symbols": sorted(hot)}
    target = Path(__file__).with_name("real_ladder_inputs.json.gz")
    with gzip.open(target, "wt", encoding="utf-8") as h:
        json.dump(out, h, ensure_ascii=False, sort_keys=True)
    print(target, len(hot), "symbols", sum(len(p["rows"]) for p in kline.values()), "kline rows")


if __name__ == "__main__":
    main()
