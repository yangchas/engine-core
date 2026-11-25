#!/usr/bin/env python3
"""Reconcile the sealed previous-limit pool against 2026-10-09 09:25 facts."""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core.calendar import build_calendar_snapshot  # noqa: E402
from engine_core.contracts import DataRequest, DataResult  # noqa: E402
from engine_core.data import DataContext, ProviderResult  # noqa: E402
from engine_core.previous_day_limit_pool import PreviousDayLimitPoolFunction  # noqa: E402
from engine_core.previous_day_limit_feedback import build_previous_day_limit_feedback  # noqa: E402
from engine_core.previous_day_limit_structure import build_previous_day_limit_structure  # noqa: E402

POOL_PATH = Path("/home/exedev/validation/limit-pool-input-20261013/pool_2026-10-09_prev_2026-10-08.json")
ANCHOR_PATH = Path("/home/exedev/validation/short-windows-20261012/result_A_warm.json")


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


class MemoryPreviousDayLimitPoolProvider:
    def __init__(self, payload: Mapping[str, Any], *, observed_at_ms: int) -> None:
        self._payload = payload
        self._observed_at_ms = observed_at_ms

    def fetch(self, request: DataRequest, *, previous_trade_date: str) -> ProviderResult:
        if previous_trade_date != self._payload.get("previous_trade_date"):
            raise ValueError("calendar-derived previous trade date mismatch")
        return ProviderResult(
            raw_data=self._payload,
            source_id="sealed_previous_day_limit_pool_json",
            source_schema="PreviousDayLimitPoolV1",
            effective_at_ms=None,
            available_at_ms=None,
            observed_at_ms=self._observed_at_ms,
            availability_status="OBSERVED",
            evidence_ref=str(POOL_PATH),
        )


