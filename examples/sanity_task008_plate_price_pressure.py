#!/usr/bin/env python3
"""Sanity-check plate opening price breadth and 0924->0925 auction pressure.

Core's plate summaries are recomputed independently from raw TD auction rows,
the frozen plate mapping and the 0932 opening facts using plain arithmetic, then
the per-symbol pressure is cross-checked against Q2Frame state at the TD times.
Local files only; no Redis/TD/Rabbit.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    build_anchor_shadow_evidence,
    build_opening_plate_auction_pressure_summary,
    build_opening_plate_price_summary,
)
from engine_core.q2 import normalize_q2  # noqa: E402

V = Path("/home/exedev/validation")
TD = V / "task010-auction-anchor-shadow-20261011T142800+0800/td_auction_rows_20261009.jsonl"
PLATES = V / "task010-plate-aggregate-20261011T180806+0800/stock_plate_snapshot_20261009.json"
OPEN = V / "short-windows-20261012/result_B.json"
Q2E = V / "short-windows-20261012/window_E_0924_0927_warm.jsonl"
DATE = "2026-10-09"


def td_by_tag():
    out = defaultdict(dict)
    for line in TD.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["auction_tag"]][r["symbol"]] = r
    return out


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    td = td_by_tag()
    mapping = json.loads(PLATES.read_text(encoding="utf-8"))["mapping"]
    mapped = defaultdict(list)
    for symbol, plate in mapping.items():
        mapped[str(plate).strip()].append(str(symbol).strip())
    red = []

    # ---- price breadth ----
    facts = json.loads(OPEN.read_text(encoding="utf-8"))["ordered"]["opening_evidence"]["OPENING_0932"]["facts_by_symbol"]
    auction_valid = {s for s, r in td["0925"].items() if r.get("chg_bp") is not None and (r.get("px_milli") or 0) > 0}
    auction_by_plate = {p: [s for s in m if s in auction_valid] for p, m in mapped.items()}
    summary = build_opening_plate_price_summary(
        facts, mapped_symbols_by_plate=mapped, auction_symbols_by_plate=auction_by_plate)
    bad = 0
    checked = 0
    for row in summary["plates"]:
        plate = row["plate"]
        cohort = [s for s in auction_by_plate[plate] if s in facts and facts[s].get("status") == "available"]
        ch = [facts[s]["change_pct"] for s in cohort if isinstance(facts[s].get("change_pct"), (int, float))]
        if not ch:
            continue
        checked += 1
        up = sum(v > 0 for v in ch)
        ok = (
            row["comparison_valid_count"] == len(cohort)
            and row["open_up_count"] == up
            and abs(row["open_positive_ratio"] - up / len(ch)) < 1e-12
            and abs(row["open_median_change_pct"] - statistics.median(ch)) < 1e-9
        )
        if not ok:
            bad += 1
            red.append("price mismatch %s" % plate)
        if not 0 <= row["open_positive_ratio"] <= 1:
            red.append("price ratio out of range %s" % plate)
    print("PRICE plates=%d checked=%d mismatches=%d auction_cohort=%d open_facts=%d"
          % (len(summary["plates"]), checked, bad, len(auction_valid), len(facts)))

    # ---- auction pressure ----
    evidence = build_anchor_shadow_evidence(
        [{**r, "tag": r["auction_tag"], "price_milli": r["px_milli"],
          "auction_amount_yuan": r["match_amt_yuan"], "bid_amount_yuan": r["rest_bid_amt_yuan"],
          "ask_amount_yuan": r["rest_ask_amt_yuan"]}
         for tag in ("0924", "0925") for r in td[tag].values()],
        from_tag="0924", to_tag="0925")
    by_symbol = {e["symbol"]: e for e in evidence}
    pressure = build_opening_plate_auction_pressure_summary(
        by_symbol, mapped_symbols_by_plate=mapped, trade_date=DATE)

    def independent(s):
        a, b = td["0924"].get(s), td["0925"].get(s)
        if not a or not b:
            return None
        vals = [a.get("match_amt_yuan"), b.get("match_amt_yuan"), a.get("px_milli"), b.get("px_milli"),
                a.get("rest_bid_amt_yuan"), a.get("rest_ask_amt_yuan"), b.get("rest_bid_amt_yuan"), b.get("rest_ask_amt_yuan")]
        if any(v is None for v in vals) or b["px_milli"] <= 0 or a["px_milli"] <= 0:
            return None
        d_amt = b["match_amt_yuan"] - a["match_amt_yuan"]
        d_px = b["px_milli"] - a["px_milli"]
        d_p = (b["rest_bid_amt_yuan"] - b["rest_ask_amt_yuan"]) - (a["rest_bid_amt_yuan"] - a["rest_ask_amt_yuan"])
        if d_amt <= 0:
            return 0.0
        if d_px != 0:
            return d_amt if d_px > 0 else -d_amt
        return d_amt if d_p > 0 else -d_amt if d_p < 0 else 0.0

    pbad = 0
    pchecked = 0
    for row in pressure["plates"]:
        vals = [independent(s) for s in mapped[row["plate"]]]
        vals = [v for v in vals if v is not None]
        pchecked += 1
        total = sum(vals)
        core = row["auction_pressure_yuan"]
        if (core is None and vals) or (core is not None and abs(core - total) > 1e-6):
            pbad += 1
            red.append("pressure mismatch %s core=%s indep=%s" % (row["plate"], core, total))
        if row["pressure_usable_count"] != len(vals):
            pbad += 1
            red.append("pressure usable count %s" % row["plate"])
    print("PRESSURE plates=%d mismatches=%d symbols_with_delta=%d" % (pchecked, pbad, sum(
        1 for e in evidence if e["status"] in ("resolved", "balanced", "unresolved"))))
    directions = defaultdict(int)
    for e in evidence:
        directions[e["direction"]] += 1
    print("DIRECTIONS", dict(directions))
    top = sorted((r for r in pressure["plates"] if r["auction_pressure_yuan"] is not None),
                 key=lambda r: -r["auction_pressure_yuan"])[:5]
    for r in top:
        print("TOP", r["plate"], r["pressure_usable_count"], "/", r["pressure_total_count"], "%.0f" % r["auction_pressure_yuan"])

    # ---- Q2 cross-check at TD times ----
    ts = {t: max(r["ts"] for r in td[t].values()) for t in ("0924", "0925")}
    state, snaps = {}, {}
    frames = [json.loads(l) for l in Q2E.read_text(encoding="utf-8").splitlines() if l.strip()]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    def ms(text):
        return int(datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    # TD 0924 is the 09:24:10 snapshot: take Q2 state as of the last frame at or
    # before it; TD 0925 is the final snapshot: take state after 09:25:59.
    cut = {"0924": ms(DATE + "T09:24:10"), "0925": ms(DATE + "T09:25:59")}
    for fr in frames:
        if "0924" not in snaps and fr["logical_ts_ms"] > cut["0924"]:
            snaps["0924"] = {s: dict(d) for s, d in state.items()}
        for u in fr["q2_updates"]:
            state.setdefault(u["symbol"], {}).update(u)
        if "0925" not in snaps and fr["logical_ts_ms"] > cut["0925"]:
            snaps["0925"] = {s: dict(d) for s, d in state.items()}
    cmp = defaultdict(lambda: [0, 0])
    for tag in ("0924", "0925"):
        snap = snaps.get(tag)
        if snap is None:
            continue
        for s, r in td[tag].items():
            q = snap.get(s)
            if q is None:
                continue
            for name, tdv, qv in (("amount", r.get("match_amt_yuan"), q.get("am")),
                                  ("bid", r.get("rest_bid_amt_yuan"), q.get("br")),
                                  ("ask", r.get("rest_ask_amt_yuan"), q.get("ar"))):
                if tdv is None or qv in (None, ""):
                    continue
                cmp[tag + "_" + name][1] += 1
                if abs(float(tdv) - float(qv)) <= 1:
                    cmp[tag + "_" + name][0] += 1
    for k, (eq, n) in sorted(cmp.items()):
        print("Q2_VS_TD %s equal=%d/%d" % (k, eq, n))
    print("RED", len(red))
    for item in red[:20]:
        print("RED", item)
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
