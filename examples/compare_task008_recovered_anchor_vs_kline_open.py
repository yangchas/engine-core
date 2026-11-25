"""Compare recovered auction anchors with TD daily opens for the same trade date.

恢复价应与交易日当日的日K open 比较(open-json 来自
run_real_previous_day_stats --previous-trade-date 为该交易日)。 All inputs are
local JSON files. Missing anchor/open values remain missing and are excluded
from equality counts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping


SOURCES = ("a25", "ph0_recovered")


def compare(anchor_table: Mapping, open_by_symbol: Mapping, tol: float = 0.006,
            conflicting_open_symbols=()) -> dict:
    """Compare present anchors to daily opens, grouped by anchor source."""
    conflicting_open_symbols = set(conflicting_open_symbols)
    result = {}
    for source in SOURCES:
        n_with_anchor = n_with_open = n_equal = n_not_equal = n_no_open = n_conflicting_open = 0
        differences = []
        for symbol, row in anchor_table.items():
            if not isinstance(row, Mapping) or row.get("src") != source:
                continue
            a25_milli = row.get("a25")
            if a25_milli is None:
                continue
            n_with_anchor += 1
            if symbol in conflicting_open_symbols:
                n_conflicting_open += 1
                continue
            open_yuan = open_by_symbol.get(symbol)
            if open_yuan is None:
                n_no_open += 1
                continue
            a25_yuan = float(a25_milli) / 1000.0
            open_yuan = float(open_yuan)
            diff = a25_yuan - open_yuan
            n_with_open += 1
            if abs(diff) <= tol:
                n_equal += 1
            else:
                n_not_equal += 1
                differences.append({
                    "symbol": symbol,
                    "a25_yuan": a25_yuan,
                    "open_yuan": open_yuan,
                    "diff": diff,
                })
        differences.sort(key=lambda item: (-abs(item["diff"]), item["symbol"]))
        result[source] = {
            "n_with_anchor": n_with_anchor,
            "n_with_open": n_with_open,
            "n_equal": n_equal,
            "n_not_equal": n_not_equal,
            "n_no_open": n_no_open,
            "n_conflicting_open": n_conflicting_open,
            "equal_rate": n_equal / n_with_open if n_with_open else None,
            "worst_differences": differences[:10],
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor-table", type=Path, required=True)
    parser.add_argument("--open-json", type=Path, nargs="+", required=True)
    parser.add_argument("--tol", type=float, default=0.006)
    parser.add_argument("--expect-trade-date", help="require open-json previous_trade_date to match YYYY-MM-DD")
    args = parser.parse_args()

    anchor_table = json.loads(args.anchor_table.read_text())
    open_by_symbol = {}
    conflicting_open_symbols = set()
    for path in args.open_json:
        payload = json.loads(path.read_text())
        previous_trade_date = payload.get("previous_trade_date") if isinstance(payload, Mapping) else None
        if (args.expect_trade_date is not None and previous_trade_date is not None
                and previous_trade_date != args.expect_trade_date):
            print(
                f"previous_trade_date {previous_trade_date} does not match expected {args.expect_trade_date}"
            )
            return 2
        values = payload.get("open_by_symbol", payload)
        for symbol, value in values.items():
            if symbol not in open_by_symbol:
                open_by_symbol[symbol] = value
            elif open_by_symbol[symbol] != value:
                conflicting_open_symbols.add(symbol)
    result = compare(anchor_table, open_by_symbol, args.tol, conflicting_open_symbols)
    result["conflicting_open_symbols"] = sorted(conflicting_open_symbols)
    print(json.dumps(result,
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
