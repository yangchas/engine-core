#!/usr/bin/env python3
"""Three-way comparison of the D-1 limit-up pool: Kaipan vs WenCai vs TD-derived (C580 acceptance B).

Inputs are recorded REAL responses (captured once through the production providers; see
``--evidence-dir``) for Kaipan and WenCai, and a LIVE read-only SELECT on ``market_data1.daily_kline`` for the TD
side.  Nothing is written except the optional ``--out`` JSON.  Every "only in one source" member is classified with
independent facts (name / listing history / previous-session bar), never with the pool itself.

Usage: python examples/compare_limit_pool_sources.py --dates 2026-10-23,2026-10-26 [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core.data_pipeline import limit_pool as lp  # noqa: E402
from engine_core.data_pipeline.providers.limit_pool_sources import read_td_bars, universe_names  # noqa: E402

DEFAULT_EVIDENCE = Path("/home/exedev/fw/evidence/C580")
DEFAULT_CORE_DATA = Path("~/core-data")


def load_kaipan(evidence: Path, day: str) -> lp.Partition:
    doc = json.loads((evidence / f"kaipan_pool_{day.replace('-', '')}.json").read_text(encoding="utf-8"))
    pages = {int(level): page for level, page in doc["pages"].items()}
    return lp.build_kaipan(day, pages)


def load_wencai(evidence: Path, day: str) -> lp.Partition:
    doc = json.loads((evidence / f"wencai_pool_{day.replace('-', '')}.json").read_text(encoding="utf-8"))
    response = doc["response"]
    return lp.build_wencai(day, response["data"], reported_total=response.get("row_count"))


def classify(symbol: str, day: str, bars: dict[str, list[tuple]], names: dict[str, str] | None,
             sessions: list[str]) -> str:
    """Why is ``symbol`` in one pool and not in the other?  Uses TD bars + universe names only."""
    name = (names or {}).get(symbol)
    if name and lp.is_st_name(name):
        return "ST"
    series = {bar[0]: bar for bar in bars.get(symbol, [])}
    if not series:
        # the legacy TD table has no row for this code in the whole window: newly listed after its universe
        # snapshot, or a code outside that universe (e.g. Beijing 92xxxx)
        return "absent_from_TD_table_in_universe" if name else "absent_from_TD_table_not_in_universe"
    if day not in series:
        return "no_TD_bar_on_date"
    earlier = [d for d in sessions if d < day]
    first_seen = min(series) if series else day
    if earlier and first_seen > earlier[0] and len([d for d in earlier if d in series]) < len(earlier) // 2:
        return "new_listing_or_few_bars"
    if earlier and earlier[-1] not in series:
        return "resumed_from_suspension"
    bar = series[day]
    pct, close, high = bar[3], bar[1], bar[2]
    limit = lp.limit_pct(symbol)
    if pct is not None and abs(pct - limit) <= 0.6 and close != high:
        return "rounding_boundary_close_not_high"
    if pct is not None and limit - 0.6 > pct >= limit - 1.0:
        return "rounding_boundary_pct"
    return "other"


def compare_day(day: str, evidence: Path, core_data: Path) -> dict:
    kaipan, wencai = load_kaipan(evidence, day), load_wencai(evidence, day)
    bars = read_td_bars(day)
    sessions = lp.infer_sessions(bars)
    names, st_source = universe_names(core_data, day)
    td = lp.build_td_derived(day, bars, names=names, sessions=sessions, st_source=st_source)
    td_with_st = lp.build_td_derived(day, bars, names=None, sessions=sessions)
    sets = {"kaipan": {r["symbol"]: r for r in kaipan.rows}, "wencai": {r["symbol"]: r for r in wencai.rows},
            "td_derived": {r["symbol"]: r for r in td.rows},
            "td_no_st_filter": {r["symbol"]: r for r in td_with_st.rows}}
    report = {"date": day, "sizes": {k: len(v) for k, v in sets.items()},
              "td_derived_without_st_filter": len(td_with_st.rows), "td_st_filter": td.notes.get("st_filter"),
              "st_source": st_source, "sessions_used": len(sessions), "pairs": {}}
    for a, b in (("kaipan", "wencai"), ("kaipan", "td_derived"), ("wencai", "td_derived"),
                 ("kaipan", "td_no_st_filter")):
        sa, sb = set(sets[a]), set(sets[b])
        common = sorted(sa & sb)
        lb_diff = [{"symbol": s, a: sets[a][s]["lb_days"], b: sets[b][s]["lb_days"]} for s in common
                   if sets[a][s]["lb_days"] != sets[b][s]["lb_days"]]
        only_a, only_b = sorted(sa - sb), sorted(sb - sa)

        def describe(symbols: list[str]) -> dict:
            groups: dict[str, list[str]] = {}
            for s in symbols:
                groups.setdefault(classify(s, day, bars, names, sessions), []).append(s)
            return {k: {"count": len(v), "examples": [f"{s} {(names or {}).get(s, '?')}" for s in v[:4]]}
                    for k, v in sorted(groups.items())}

        report["pairs"][f"{a}_vs_{b}"] = {
            "common": len(common), f"only_{a}": len(only_a), f"only_{b}": len(only_b),
            f"only_{a}_why": describe(only_a), f"only_{b}_why": describe(only_b),
            "lb_days_differs": len(lb_diff), "lb_days_diff_examples": lb_diff[:6]}
    # price/pct evidence of the Kaipan columns against the independent TD bars
    close_mismatch = pct_mismatch = checked = 0
    for symbol, row in sets["kaipan"].items():
        series = {bar[0]: bar for bar in bars.get(symbol, [])}
        bar = series.get(day)
        if bar is None:
            continue
        checked += 1
        close_mismatch += abs(float(row["price"]) - float(bar[1])) > 0.006
        pct_mismatch += abs(float(row["pct_chg"]) - float(bar[3])) > 0.06
    report["kaipan_price_pct_vs_td"] = {"checked": checked, "price_diff_gt_0.006": close_mismatch,
                                        "pct_diff_gt_0.06": pct_mismatch}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dates", required=True)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--core-data", type=Path, default=DEFAULT_CORE_DATA)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not os.environ.get("TD_CONFIG"):
        default = Path("/home/exedev/validation/operator-runs/taos-cfg")
        if default.exists():
            os.environ["TD_CONFIG"] = str(default)
    reports = [compare_day(day.strip(), args.evidence_dir, args.core_data.expanduser())
               for day in args.dates.split(",") if day.strip()]
    text = json.dumps(reports, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
