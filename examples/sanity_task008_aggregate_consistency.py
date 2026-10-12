#!/usr/bin/env python3
"""Read-only consistency checks for the two frozen TASK-008 result JSONs."""

import json
import sys
from pathlib import Path


WINDOWS = (
    ("A_0924_0925", Path("/home/exedev/validation/short-windows-20261012/result_A_warm.json")),
    ("B_0932", Path("/home/exedev/validation/short-windows-20261012/result_B.json")),
)


def close(left, right):
    return left == right if isinstance(left, int) and isinstance(right, int) else (
        left is not None and right is not None and
        abs(float(left) - float(right)) <= 1e-9 * max(1.0, abs(float(right)))
    )


def main():
    selected = WINDOWS
    if len(sys.argv) > 1:
        requested = Path(sys.argv[1]).resolve()
        selected = tuple((name, path) for name, path in WINDOWS if path.resolve() == requested)
        if not selected:
            raise SystemExit("input must be one of the two configured result JSON files")
    for label, path in selected:
        total = ok = mismatch = red = 0
        data = json.loads(path.read_text())
        ordered = data["ordered"]
        print("WINDOW", label)
        if label.startswith("A_"):
            anchor = ordered["anchor_evidence"]["0925"]
            summary = anchor["q2_auction_summary"]
            facts = anchor["auction_anchor_facts_by_symbol"]
            print("REPORTED q2_auction_summary", {
                "input_symbol_count": summary["input_symbol_count"],
                "candidate_count": summary["candidate_count"],
                "candidate_membership_unknown_count": summary["candidate_membership_unknown_count"],
                "metrics": summary["metrics"],
                "metric_unknown_counts": summary["metric_unknown_counts"],
                "per_symbol_raw_q2_facts": len(anchor["facts_by_symbol"]),
            })
            checks = (
                ("candidate_count", summary["candidate_count"], len(summary["candidate_symbols"])),
                ("candidate_membership_unknown_count", summary["candidate_membership_unknown_count"], len(summary["candidate_membership_unknown_symbols"])),
            )
            for name, reported, recomputed in checks:
                passed = close(reported, recomputed)
                total += 1; ok += passed; mismatch += not passed
                print(f"{'OK' if passed else 'MISMATCH'} {name}: reported={reported} recomputed={recomputed} (candidate-list consistency only)")
            # Q2 source fields (am/br/ar/a25/pc/ls) are absent; do not treat
            # the derived candidate list or anchor prices as those inputs.
            m = summary["metrics"]
            for name, value in m.items():
                invalid = value < 0 or (name.endswith("count") and value > summary["input_symbol_count"])
                red += invalid
                print(f"{'RED' if invalid else 'PLAUSIBLE'} q2.metrics.{name}: {value}")
            if len(facts) != summary["input_symbol_count"]:
                red += 1
                print(f"RED anchor_fact_symbol_count: {len(facts)} vs input_symbol_count={summary['input_symbol_count']}")
            else:
                print(f"PLAUSIBLE anchor_fact_symbol_count: {len(facts)} (does not supply raw Q2 aggregate inputs)")
            print("UNRECOMPUTABLE q2 auction_amount/status/limit counts: raw per-symbol Q2 fields are absent")
        else:
            tag = ordered["opening_evidence"]["OPENING_0932"]
            facts = tag["facts_by_symbol"]
            expected = tag["expected_symbol_count"]
            available = [row for row in facts.values() if row.get("status") == "available"]
            amounts = [row["amount_2m_yuan"] for row in available if isinstance(row.get("amount_2m_yuan"), (int, float))]
            states = [row["limit_state"] for row in available if isinstance(row.get("limit_state"), int)]
            changes = [row["change_pct"] for row in available if isinstance(row.get("change_pct"), (int, float))]
            amount_sum = sum(amounts)
            amount_summary = tag["amount_2m_summary"]
            limit_summary = tag["limit_state_summary"]
            cross = tag["cross_section_facts"]
            trans = tag["opening_transition_summary"]["facts"]
            reported = {
                "coverage": tag["coverage"], "observed_symbol_count": tag["observed_symbol_count"],
                "missing_symbol_count": tag["missing_symbol_count"], "amount_2m_summary": amount_summary,
                "limit_state_summary": limit_summary,
                "cross_section_facts": {"coverage": cross["coverage"], "observed_count": cross["observed_count"],
                                         "missing_count": cross["missing_count"], "market_breadth": cross["market_breadth"]},
                "opening_transition_summary.facts": {k: trans[k] for k in (
                    "auction_anchor_price_available_count", "auction_change_available_count",
                    "transition_comparable_count", "transition_unavailable_count", "sign_state_counts", "delta_state_counts")},
                "fact_status_counts": tag["fact_status_counts"],
                "opening_fact_field_status_counts": tag["opening_fact_field_status_counts"],
            }
            # Keep printed structures compact while preserving every aggregate value.
            for name, value in reported.items():
                print("REPORTED", name, value)
            observed = len(facts)
            missing = expected - observed
            state_counts = {"down_count": states.count(-1), "normal_count": states.count(0), "up_count": states.count(1)}
            breadth = {"up_count": sum(v > 0 for v in changes), "flat_count": sum(v == 0 for v in changes),
                       "down_count": sum(v < 0 for v in changes), "unknown_count": len(facts) - len(changes)}
            comparisons = (
                ("observed_symbol_count", tag["observed_symbol_count"], observed),
                ("missing_symbol_count", tag["missing_symbol_count"], missing),
                ("coverage", tag["coverage"], observed / expected if expected else None),
                ("amount_2m_summary.amount_2m_yuan_present_count", amount_summary["amount_2m_yuan_present_count"], len(amounts)),
                ("amount_2m_summary.amount_2m_yuan_missing_count", amount_summary["amount_2m_yuan_missing_count"], amount_summary["amount_2m_yuan_total_count"] - len(amounts)),
                ("amount_2m_summary.amount_2m_yuan_total_count", amount_summary["amount_2m_yuan_total_count"], len(available)),
                ("amount_2m_summary.amount_2m_yuan_sum", amount_summary["amount_2m_yuan_sum"], amount_sum),
                ("amount_2m_summary.amount_2m_yuan_coverage", amount_summary["amount_2m_yuan_coverage"], len(amounts) / amount_summary["amount_2m_yuan_total_count"] if amount_summary["amount_2m_yuan_total_count"] else None),
                ("limit_state_summary.limit_state_counts", limit_summary["limit_state_counts"], state_counts),
                ("limit_state_summary.limit_state_valid_count", limit_summary["limit_state_valid_count"], len(states)),
                ("cross_section_facts.market_breadth", cross["market_breadth"], breadth),
                ("fact_status_counts.READY", tag["fact_status_counts"].get("READY", 0), len(available)),
            )
            for name, reported_value, recomputed in comparisons:
                passed = all(close(reported_value.get(k), recomputed.get(k)) for k in recomputed) if isinstance(recomputed, dict) else close(reported_value, recomputed)
                total += 1; ok += passed; mismatch += not passed
                print(f"{'OK' if passed else 'MISMATCH'} {name}: reported={reported_value} recomputed={recomputed}")
            field_status = tag["opening_fact_field_status_by_symbol"]
            for field_name, statuses in tag["opening_fact_field_status_counts"].items():
                recomputed_count = sum(row.get(field_name) == "AVAILABLE" for row in field_status.values())
                passed = close(statuses.get("AVAILABLE", 0), recomputed_count)
                total += 1; ok += passed; mismatch += not passed
                print(f"{'OK' if passed else 'MISMATCH'} opening_fact_field_status_counts.{field_name}.AVAILABLE: reported={statuses.get('AVAILABLE', 0)} recomputed={recomputed_count}")
            transition_rows = tag["opening_transition_summary"]["facts"]["facts_by_symbol"]
            transition_unavailable = sum(row.get("status") == "unavailable" for row in transition_rows.values())
            transition_reported = tag["opening_transition_summary"]["facts"]["transition_unavailable_count"]
            passed = close(transition_reported, transition_unavailable)
            total += 1; ok += passed; mismatch += not passed
            print(f"{'OK' if passed else 'MISMATCH'} opening_transition_summary.transition_unavailable_count: reported={transition_reported} recomputed={transition_unavailable}")
            top = sorted((float(v) for v in amounts if v >= 0), reverse=True)
            top1 = top[0] / amount_sum if amount_sum else None
            top3 = sum(top[:3]) / amount_sum if amount_sum else None
            bad_top = top1 is None or top3 is None or not (0 <= top1 <= 1 and top1 <= top3 <= 1)
            red += bad_top
            print(f"{'RED' if bad_top else 'PLAUSIBLE'} computed Top1/Top3 concentration: {top1}/{top3} (from available amount_2m_yuan)")
            for name, value, predicate in (
                ("amount_2m_yuan_sum", amount_summary["amount_2m_yuan_sum"], lambda x: x >= 0),
                ("symbol_coverage", amount_summary["symbol_coverage"], lambda x: 0 <= x <= 1),
                ("opening_transition_summary.Top1/Top3 unavailable", None, lambda x: True),
            ):
                if value is None:
                    continue
                invalid = not predicate(value)
                red += invalid
                print(f"{'RED' if invalid else 'PLAUSIBLE'} {name}: {value}")
        print(f"SUMMARY: reconciliation_items={total} OK={ok} MISMATCH={mismatch} RED={red}")


if __name__ == "__main__":
    main()
