#!/usr/bin/env python3
"""Offline comparison of TD-kline-derived and sealed legacy limit-up pools."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from engine_core.limit_up_pool_derivation import derive_limit_up_pool_from_daily_klines


def compare_pools(
    klines_by_symbol: Mapping[str, Sequence[Sequence[Any]]],
    legacy_rows: Sequence[Mapping[str, Any]],
    trade_date: str,
    *, close_limit_state_by_symbol: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    derivation = derive_limit_up_pool_from_daily_klines(
        klines_by_symbol, trade_date,
        close_limit_state_by_symbol=close_limit_state_by_symbol,
    )
    core_rows = derivation.rows
    core = {row["symbol"]: row for row in core_rows}
    legacy = {str(row["symbol"]): row for row in legacy_rows if row.get("symbol") is not None}
    core_symbols, legacy_symbols = set(core), set(legacy)
    both = core_symbols & legacy_symbols
    core_only = sorted(core_symbols - legacy_symbols)
    legacy_only = sorted(legacy_symbols - core_symbols)
    equal, diff = [], []
    for symbol in sorted(both):
        core_lb, legacy_lb = core[symbol].get("lb_days"), legacy[symbol].get("lb_days")
        if core_lb == legacy_lb:
            equal.append({"symbol": symbol, "core": core_lb, "legacy": legacy_lb})
        else:
            diff.append({"symbol": symbol, "core": core_lb, "legacy": legacy_lb})
    return {
        "trade_date": trade_date,
        "n_core": len(core),
        "n_legacy": len(legacy),
        "seal_evidence_mode": derivation.seal_evidence_mode,
        "core_seal_verified_count": sum(row.get("seal_basis") == "Q2_LS_AT_CLOSE" for row in core_rows),
        "core_no_seal_evidence_count": sum(
            row.get("seal_basis") == "KLINE_CLOSE_AT_LIMIT_NO_SEAL_EVIDENCE" for row in core_rows
        ),
        "excluded_limit_price_without_seal": derivation.notes.get("limit_price_without_seal_symbols", []),
        "both": len(both),
        "core_only": [{"symbol": s, "lb_days": core[s]["lb_days"], "close_pct": core[s]["close_pct"]} for s in core_only],
        "legacy_only": [{"symbol": s, "lb_days": legacy[s].get("lb_days"), "close_pct": legacy[s].get("close_pct")} for s in legacy_only],
        "lb_days_equal": equal,
        "lb_days_diff": diff,
        "recall": len(both) / len(legacy) if legacy else None,
        "precision": len(both) / len(core) if core else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--klines", required=True, type=Path, help='JSON object with a "symbols" mapping')
    parser.add_argument("--legacy-pool", required=True, type=Path, help='Sealed pool JSON artifact with data.rows')
    parser.add_argument("--trade-date", required=True)
    args = parser.parse_args()
    klines = json.loads(args.klines.read_text(encoding="utf-8"))["symbols"]
    legacy_payload = json.loads(args.legacy_pool.read_text(encoding="utf-8"))
    rows = legacy_payload["data"]["rows"]
    print(json.dumps(compare_pools(klines, rows, args.trade_date), ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
