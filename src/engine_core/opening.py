"""Side-effect-free opening facts and cohort summaries extracted from ``engine_next``.

These helpers operate only on already-read/normalized values.  They do not
fetch Q2, infer a limit state, impose a run gate, or make a strategy decision.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from .contracts import semantic_hash

OPENING_FACT_CONTRACT_VERSION = "OpeningFactV1"
OPENING_TRANSITION_FACT_CONTRACT_VERSION = "OpeningTransitionFactV1"
OPENING_AMOUNT_SUMMARY_CONTRACT_VERSION = "OpeningAmountSummaryV1"
OPENING_LIMIT_STATE_SUMMARY_CONTRACT_VERSION = "OpeningLimitStateSummaryV1"
OPENING_PLATE_AMOUNT_SUMMARY_CONTRACT_VERSION = "OpeningPlateAmountSummaryV1"
_VALID_LIMIT_STATES = {-1, 0, 1}
_OPENING_COHORT_SCOPES = {
    "OBSERVED_COHORT",
    "FRESH_OBSERVED_COHORT",
    "STALE_OBSERVED_COHORT",
    "UNCLASSIFIED_TIME_OBSERVED_COHORT",
}


def _number(value: Any) -> Optional[float]:
    """Return a finite number using the legacy reader's coercion rule."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def compute_open_change_pct(
    price_milli: Any,
    previous_close_milli: Any,
) -> Optional[float]:
    """Compute opening percentage change in percentage-point units.

    Both prices must be finite and strictly positive.  The returned value is
    e.g. ``5.0`` for a five-percent change, not ``0.05`` or basis points.
    Missing, malformed, zero, and negative inputs return ``None``.
    """

    price = _number(price_milli)
    previous = _number(previous_close_milli)
    if price is None or previous is None or price <= 0 or previous <= 0:
        return None
    return ((price / previous) - 1.0) * 100.0


def compute_delta(after: Any, before: Any) -> Optional[float]:
    """Return ``after - before`` when both operands are finite numbers."""

    after_number = _number(after)
    before_number = _number(before)
    if after_number is None or before_number is None:
        return None
    return after_number - before_number


def compute_change_delta_bp(
    opening_change_pct: Any,
    auction_change_pct: Any,
) -> Optional[int]:
    """Convert an opening-minus-auction percentage delta to basis points.

    Both inputs use the deployed reader's percentage-point unit (``5.0`` is
    five percent).  The explicit ``* 100`` conversion therefore yields basis
    points, matching ``engine_next._change_bp``.  Missing or malformed values
    remain unavailable.
    """

    delta = compute_delta(opening_change_pct, auction_change_pct)
    return round(delta * 100.0) if delta is not None else None


def classify_delta(delta: Any) -> str:
    """Classify a numeric delta without applying a strategy threshold."""

    value = _number(delta)
    if value is None:
        return "unavailable"
    if value > 0:
        return "expanded"
    if value < 0:
        return "contracted"
    return "unchanged"


def classify_sign_state(before: Any, after: Any) -> str:
    """Classify sign reversal using the deployed opening rule.

    Touching zero is not a reversal; it is classified by the ordinary delta
    state.  Missing or malformed operands remain ``unavailable``.
    """

    before_number = _number(before)
    after_number = _number(after)
    if before_number is None or after_number is None:
        return "unavailable"
    if before_number * after_number < 0:
        return "reversed"
    return classify_delta(compute_delta(after_number, before_number))


def _limit_state_valid(value: Any) -> bool:
    number = _number(value)
    return (
        number is not None
        and number.is_integer()
        and int(number) in _VALID_LIMIT_STATES
    )


