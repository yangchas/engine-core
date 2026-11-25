#!/usr/bin/env python3
"""Check Core's opening amount aggregation against real mapping and Q2 facts."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import build_opening_plate_amount_summary  # noqa: E402

DEFAULT_MAPPING = Path("/home/exedev/validation/stock_plate_snapshot_20261009.json")
DEFAULT_RESULT = Path("/home/exedev/validation/short-windows-20261012/result_B.json")


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT)
    args = parser.parse_args()
    if not args.mapping.is_file():
        print(f"BLOCKED mapping_missing={args.mapping}")
        return 2
    if not args.result.is_file():
        print(f"BLOCKED result_missing={args.result}")
        return 2

    snapshot = load(args.mapping)
    report = load(args.result)
    event = report["ordered"]["opening_evidence"]["OPENING_0932"]
    facts = event.get("facts_by_symbol")
    mapping = snapshot.get("mapping")
    if not isinstance(facts, dict) or not isinstance(mapping, dict):
        print("BLOCKED facts_by_symbol_or_mapping_missing")
        return 2

    mapped: dict[str, list[str]] = defaultdict(list)
    for symbol, plate in mapping.items():
        symbol, plate = str(symbol).strip(), str(plate).strip()
        if symbol and plate:
            mapped[plate].append(symbol)

    # Window B contains no auction anchor facts, so concentration comparison
    # inputs are explicitly empty/unknown; opening amount aggregation remains usable.
    empty_auction = {plate: [] for plate in mapped}
    summary = build_opening_plate_amount_summary(
        facts,
        mapped_symbols_by_plate=mapped,
        auction_symbols_by_plate=empty_auction,
        auction_top1_amount_ratio_by_plate={},
        selected_plates=list(mapped),
    )

    def available_amount(symbol: str) -> float | None:
        row = facts.get(symbol)
        if not isinstance(row, dict) or row.get("status") != "available":
            return None
        value = row.get("amount_2m_yuan")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(float(value)) else None

    market_amounts = [value for symbol in facts if (value := available_amount(symbol)) is not None]
    market_total = sum(market_amounts)
    independent: dict[str, tuple[int, float]] = {}
    for plate, symbols in mapped.items():
        values = [value for symbol in set(symbols) if (value := available_amount(symbol)) is not None]
        independent[plate] = (len(values), sum(values))

    rows = []
    mismatches: list[str] = []
    flags: list[str] = []
    unavailable = 0
    for row in summary["plates"]:
        plate = row["plate"]
        count, amount = independent[plate]
        core_amount = row["open_window_amount_yuan"]
        if count == 0 and core_amount is None and row["open_window_amount_status"] == "unavailable":
            # Missing observations are not zero-valued observations.
            unavailable += 1
        elif row["open_valid_count"] != count or core_amount is None:
            mismatches.append(
                f"{plate} core_count={row['open_valid_count']} independent_count={count} "
                f"core_amount={core_amount} independent_amount={amount:.6f}"
            )
        elif (
            (amount == 0 and core_amount != 0)
            or (amount != 0 and abs(core_amount - amount) / abs(amount) > 1e-9)
        ):
            mismatches.append(
                f"{plate} core_amount={core_amount:.6f} independent_amount={amount:.6f}"
            )
        ratio = amount / market_total if market_total else None
        if amount < 0:
            flags.append(f"{plate} negative_amount={amount:.6f}")
        if count > len(set(mapped[plate])):
            flags.append(f"{plate} member_count={count} mapped_count={len(set(mapped[plate]))}")
        if ratio is not None and not 0 <= ratio <= 1:
            flags.append(f"{plate} share={ratio:.12g} amount={amount:.6f} market={market_total:.6f}")
        rows.append((amount, plate, count, ratio))

    print(
        f"INPUT trade_date={snapshot.get('trade_date')} mapping_records={snapshot.get('record_count')} "
        f"mapping_plates={len(mapped)} opening_facts={len(facts)} "
        f"available_amount_facts={len(market_amounts)} auction_context=UNKNOWN"
    )
    print("TOP10 plate | members | amount_yuan | share_of_available_opening_amount")
    for amount, plate, count, ratio in sorted(rows, reverse=True)[:10]:
        print(f"{plate} | {count} | {amount:.6f} | {ratio:.9%}" if ratio is not None else f"{plate} | {count} | {amount:.6f} | UNKNOWN")
    print(
        f"TOTAL mapped_plate_amount_yuan={sum(x[0] for x in rows):.6f} "
        f"market_available_amount_yuan={market_total:.6f} multi_label=NO(mapping is symbol->single plate)"
    )
    print(
        f"SUMMARY reconciliations={len(rows)-unavailable} OK={len(rows)-unavailable-len(mismatches)} "
        f"MISMATCH={len(mismatches)} UNAVAILABLE={unavailable} RED={len(flags)}"
    )
    for item in mismatches:
        print(f"MISMATCH {item}")
    for item in flags:
        print(f"RED {item}")
    return 1 if mismatches or flags else 0


if __name__ == "__main__":
    raise SystemExit(main())
