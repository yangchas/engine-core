"""Run the migrated fact-only auction strategy on bounded real TD rows.

This command is intentionally a read-only validation harness.  It queries the
existing ``auction_snapshot_v2`` table through the existing TD client path,
converts the three business anchors to immutable ``EngineSnapshot`` values,
and invokes ``AuctionShadowStrategy``.  It does not start a consumer, write
Redis/TD, send a notification, or replace ``engine-next``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (  # noqa: E402
    AuctionShadowStrategy,
    FrozenDataBundle,
    canonical_json,
)

try:  # Script execution resolves sibling examples directly.
    from run_real_auction_shadow import build_snapshots_from_rows, query_rows
except ModuleNotFoundError:  # Pytest/import execution resolves the package.
    from examples.run_real_auction_shadow import build_snapshots_from_rows, query_rows


def _strict_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
    if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
        raise ValueError("symbols must be comma-separated six-digit codes")
    return symbols


def run_real_strategy_shadow(
    *,
    trade_date: str,
    symbols: tuple[str, ...],
    td_config: dict[str, Any],
) -> dict[str, Any]:
    """Read real TD rows and return deterministic fact-only strategy traces."""

    results = []
    for symbol in _strict_symbols(",".join(symbols)):
        rows = query_rows(
            trade_date=trade_date,
            symbol=symbol,
            **td_config,
        )
        snapshots = build_snapshots_from_rows(
            rows,
            trade_date=trade_date,
            symbol=symbol,
        )
        strategy = AuctionShadowStrategy(
            scope_id=symbol,
            start_trigger_id="AUCTION_0920",
            middle_trigger_id="AUCTION_0924",
            end_trigger_id="AUCTION_0925",
            previous_coverage_status=(
                "READY"
                if snapshots["0920"].completeness == "READY"
                and snapshots["0924"].completeness == "READY"
                else "PARTIAL"
            ),
            current_coverage_status=(
                "READY"
                if snapshots["0924"].completeness == "READY"
                and snapshots["0925"].completeness == "READY"
                else "PARTIAL"
            ),
        )
        trace = None
        for index, tag in enumerate(("0920", "0924", "0925")):
            snapshot = snapshots[tag]
            trace = strategy.evaluate(
                snapshot,
                FrozenDataBundle.empty(
                    "real-auction:%s:%s:%s" % (trade_date, symbol, index),
                    snapshot.logical_time_ms,
                ),
            )
        if trace is None:
            raise RuntimeError("auction strategy did not produce a result")
        results.append(
            {
                "symbol": symbol,
                "row_count": len(rows),
                "anchor_completeness": {
                    tag: snapshots[tag].completeness for tag in ("0920", "0924", "0925")
                },
                "strategy_result": trace,
            }
        )
    return {
        "contract_version": "RealAuctionStrategyShadowV1",
        "trade_date": trade_date,
        "symbols": _strict_symbols(",".join(symbols)),
        "read_only": True,
        "side_effect_boundary": (
            "TD SELECT only; no Rabbit consumer/ACK, Redis/TD write, recovery, "
            "notification, or effect"
        ),
        "results": tuple(results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbols", default="600519")
    parser.add_argument("--td-host", default=os.environ.get("TDENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--td-port", type=int, default=int(os.environ.get("TDENGINE_PORT", "6030")))
    parser.add_argument("--td-user", default=os.environ.get("TDENGINE_USER", "root"))
    parser.add_argument("--td-password", default=os.environ.get("TDENGINE_PASSWORD", "taosdata"))
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    args = parser.parse_args()
    result = run_real_strategy_shadow(
        trade_date=args.trade_date,
        symbols=_strict_symbols(args.symbols),
        td_config={
            "host": args.td_host,
            "port": args.td_port,
            "user": args.td_user,
            "password": args.td_password,
            "database": args.td_database,
        },
    )
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