def build_open_fact(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the deployed single-stock opening fact without I/O.

    ``change_pct`` is a percentage-point value.  ``limit_state`` is kept as a
    separate field and receives its own availability status; it is never
    inferred from price change.  Malformed present limit values are preserved
    instead of being silently converted to missing or zero.
    """

    price = _number(row.get("price_milli"))
    previous = _number(row.get("previous_close_milli"))
    valid = price is not None and previous is not None and price > 0 and previous > 0

    raw_limit_state = row.get("limit_state")
    limit_state = _number(raw_limit_state)
    if limit_state is None and raw_limit_state is not None:
        limit_state = raw_limit_state
    if limit_state is None:
        limit_state_status = "unavailable"
    elif _limit_state_valid(limit_state):
        limit_state_status = "available"
    else:
        limit_state_status = "invalid"

    normalized_limit_state: Any = limit_state
    if (
        isinstance(limit_state, (int, float))
        and not isinstance(limit_state, bool)
        and float(limit_state).is_integer()
    ):
        normalized_limit_state = int(limit_state)

    return {
        "symbol": str(row.get("symbol") or ""),
        "timestamp_ms": row.get("timestamp_ms"),
        "change_pct": compute_open_change_pct(price, previous),
        "amount_2m_yuan": _number(row.get("amount_2m_yuan")),
        "limit_state": normalized_limit_state,
        "limit_state_status": limit_state_status,
        "name": str(row.get("name") or ""),
        "speed_1m": _number(row.get("speed_1m")),
        "status": "available" if valid else "unavailable",
    }


def build_opening_limit_state_summary(
    facts_by_symbol: Mapping[str, Mapping[str, Any]],
    *,
    expected_symbols: Optional[Sequence[str]] = None,
    scope: str = "OBSERVED_COHORT",
) -> dict[str, Any]:
    """Aggregate producer ``limit_state`` facts without turning them into a gate.

    The input is the per-symbol result of :func:`build_open_fact`.  As in the
    deployed opening consumer, the limit-state denominator contains rows with
    a price-valid opening fact (``status == "available"``).  Quality counts
    are independent of price breadth: a missing state is not a normal state,
    and an invalid state is not missing.  Known enum counts are always retained
    with their valid-value denominator, even for a partial cohort.

    ``scope`` describes the caller-selected cohort; only observed/fresh/stale
    Q2 cohorts are accepted here.  This function cannot establish a full
    market universe, so its output always keeps that authority UNPROVEN.
    """

    if scope not in _OPENING_COHORT_SCOPES:
        raise ValueError("scope must identify an observed Q2 cohort")

    observed_symbols = {str(symbol) for symbol in facts_by_symbol}
    expected = (
        observed_symbols
        if expected_symbols is None
        else {str(symbol) for symbol in expected_symbols}
    )
    if observed_symbols - expected:
        raise ValueError("facts contain symbols outside expected_symbols")

    eligible = tuple(
        (str(symbol), fact)
        for symbol, fact in facts_by_symbol.items()
        if fact.get("status") == "available"
    )
    present_count = valid_count = invalid_count = 0
    counts = {"up_count": 0, "normal_count": 0, "down_count": 0}
    for _symbol, fact in eligible:
        value = fact.get("limit_state")
        if value is None:
            continue
        present_count += 1
        if not _limit_state_valid(value):
            invalid_count += 1
            continue
        valid_count += 1
        state = int(_number(value))
        if state == 1:
            counts["up_count"] += 1
        elif state == -1:
            counts["down_count"] += 1
        else:
            counts["normal_count"] += 1

    total_count = len(eligible)
    missing_count = total_count - present_count
    if total_count and valid_count == total_count:
        status = "available"
    elif valid_count:
        status = "partial"
    else:
        status = "unavailable"

    summary: dict[str, Any] = {
        "contract": OPENING_LIMIT_STATE_SUMMARY_CONTRACT_VERSION,
        "scope": scope,
        "scope_authority": "Q2_COHORT_ONLY_NOT_FULL_MARKET",
        "full_market_coverage": "UNPROVEN",
        "status_scope": "PRICE_VALID_OPENING_FACTS",
        "source_field": "Q2.limit_state",
        "expected_count": len(expected),
        "observed_count": len(observed_symbols),
        "missing_symbol_count": len(expected - observed_symbols),
        "symbol_coverage": (
            len(observed_symbols) / len(expected) if expected else None
        ),
        "price_eligible_count": total_count,
        "price_ineligible_count": len(observed_symbols) - total_count,
        "limit_state_total_count": total_count,
        "limit_state_present_count": present_count,
        "limit_state_valid_count": valid_count,
        "limit_state_missing_count": missing_count,
        "limit_state_invalid_count": invalid_count,
        "limit_state_counts": counts,
        "valid_count_denominator": valid_count,
        "valid_coverage": valid_count / total_count if total_count else None,
        "cohort_field_status": status,
    }
    summary["content_hash"] = semantic_hash(summary)
    return summary


def build_opening_amount_summary(
    facts_by_symbol: Mapping[str, Mapping[str, Any]],
    *,
    expected_symbols: Optional[Sequence[str]] = None,
    scope: str = "OBSERVED_COHORT",
) -> dict[str, Any]:
    """Aggregate Q2 two-minute amounts over price-valid opening facts.

    This follows the deployed ``open_confirmation`` market sum: only rows with
    an available price-based opening fact enter the denominator, and the sum
    is exposed only when every eligible row has a finite amount.  A numeric
    zero is present data; a missing or malformed amount is not zero.  The
    status describes this observed cohort only and never gates processing or
    claims full-market coverage.
    """

    if scope not in _OPENING_COHORT_SCOPES:
        raise ValueError("scope must identify an observed Q2 cohort")

    observed_symbols = {str(symbol) for symbol in facts_by_symbol}
    expected = (
        observed_symbols
        if expected_symbols is None
        else {str(symbol) for symbol in expected_symbols}
    )
    if observed_symbols - expected:
        raise ValueError("facts contain symbols outside expected_symbols")

    eligible = tuple(
        fact
        for fact in facts_by_symbol.values()
        if fact.get("status") == "available"
    )
    values = tuple(_number(fact.get("amount_2m_yuan")) for fact in eligible)
    total_count = len(eligible)
    present_count = sum(value is not None for value in values)
    missing_count = total_count - present_count
    if total_count and present_count == total_count:
        status = "available"
        amount_sum = sum(value for value in values if value is not None)
    elif present_count:
        status = "partial"
        amount_sum = None
    else:
        status = "unavailable"
        amount_sum = None

    summary: dict[str, Any] = {
        "contract": OPENING_AMOUNT_SUMMARY_CONTRACT_VERSION,
        "scope": scope,
        "scope_authority": "Q2_COHORT_ONLY_NOT_FULL_MARKET",
        "full_market_coverage": "UNPROVEN",
        "status_scope": "PRICE_VALID_OPENING_FACTS",
        "source_field": "Q2.amount_2m_yuan",
        "aggregation": "SUM_WHEN_ALL_PRICE_VALID_COHORT_VALUES_PRESENT",
        "expected_count": len(expected),
        "observed_count": len(observed_symbols),
        "missing_symbol_count": len(expected - observed_symbols),
        "symbol_coverage": (
            len(observed_symbols) / len(expected) if expected else None
        ),
        "price_eligible_count": total_count,
        "price_ineligible_count": len(observed_symbols) - total_count,
        "amount_2m_yuan_total_count": total_count,
        "amount_2m_yuan_present_count": present_count,
        "amount_2m_yuan_missing_count": missing_count,
        "amount_2m_yuan_coverage": (
            present_count / total_count if total_count else None
        ),
        "amount_2m_yuan_sum": amount_sum,
        "amount_2m_yuan_status": status,
    }
    summary["content_hash"] = semantic_hash(summary)
    return summary


def build_opening_plate_amount_summary(
    facts_by_symbol: Mapping[str, Mapping[str, Any]],
    *,
    mapped_symbols_by_plate: Mapping[str, Sequence[str]],
    auction_symbols_by_plate: Mapping[str, Sequence[str]],
    auction_top1_amount_ratio_by_plate: Mapping[str, Any],
    selected_plates: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Aggregate the deployed per-plate opening amount/concentration facts.

    ``mapped_symbols_by_plate`` comes from the date-frozen canonical stock to
    plate mapping carried by the auction detail snapshot. ``auction_symbols``
    is its subset with a usable 09:25 price/amount fact. The legacy consumer
    intentionally used two cohorts: total opening amount covers every mapped
    symbol with an available opening fact, while Top1/Top3 concentration uses
    only the intersection with valid 09:25 auction symbols. Keep both counts
    visible; they are not interchangeable denominators.

    ``auction_top1_amount_ratio_by_plate`` is an already-computed auction fact,
    not a strategy input. Partial opening amounts remain visible as partial
    coverage but do not yield a complete sum or concentration ratio. The
    function is pure and does not determine plate membership, data freshness,
    selection ranking, or full-market authority.
    """

    def normalize_memberships(
        values_by_plate: Mapping[str, Sequence[str]],
    ) -> dict[str, set[str]]:
        return {
            str(plate): {
                str(symbol).strip()
                for symbol in symbols
                if str(symbol).strip()
            }
            for plate, symbols in values_by_plate.items()
        }

    mapped_by_plate = normalize_memberships(mapped_symbols_by_plate)
    auction_by_plate = normalize_memberships(auction_symbols_by_plate)
    if selected_plates is None:
        selected = sorted(set(mapped_by_plate) | set(auction_top1_amount_ratio_by_plate))
    else:
        selected = list(dict.fromkeys(str(plate).strip() for plate in selected_plates if str(plate).strip()))

    plate_summaries: list[dict[str, Any]] = []
    for plate in selected:
        mapped_symbols = mapped_by_plate.get(plate, set())
        observed_open_symbols = mapped_symbols & {str(symbol) for symbol in facts_by_symbol}
        common_symbols = auction_by_plate.get(plate, set()) & observed_open_symbols
        valid_open = tuple(
            facts_by_symbol[symbol]
            for symbol in sorted(observed_open_symbols)
            if facts_by_symbol[symbol].get("status") == "available"
        )
        valid_comparison = tuple(
            facts_by_symbol[symbol]
            for symbol in sorted(common_symbols)
            if facts_by_symbol[symbol].get("status") == "available"
        )

        def complete_sum(
            rows: Sequence[Mapping[str, Any]],
        ) -> tuple[float | None, str, int, int]:
            if not rows:
                return None, "unavailable", 0, 0
            values = tuple(_number(row.get("amount_2m_yuan")) for row in rows)
            present_count = sum(value is not None for value in values)
            if present_count == len(rows):
                return sum(value for value in values if value is not None), "available", present_count, len(rows)
            status = "partial" if present_count else "unavailable"
            return None, status, present_count, len(rows)

        open_total, open_status, open_present, open_count = complete_sum(valid_open)
        comparison_total, comparison_status, comparison_present, comparison_count = complete_sum(valid_comparison)
        comparison_amounts = tuple(
            value
            for value in (_number(row.get("amount_2m_yuan")) for row in valid_comparison)
            if value is not None and value >= 0
        )
        ordered_amounts = sorted(comparison_amounts, reverse=True)
        open_top1 = (
            ordered_amounts[0] / comparison_total
            if comparison_total not in (None, 0) and ordered_amounts
            else None
        )
        open_top3 = (
            sum(ordered_amounts[:3]) / comparison_total
            if comparison_total not in (None, 0) and ordered_amounts
            else None
        )
        auction_top1 = _number(auction_top1_amount_ratio_by_plate.get(plate))
        top1_delta = compute_delta(open_top1, auction_top1)

        plate_summaries.append(
            {
                "plate": plate,
                "mapped_symbol_count": len(mapped_symbols),
                "auction_symbol_count": len(auction_by_plate.get(plate, set())),
                "open_valid_count": len(valid_open),
                "common_symbol_count": len(common_symbols),
                "comparison_valid_count": len(valid_comparison),
                "comparison_scope": "COMMON_VALID_AUCTION_AND_OPEN_SYMBOLS",
                "open_window_amount_yuan": open_total,
                "open_window_amount_status": open_status,
                "open_amount_present_count": open_present,
                "open_amount_total_count": open_count,
                "comparison_amount_status": comparison_status,
                "comparison_amount_present_count": comparison_present,
                "comparison_amount_total_count": comparison_count,
                "open_top1_amount_ratio": open_top1,
                "open_top3_amount_ratio": open_top3,
                "auction_top1_amount_ratio": auction_top1,
                "top1_amount_ratio_delta": top1_delta,
                "concentration_state": classify_delta(top1_delta),
                "open_symbols": [
                    symbol
                    for symbol in sorted(observed_open_symbols)
                    if facts_by_symbol[symbol].get("status") == "available"
                ],
            }
        )

    summary: dict[str, Any] = {
        "contract": OPENING_PLATE_AMOUNT_SUMMARY_CONTRACT_VERSION,
        "scope": "FROZEN_MAPPING_AND_OBSERVED_Q2_COHORT",
        "scope_authority": "FROZEN_MAPPING_AND_OBSERVED_Q2_COHORT",
        "full_market_coverage": "UNPROVEN",
        "status_scope": "MAPPED_OPEN_FACTS_AND_VALID_COMMON_SYMBOLS",
        "source_field": "Q2.amount_2m_yuan",
        "aggregation": "LEGACY_OPEN_AMOUNT_AND_COMMON_SYMBOL_TOP1_TOP3",
        "selected_plate_count": len(plate_summaries),
        "plates": plate_summaries,
    }
    summary["content_hash"] = semantic_hash(summary)
    return summary


def build_opening_transition_fact(
    auction_change_pct: Any,
    opening_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare a normalized auction change with one opening observation.

    ``auction_change_pct`` must already be in percentage-point units.  The
    function intentionally does not interpret raw ``chg_bp`` or apply a
    strategy threshold; source-specific unit conversion belongs in the
    provider/adapter boundary.  Missing either side leaves the delta facts
    unavailable rather than manufacturing zero.
    """

    opening = build_open_fact(opening_row)
    auction = _number(auction_change_pct)
    opening_change = opening["change_pct"]
    delta_pct = compute_delta(opening_change, auction)
    return {
        "symbol": opening["symbol"],
        "auction_change_pct": auction,
        "opening_change_pct": opening_change,
        "delta_change_pct": delta_pct,
        "delta_change_bp": compute_change_delta_bp(opening_change, auction),
        "delta_state": classify_delta(delta_pct),
        "sign_state": classify_sign_state(auction, opening_change),
        "status": "available" if delta_pct is not None else "unavailable",
    }
