"""Compare Core's 0925 anchor table with the legacy anchor archive (local files).

Inputs are the ``anchor_table_NNN.json`` written by the shadow script and the
``run_real_auction_anchor_archive.py`` output (which stores only counts), or a
full archive JSON via ``--legacy-archive``.  Only values are compared; nothing
is repaired.  A legacy ``change_pct`` of ``None`` is "missing", not zero.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def compare(core_table: dict, legacy: dict, *, tol: float = 1.5e-4) -> dict:
    counts = Counter()
    worst = []
    amount_equal = amount_total = 0
    for symbol in sorted(set(core_table) | set(legacy)):
        c, l = core_table.get(symbol), legacy.get(symbol)
        if c is None:
            counts["legacy_only_symbol"] += 1
            continue
        if l is None:
            counts["core_only_symbol"] += 1
            continue
        a25, pc = c.get("a25"), c.get("pc")
        core_ratio = (a25 - pc) / pc if a25 and pc else None
        legacy_ratio = l.get("change_pct")
        if core_ratio is None and legacy_ratio is None:
            counts["both_missing"] += 1
        elif core_ratio is None:
            counts["only_legacy_valid"] += 1
        elif legacy_ratio is None:
            counts["only_core_valid"] += 1
        elif abs(core_ratio - legacy_ratio) <= tol:
            counts["both_valid_equal"] += 1
        else:
            counts["both_valid_differ"] += 1
            worst.append((abs(core_ratio - legacy_ratio), symbol, core_ratio, legacy_ratio))
        if c.get("amt") is not None and l.get("amount") is not None:
            amount_total += 1
            amount_equal += abs(float(c["amt"]) - float(l["amount"])) <= 1
    worst.sort(reverse=True)
    return {"counts": dict(counts), "amount_equal": amount_equal, "amount_compared": amount_total,
            "worst_differences": worst[:10]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-table", type=Path, required=True)
    parser.add_argument("--legacy-archive", type=Path, required=True,
                        help="run_real_auction_anchor_archive.py output, or a symbol->row mapping JSON")
    args = parser.parse_args()
    legacy = json.loads(args.legacy_archive.read_text())
    legacy = legacy.get("rows_by_symbol", legacy)
    result = compare(json.loads(args.core_table.read_text()), legacy)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
