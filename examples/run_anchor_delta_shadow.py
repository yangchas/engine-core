"""Run the migrated anchor-delta fact against read-only TD rows.

This is a bounded Gate-B evidence tool.  It reuses the existing TD query
shape, normalizes the returned projection rows once, and calls the pure Core
``build_anchor_shadow_evidence`` wheel.  No provider recovery, writer, Rabbit
consumer, notification, or strategy path is imported.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import build_anchor_shadow_evidence, semantic_hash

try:
    from run_real_auction_shadow import query_rows
except ModuleNotFoundError:
    from examples.run_real_auction_shadow import query_rows


def normalize_td_rows(rows: Sequence[Sequence[Any] | Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Map TD ``auction_snapshot_v2`` columns to the anchor fact contract."""

    names = (
        "ts",
        "px_milli",
        "chg_bp",
        "match_amt_yuan",
        "rest_bid_amt_yuan",
        "rest_ask_amt_yuan",
        "limit_state",
        "symbol",
        "trade_date",
        "auction_tag",
    )
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            item = dict(row)
        else:
            if len(row) != len(names):
                raise ValueError("TD row has an unexpected column count")
            item = dict(zip(names, row))
        normalized.append(
            {
                "symbol": item.get("symbol"),
                "tag": item.get("auction_tag") or item.get("tag"),
                "price_milli": item.get("px_milli", item.get("price_milli")),
                "auction_amount_yuan": item.get("match_amt_yuan", item.get("auction_amount_yuan")),
                "bid_amount_yuan": item.get("rest_bid_amt_yuan", item.get("bid_amount_yuan")),
                "ask_amount_yuan": item.get("rest_ask_amt_yuan", item.get("ask_amount_yuan")),
                "ask_amount_present": item.get("rest_ask_amt_yuan") is not None
                if "rest_ask_amt_yuan" in item
                else item.get("ask_amount_present", True),
                "source_record_time": item.get("ts"),
            }
        )
    return tuple(normalized)


def build_shadow_from_td_rows(
    rows: Sequence[Sequence[Any] | Mapping[str, Any]],
    *,
    from_tag: str,
    to_tag: str,
) -> dict[str, Any]:
    normalized = normalize_td_rows(rows)
    facts = build_anchor_shadow_evidence(normalized, from_tag=from_tag, to_tag=to_tag)
    # Provider-native timestamps are evidence, not semantic inputs.  TD may
    # return naive datetimes; keeping them out of this business hash prevents
    # driver timezone representation from changing the fact identity.
    semantic_input = tuple(
        {key: value for key, value in item.items() if key != "source_record_time"}
        for item in normalized
    )
    return {
        "contract_version": "AnchorDeltaFactV1",
        "from_tag": from_tag,
        "to_tag": to_tag,
        "row_count": len(normalized),
        "normalized_input_hash": semantic_hash(semantic_input),
        "fact_hash": semantic_hash(facts),
        "facts": facts,
        "read_only": True,
        "side_effect_boundary": "TD SELECT only; no Redis/TD writer, Rabbit ACK, recovery, notification, strategy or effect",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--from-tag", default="0924")
    parser.add_argument("--to-tag", default="0925")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--td-host", default=os.environ.get("TDENGINE_HOST", "127.0.0.1"))
    parser.add_argument("--td-port", type=int, default=int(os.environ.get("TDENGINE_PORT", "6030")))
    parser.add_argument("--td-user", default=os.environ.get("TDENGINE_USER", "root"))
    parser.add_argument("--td-password", default=os.environ.get("TDENGINE_PASSWORD", "taosdata"))
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    args = parser.parse_args()
    rows = query_rows(
        trade_date=args.trade_date,
        symbol=args.symbol,
        host=args.td_host,
        port=args.td_port,
        user=args.td_user,
        password=args.td_password,
        database=args.td_database,
    )
    result = build_shadow_from_td_rows(rows, from_tag=args.from_tag, to_tag=args.to_tag)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