def execute_previous_pool(raw: Mapping[str, Any]) -> DataResult:
    source_data = raw["data"]
    rows = tuple(source_data["rows"])
    previous_trade_date = str(raw["actual_trade_date"])
    payload = dict(source_data)
    payload.update(
        previous_trade_date=previous_trade_date,
        rows=rows,
        by_symbol={str(row["symbol"]): row for row in rows},
        row_count=len(rows),
    )
    cutoff_ms = int(
        datetime(2026, 10, 9, 9, 25, 6, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        * 1000
    )
    calendar = build_calendar_snapshot(
        ("2026-10-08", "2026-10-09"),
        version="sealed-two-session-sanity-v1",
        declared_valid_from="2026-10-08",
        declared_valid_to="2026-10-09",
        source_guard_valid_from="2026-10-08",
        source_guard_valid_to="2026-10-09",
        observed_at_ms=int(raw["observed_at_ms"]),
        evidence_ref="injected sanity calendar: 2026-10-08 and 2026-10-09",
    )
    request = DataRequest(
        request_id="sanity-task008-prev-limit-feedback",
        function_id="previous_day_limit_pool",
        trade_date="2026-10-09",
        effective_as_of_ms=cutoff_ms,
        knowledge_as_of_ms=cutoff_ms,
        temporal_mode="HISTORICAL",
        allow_unverified_previous_day_availability=True,
    )
    context = DataContext(
        evaluation_id="sanity-task008-prev-limit-feedback",
        phase="historical-sanity",
        observed_at_ms=int(raw["observed_at_ms"]),
        temporal_mode="HISTORICAL",
    )
    provider = MemoryPreviousDayLimitPoolProvider(
        payload, observed_at_ms=int(raw["observed_at_ms"])
    )
    return PreviousDayLimitPoolFunction(provider, calendar).execute(context, request)


def main() -> int:
    pool_artifact = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    anchor_artifact = json.loads(ANCHOR_PATH.read_text(encoding="utf-8"))
    previous = execute_previous_pool(pool_artifact)
    rows = previous.data.get("rows", ())
    anchor = anchor_artifact["ordered"]["anchor_evidence"]["0925"]
    facts = anchor["auction_anchor_facts_by_symbol"]

    current_rows = []
    for symbol, fact in facts.items():
        row = {
            "symbol": str(fact.get("symbol") or symbol),
            "trade_date": fact.get("trade_date"),
            "tag": fact.get("tag"),
        }
        for field in ("change_pct", "auction_amount_yuan"):
            if field in fact:
                row[field] = fact[field]
        if "source_time_ms" in fact:
            row["source_record_time_ms"] = fact["source_time_ms"]
        current_rows.append(row)

    structure = build_previous_day_limit_structure(previous)
    feedback = build_previous_day_limit_feedback(
        previous, current_rows, current_trade_date="2026-10-09"
    )
    by_symbol = {row["symbol"]: row for row in rows if isinstance(row, Mapping)}
    dist = Counter(str(row["lb_days"]) for row in rows if isinstance(row, Mapping))
    matched_facts = {symbol: facts.get(symbol) for symbol in by_symbol}
    price_count = sum(
        isinstance(fact, Mapping)
        and fact.get("status") == "AVAILABLE"
        and finite_number(fact.get("price_milli"))
        for fact in matched_facts.values()
    )

    explicit_changes = [
        row["change_pct"] for row in current_rows if finite_number(row.get("change_pct"))
    ]
    independent_returns = (
        sum(v > 0 for v in explicit_changes),
        sum(v < 0 for v in explicit_changes),
        sum(v == 0 for v in explicit_changes),
    )
    function_returns = (feedback.up_count, feedback.down_count, feedback.flat_count)
    output_amounts = [
        record.get("auction_amount_yuan")
        for record in feedback.records
        if finite_number(record.get("auction_amount_yuan"))
    ]
    explicit_amounts = [row["auction_amount_yuan"] for row in current_rows
                        if finite_number(row.get("auction_amount_yuan"))]
    independent_amount = sum(explicit_amounts) if explicit_amounts else None
    function_amount = sum(output_amounts) if output_amounts else None

    checks = [
        ("pool_rows", len(rows), structure.row_count, structure.row_count is not None),
        ("lb_days_distribution", dict(sorted(dist.items())), dict(structure.board_height_distribution), structure.row_count is not None),
        ("change_pct_up_down_flat", independent_returns, function_returns, feedback.valid_return_count is not None),
        ("auction_amount_yuan_sum", independent_amount, function_amount, bool(explicit_amounts)),
    ]
    mismatch_count = sum(comparable and left != right for _, left, right, comparable in checks)
    unavailable_count = sum(not comparable for _, _, _, comparable in checks)
    zero_turnover = [row for row in rows if row.get("turnover_yuan") == 0]
    same_close_pct = len({row.get("close_pct") for row in rows}) <= 1
    all_seal_empty = all(not row.get("seal_time") for row in rows)
    missing_price = len(rows) - price_count
    annotations = [
        "close_pct_all_same=%s (%s rows; 10.0 vs 30/68/92 board limit conventions needs source clarification)"
        % ("YES" if same_close_pct else "NO", len(rows)),
        "seal_time_all_empty=%s (%s rows)" % ("YES" if all_seal_empty else "NO", len(rows)),
        "turnover_yuan_zero=%s rows (treat as missing, not zero): %s"
        % (len(zero_turnover), ", ".join("%s/%s" % (r.get("symbol"), r.get("name")) for r in zero_turnover) or "none"),
        "pool_symbols_missing_0925_price=%s/%s" % (missing_price, len(rows)),
        "0925_change_pct_present=%s/%s; absent values remain missing" % (len(explicit_changes), len(current_rows)),
        "0925_auction_amount_yuan_present=%s/%s; sum=%s" % (
            len(explicit_amounts), len(current_rows), "%.2f" % independent_amount if independent_amount is not None else "UNAVAILABLE"
        ),
    ]

    print("SUMMARY checks=%d OK=%d MISMATCH=%d UNAVAILABLE=%d annotations=%d" % (
        len(checks), len(checks) - mismatch_count - unavailable_count, mismatch_count,
        unavailable_count, len(annotations)
    ))
    temporal_markers = sorted({
        note
        for item in previous.provenance
        for note in item.notes
        if note == "available_at_unknown_previous_day_data"
    })
    print("POOL status=%s rows=%s markers=%s" % (
        previous.status.value, len(rows), temporal_markers
    ))
    print("STRUCTURE status=%s rows=%s highest=%s distribution=%s" % (
        structure.status, structure.row_count, structure.highest_board_height,
        dict(structure.board_height_distribution),
    ))
    print("FEEDBACK status=%s prior=%s valid_return=%s unavailable_return=%s up/down/flat=%s/%s/%s amount_sum=%s" % (
        feedback.status, feedback.prior_limit_up_count, feedback.valid_return_count,
        feedback.return_unavailable_count, feedback.up_count, feedback.down_count,
        feedback.flat_count, "%.2f" % function_amount if function_amount is not None else "UNAVAILABLE",
    ))
    for name, independent, output, comparable in checks:
        status = "UNAVAILABLE" if not comparable else "OK" if independent == output else "MISMATCH"
        print("CHECK %s %s independent=%s function=%s" % (name, status, independent, output))
    print("PRICE available_in_0925=%s/%s" % (price_count, len(rows)))
    for note in annotations:
        print("NOTE " + note)
    return 1 if mismatch_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
