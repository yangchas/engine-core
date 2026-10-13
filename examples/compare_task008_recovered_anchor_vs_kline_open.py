"""Compare recovered auction anchors with previous-day TD kline opens.

All inputs are local JSON files. Missing anchor/open values remain missing and
are excluded from equality counts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping


SOURCES = ("a25", "ph0_recovered")


def compare(anchor_table: Mapping, open_by_symbol: Mapping, tol: float = 0.006) -> dict:
    """Compare present anchors to daily opens, grouped by anchor source."""
    result = {}
    for source in SOURCES:
        n_with_anchor = n_with_open = n_equal = n_not_equal = n_no_open = 0
        differences = []
        for symbol, row in anchor_table.items():
            if not isinstance(row, Mapping) or row.get("src") != source:
                continue
            a25_milli = row.get("a25")
            if a25_milli is None:
                continue
            n_with_anchor += 1
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
            "equal_rate": n_equal / n_with_open if n_with_open else None,
            "worst_differences": differences[:10],
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor-table", type=Path, required=True)
    parser.add_argument("--open-json", type=Path, nargs="+", required=True)
    parser.add_argument("--tol", type=float, default=0.006)
    args = parser.parse_args()

    anchor_table = json.loads(args.anchor_table.read_text())
    open_by_symbol = {}
    for path in args.open_json:
        payload = json.loads(path.read_text())
        values = payload.get("open_by_symbol", payload)
        open_by_symbol.update(values)
    print(json.dumps(compare(anchor_table, open_by_symbol, args.tol),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
