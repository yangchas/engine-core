"""Side-effect-free opening facts and cohort summaries extracted from ``engine_next``.

These helpers operate only on already-read/normalized values.  They do not
fetch Q2, infer a limit state, impose a run gate, or make a strategy decision.
"""

from __future__ import annotations

import math
from datetime import date
from statistics import median
from typing import Any, Mapping, Optional, Sequence

from .contracts import semantic_hash

OPENING_FACT_CONTRACT_VERSION = "OpeningFactV1"
OPENING_TRANSITION_FACT_CONTRACT_VERSION = "OpeningTransitionFactV1"
OPENING_AMOUNT_SUMMARY_CONTRACT_VERSION = "OpeningAmountSummaryV1"
OPENING_LIMIT_STATE_SUMMARY_CONTRACT_VERSION = "OpeningLimitStateSummaryV1"
OPENING_PLATE_AMOUNT_CONTEXT_CONTRACT_VERSION = "OpeningPlateAmountContextV1"
OPENING_PLATE_AMOUNT_SUMMARY_CONTRACT_VERSION = "OpeningPlateAmountSummaryV1"
OPENING_PLATE_PRICE_SUMMARY_CONTRACT_VERSION = "OpeningPlatePriceSummaryV2"
OPENING_PLATE_PRICE_REFERENCE_CONTEXT_CONTRACT_VERSION = "OpeningPlatePriceReferenceV1"
OPENING_TRANSITION_SUMMARY_CONTRACT_VERSION = "OpeningTransitionSummaryV1"
_VALID_LIMIT_STATES = {-1, 0, 1}
_OPENING_COHORT_SCOPES = {
    "OBSERVED_COHORT",
    "FRESH_OBSERVED_COHORT",
    "STALE_OBSERVED_COHORT",
    "UNCLASSIFIED_TIME_OBSERVED_COHORT",
}


def _cohort_scope(
    observed_symbols: Sequence[str] | set[str],
    expected_symbols: Optional[Sequence[str]],
) -> tuple[set[str], set[str], list[str]]:
    """Return the declared cohort, its observed members, and extras.

    A stray row must not abort a fact summary for the declared cohort. Keep it
    visible in diagnostics and exclude it from the cohort's denominator and
    aggregates. Missing expected symbols remain represented separately by the
    caller's existing coverage fields.
    """

    observed = {str(symbol) for symbol in observed_symbols}
    expected = (
        observed
        if expected_symbols is None
        else {str(symbol) for symbol in expected_symbols}
    )
    return expected, observed & expected, sorted(observed - expected)


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

    expected, observed_symbols, out_of_scope_symbols = _cohort_scope(
        {str(symbol) for symbol in facts_by_symbol}, expected_symbols
    )

    eligible = tuple(
        (str(symbol), fact)
        for symbol, fact in facts_by_symbol.items()
        if str(symbol) in observed_symbols and fact.get("status") == "available"
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
    if out_of_scope_symbols:
        summary["out_of_scope_symbol_count"] = len(out_of_scope_symbols)
        summary["out_of_scope_symbols"] = out_of_scope_symbols
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

    expected, observed_symbols, out_of_scope_symbols = _cohort_scope(
        {str(symbol) for symbol in facts_by_symbol}, expected_symbols
    )

    eligible = tuple(
        fact
        for symbol, fact in facts_by_symbol.items()
        if str(symbol) in observed_symbols and fact.get("status") == "available"
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
    if out_of_scope_symbols:
        summary["out_of_scope_symbol_count"] = len(out_of_scope_symbols)
        summary["out_of_scope_symbols"] = out_of_scope_symbols
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


def build_opening_plate_price_summary(
    facts_by_symbol: Mapping[str, Mapping[str, Any]],
    *,
    mapped_symbols_by_plate: Mapping[str, Sequence[str]],
    auction_symbols_by_plate: Mapping[str, Sequence[str]],
    selected_plates: Optional[Sequence[str]] = None,
    auction_price_reference_by_plate: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Aggregate observed opening price breadth and median by frozen plate.

    The comparison cohort follows the legacy opening reader: symbols must be
    present in the frozen plate mapping, belong to the plate's valid auction
    cohort, and have an available opening fact. ``change_pct`` values that are
    missing or invalid are excluded from the price-statistic denominator and
    reported separately. This is descriptive cohort evidence; it is not a
    strategy decision or proof of full-market coverage.
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
        selected = sorted(set(mapped_by_plate) | set(auction_by_plate))
    else:
        if isinstance(selected_plates, (str, bytes)):
            raise TypeError("selected_plates must be a sequence of plate names")
        selected = sorted(
            {str(plate).strip() for plate in selected_plates if str(plate).strip()}
        )

    plate_summaries: list[dict[str, Any]] = []
    for plate in selected:
        mapped_symbols = mapped_by_plate.get(plate, set())
        auction_symbols = auction_by_plate.get(plate, set())
        observed_open_symbols = mapped_symbols & {
            str(symbol) for symbol in facts_by_symbol
        }
        common_symbols = auction_symbols & observed_open_symbols
        valid_open_symbols = {
            symbol
            for symbol in observed_open_symbols
            if facts_by_symbol[symbol].get("status") == "available"
        }
        valid_comparison = tuple(
            facts_by_symbol[symbol]
            for symbol in sorted(common_symbols)
            if facts_by_symbol[symbol].get("status") == "available"
        )
        changes = tuple(
            value
            for value in (_number(row.get("change_pct")) for row in valid_comparison)
            if value is not None
        )
        up_count = sum(value > 0 for value in changes)
        down_count = sum(value < 0 for value in changes)
        flat_count = sum(value == 0 for value in changes)
        comparison_valid_count = len(valid_comparison)
        value_count = len(changes)
        price_change_status = (
            "unavailable"
            if value_count == 0
            else "available"
            if value_count == comparison_valid_count
            else "partial"
        )
        reference = (
            None
            if auction_price_reference_by_plate is None
            else auction_price_reference_by_plate.get(plate)
        )
        if auction_price_reference_by_plate is None:
            positive_reference_status = median_reference_status = "NOT_PROVIDED"
            auction_positive_ratio = auction_median_change_pct = None
        elif reference is None:
            positive_reference_status = median_reference_status = "NOT_REPORTED"
            auction_positive_ratio = auction_median_change_pct = None
        else:
            positive_reference_status = str(
                reference.get("auction_positive_ratio_status") or "UNAVAILABLE"
            ).upper()
            median_reference_status = str(
                reference.get("auction_median_change_pct_status") or "UNAVAILABLE"
            ).upper()
            auction_positive_ratio = (
                _number(reference.get("auction_positive_ratio"))
                if positive_reference_status == "AVAILABLE"
                else None
            )
            auction_median_change_pct = (
                _number(reference.get("auction_median_change_pct"))
                if median_reference_status == "AVAILABLE"
                else None
            )
        positive_ratio_delta = compute_delta(
            up_count / float(value_count) if value_count else None,
            auction_positive_ratio,
        )
        open_median_change_pct = median(changes) if changes else None
        median_change_pct_delta = compute_delta(
            open_median_change_pct,
            auction_median_change_pct,
        )
        plate_summaries.append(
            {
                "plate": plate,
                "mapped_symbol_count": len(mapped_symbols),
                "auction_symbol_count": len(auction_symbols),
                "open_valid_count": len(valid_open_symbols),
                "common_symbol_count": len(common_symbols),
                "comparison_valid_count": comparison_valid_count,
                "comparison_scope": "COMMON_VALID_AUCTION_AND_OPEN_SYMBOLS",
                "price_change_value_count": value_count,
                "price_change_missing_count": comparison_valid_count - value_count,
                "price_change_coverage": (
                    value_count / float(comparison_valid_count)
                    if comparison_valid_count
                    else None
                ),
                "price_change_status": price_change_status,
                "open_up_count": up_count,
                "open_down_count": down_count,
                "open_flat_count": flat_count,
                "open_positive_ratio": up_count / float(value_count) if value_count else None,
                "open_negative_ratio": down_count / float(value_count) if value_count else None,
                "open_median_change_pct": open_median_change_pct,
                "open_symbols": sorted(valid_open_symbols),
                "auction_positive_ratio": auction_positive_ratio,
                "auction_positive_ratio_status": positive_reference_status,
                "positive_ratio_delta": positive_ratio_delta,
                "price_breadth_state": classify_delta(positive_ratio_delta),
                "auction_median_change_pct": auction_median_change_pct,
                "auction_median_change_pct_status": median_reference_status,
                "median_change_pct_delta": median_change_pct_delta,
                "median_change_state": classify_sign_state(
                    auction_median_change_pct,
                    open_median_change_pct,
                ),
                "comparison_symbols": [
                    symbol
                    for symbol in sorted(common_symbols)
                    if facts_by_symbol[symbol].get("status") == "available"
                ],
            }
        )

    summary: dict[str, Any] = {
        "contract": OPENING_PLATE_PRICE_SUMMARY_CONTRACT_VERSION,
        "scope": "FROZEN_MAPPING_AND_OBSERVED_Q2_COHORT",
        "scope_authority": "FROZEN_MAPPING_AND_OBSERVED_Q2_COHORT",
        "full_market_coverage": "UNPROVEN",
        "source_field": "OpeningFactV1.change_pct",
        "aggregation": "LEGACY_COMMON_VALID_OPENING_PRICE_BREADTH_MEDIAN_AND_DELTAS",
        "decision_status": "FACT_ONLY",
        "selected_plate_count": len(plate_summaries),
        "plates": plate_summaries,
    }
    summary["content_hash"] = semantic_hash(summary)
    return summary


def build_opening_plate_price_reference_context(
    *,
    trade_date: str,
    source_provenance: Mapping[str, Any],
    auction_price_stats_by_plate: Mapping[str, Mapping[str, Any]],
    selected_plates: Sequence[str],
) -> dict[str, Any]:
    """Build a hash-pinned, date-specific reference for auction plate prices.

    This sidecar carries only already-computed auction positive ratio and
    median change. Missing or malformed values remain explicitly unavailable
    or invalid; they do not block opening calculations.
    """

    try:
        parsed_date = date.fromisoformat(trade_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("trade_date must use strict YYYY-MM-DD form") from exc
    if parsed_date.isoformat() != trade_date:
        raise ValueError("trade_date must use strict YYYY-MM-DD form")
    if not isinstance(source_provenance, Mapping):
        raise TypeError("source_provenance must be a mapping")
    if source_provenance.get("trade_date", trade_date) != trade_date:
        raise ValueError("source provenance trade_date must match context trade_date")
    if not isinstance(auction_price_stats_by_plate, Mapping):
        raise TypeError("auction_price_stats_by_plate must be a mapping")
    if isinstance(selected_plates, (str, bytes)):
        raise TypeError("selected_plates must be a sequence of plate names")

    selected = sorted(
        {str(plate).strip() for plate in selected_plates if str(plate).strip()}
    )
    raw_stats_by_plate: dict[str, Mapping[str, Any]] = {}
    for raw_plate, raw_stats in auction_price_stats_by_plate.items():
        plate = str(raw_plate).strip()
        if not plate:
            raise ValueError("auction price reference plate names must be non-empty")
        if plate in raw_stats_by_plate:
            raise ValueError("auction price reference plate names must be unique after normalization")
        if plate not in selected:
            raise ValueError("auction price reference contains a plate outside selected_plates")
        if not isinstance(raw_stats, Mapping):
            raise TypeError("each auction price reference must be a mapping")
        raw_stats_by_plate[plate] = raw_stats

    def normalize_metric(
        raw_stats: Optional[Mapping[str, Any]],
        field: str,
        *,
        ratio: bool = False,
    ) -> tuple[Optional[float], str]:
        if raw_stats is None:
            return None, "NOT_REPORTED"
        if field not in raw_stats or raw_stats[field] is None:
            return None, "UNAVAILABLE"
        raw_value = raw_stats[field]
        value = _number(raw_value)
        if isinstance(raw_value, bool) or value is None:
            return None, "INVALID"
        if ratio and not 0.0 <= value <= 1.0:
            return None, "INVALID"
        return value, "AVAILABLE"

    normalized_stats: dict[str, dict[str, Any]] = {}
    for plate in selected:
        raw_stats = raw_stats_by_plate.get(plate)
        positive_ratio, positive_status = normalize_metric(
            raw_stats, "positive_ratio", ratio=True
        )
        median_change, median_status = normalize_metric(
            raw_stats, "median_change_pct"
        )
        normalized_stats[plate] = {
            "auction_positive_ratio": positive_ratio,
            "auction_positive_ratio_status": positive_status,
            "auction_median_change_pct": median_change,
            "auction_median_change_pct_status": median_status,
        }

    payload = {
        "contract": OPENING_PLATE_PRICE_REFERENCE_CONTEXT_CONTRACT_VERSION,
        "trade_date": trade_date,
        "source_provenance": dict(source_provenance),
        "selected_plates": selected,
        "auction_price_stats_by_plate": normalized_stats,
    }
    return {**payload, "content_hash": semantic_hash(payload)}


def validate_opening_plate_price_reference_context(
    context: Mapping[str, Any],
    *,
    trade_date: str,
    selected_plates: Sequence[str],
) -> dict[str, Any]:
    """Validate a frozen auction price reference and its date/cohort binding."""

    if not isinstance(context, Mapping):
        raise TypeError("plate price reference context must be a mapping")
    try:
        parsed_date = date.fromisoformat(trade_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("trade_date must use strict YYYY-MM-DD form") from exc
    if parsed_date.isoformat() != trade_date:
        raise ValueError("trade_date must use strict YYYY-MM-DD form")
    required_keys = {
        "contract",
        "trade_date",
        "source_provenance",
        "selected_plates",
        "auction_price_stats_by_plate",
        "content_hash",
    }
    if set(context) != required_keys:
        raise ValueError("plate price reference context has unsupported fields")
    if context.get("contract") != OPENING_PLATE_PRICE_REFERENCE_CONTEXT_CONTRACT_VERSION:
        raise ValueError("unsupported plate price reference context contract")
    if context.get("trade_date") != trade_date:
        raise ValueError("plate price reference context trade_date does not match replay")
    if isinstance(selected_plates, (str, bytes)):
        raise TypeError("selected_plates must be a sequence of plate names")
    expected_selected = sorted(
        {str(plate).strip() for plate in selected_plates if str(plate).strip()}
    )
    if context.get("selected_plates") != expected_selected:
        raise ValueError("plate price reference context selected_plates do not match")
    provenance = context.get("source_provenance")
    if not isinstance(provenance, Mapping):
        raise TypeError("source_provenance must be a mapping")
    if provenance.get("trade_date", trade_date) != trade_date:
        raise ValueError("source provenance trade_date must match context trade_date")
    stats_by_plate = context.get("auction_price_stats_by_plate")
    if not isinstance(stats_by_plate, Mapping) or set(stats_by_plate) != set(expected_selected):
        raise ValueError("auction price reference plates do not match selected_plates")

    allowed_statuses = {"AVAILABLE", "UNAVAILABLE", "INVALID", "NOT_REPORTED"}
    entry_keys = {
        "auction_positive_ratio",
        "auction_positive_ratio_status",
        "auction_median_change_pct",
        "auction_median_change_pct_status",
    }
    for plate, entry in stats_by_plate.items():
        if not isinstance(entry, Mapping) or set(entry) != entry_keys:
            raise ValueError(f"auction price reference fields are invalid for {plate}")
        for value_key, status_key in (
            ("auction_positive_ratio", "auction_positive_ratio_status"),
            ("auction_median_change_pct", "auction_median_change_pct_status"),
        ):
            status = entry[status_key]
            value = entry[value_key]
            if status not in allowed_statuses:
                raise ValueError(f"unsupported auction price reference status for {plate}")
            if status == "AVAILABLE":
                number = _number(value)
                if number is None or isinstance(value, bool):
                    raise ValueError(f"available auction reference value is invalid for {plate}")
                if value_key == "auction_positive_ratio" and not 0.0 <= number <= 1.0:
                    raise ValueError(f"auction positive ratio is out of range for {plate}")
            elif value is not None:
                raise ValueError(f"unavailable auction reference must be null for {plate}")

    payload = {key: value for key, value in context.items() if key != "content_hash"}
    if semantic_hash(payload) != context.get("content_hash"):
        raise ValueError("plate price reference context content_hash mismatch")
    return dict(context)


def build_opening_plate_amount_context(
    *,
    trade_date: str,
    source_provenance: Mapping[str, Any],
    mapped_symbols_by_plate: Mapping[str, Sequence[str]],
    auction_symbols_by_plate: Mapping[str, Sequence[str]],
    auction_top1_amount_ratio_by_plate: Mapping[str, Any],
    selected_plates: Sequence[str],
) -> dict[str, Any]:
    """Build a deterministic, date-pinned sidecar for plate opening facts.

    This is explicit replay input: Core does not discover the current mapping,
    query TD, or infer auction membership. The context is versioned and hashed
    so a replay report can identify the exact external plate cohort it used.
    Missing ratios remain ``None`` and do not block the rest of the replay.
    """

    try:
        parsed_date = date.fromisoformat(trade_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("trade_date must use strict YYYY-MM-DD form") from exc
    if parsed_date.isoformat() != trade_date:
        raise ValueError("trade_date must use strict YYYY-MM-DD form")
    if not isinstance(source_provenance, Mapping):
        raise TypeError("source_provenance must be a mapping")
    if source_provenance.get("trade_date", trade_date) != trade_date:
        raise ValueError("source provenance trade_date must match context trade_date")

    def normalize_memberships(
        groups: Mapping[str, Sequence[str]],
        name: str,
    ) -> dict[str, list[str]]:
        if not isinstance(groups, Mapping):
            raise TypeError(f"{name} must be a mapping")
        normalized: dict[str, list[str]] = {}
        for raw_plate, raw_symbols in groups.items():
            plate = str(raw_plate).strip()
            if not plate:
                raise ValueError(f"{name} plate names must be non-empty")
            if isinstance(raw_symbols, (str, bytes)):
                raise TypeError(f"{name} values must be symbol sequences")
            symbols = sorted(
                {
                    str(symbol).strip()
                    for symbol in raw_symbols
                    if str(symbol).strip()
                }
            )
            normalized[plate] = symbols
        return {plate: normalized[plate] for plate in sorted(normalized)}

    if not isinstance(auction_top1_amount_ratio_by_plate, Mapping):
        raise TypeError("auction_top1_amount_ratio_by_plate must be a mapping")
    ratios: dict[str, float | None] = {}
    for raw_plate, raw_ratio in auction_top1_amount_ratio_by_plate.items():
        plate = str(raw_plate).strip()
        if not plate:
            raise ValueError("auction ratio plate names must be non-empty")
        ratios[plate] = _number(raw_ratio)
    ratios = {plate: ratios[plate] for plate in sorted(ratios)}

    if isinstance(selected_plates, (str, bytes)):
        raise TypeError("selected_plates must be a sequence of plate names")
    selected = sorted({str(plate).strip() for plate in selected_plates if str(plate).strip()})
    payload = {
        "contract": OPENING_PLATE_AMOUNT_CONTEXT_CONTRACT_VERSION,
        "trade_date": trade_date,
        "source_provenance": dict(source_provenance),
        "mapped_symbols_by_plate": normalize_memberships(
            mapped_symbols_by_plate, "mapped_symbols_by_plate"
        ),
        "auction_symbols_by_plate": normalize_memberships(
            auction_symbols_by_plate, "auction_symbols_by_plate"
        ),
        "auction_top1_amount_ratio_by_plate": ratios,
        "selected_plates": selected,
    }
    return {**payload, "content_hash": semantic_hash(payload)}


def validate_opening_plate_amount_context(
    context: Mapping[str, Any],
    *,
    trade_date: str,
) -> dict[str, Any]:
    """Validate the sidecar contract, date binding, and canonical content hash."""

    if not isinstance(context, Mapping):
        raise TypeError("plate amount context must be a mapping")
    if context.get("contract") != OPENING_PLATE_AMOUNT_CONTEXT_CONTRACT_VERSION:
        raise ValueError("unsupported plate amount context contract")
    if context.get("trade_date") != trade_date:
        raise ValueError("plate amount context trade_date does not match replay")
    try:
        normalized = build_opening_plate_amount_context(
            trade_date=str(context.get("trade_date")),
            source_provenance=context["source_provenance"],
            mapped_symbols_by_plate=context["mapped_symbols_by_plate"],
            auction_symbols_by_plate=context["auction_symbols_by_plate"],
            auction_top1_amount_ratio_by_plate=context[
                "auction_top1_amount_ratio_by_plate"
            ],
            selected_plates=context["selected_plates"],
        )
    except KeyError as exc:
        raise ValueError(f"plate amount context is missing {exc.args[0]}") from exc
    if normalized != dict(context):
        raise ValueError("plate amount context content_hash or canonical fields mismatch")
    return normalized


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


def build_opening_transition_summary(
    auction_anchor_facts_by_symbol: Mapping[str, Mapping[str, Any]],
    opening_rows_by_symbol: Mapping[str, Mapping[str, Any]],
    *,
    expected_symbols: Optional[Sequence[str]] = None,
    scope: str = "OBSERVED_COHORT",
) -> dict[str, Any]:
    """Summarize 09:25 anchor-to-opening changes over an observed Q2 cohort.

    The auction-side percentage is derived from an available frozen anchor
    price and the opening row's previous-close price.  Missing anchors, prices,
    or previous closes remain unavailable; they are never replaced by zero.
    This is a descriptive fact summary, not a market-universe or strategy gate.
    """

    if scope not in _OPENING_COHORT_SCOPES:
        raise ValueError("scope must identify an observed Q2 cohort")

    anchors = {str(symbol): value for symbol, value in auction_anchor_facts_by_symbol.items()}
    opening_rows = {str(symbol): value for symbol, value in opening_rows_by_symbol.items()}
    input_symbols = set(anchors) | set(opening_rows)
    expected, observed_symbols, out_of_scope_symbols = _cohort_scope(
        input_symbols, expected_symbols
    )

    facts_by_symbol: dict[str, dict[str, Any]] = {}
    delta_state_counts = {
        "expanded": 0,
        "contracted": 0,
        "unchanged": 0,
        "unavailable": 0,
    }
    sign_state_counts = {
        "reversed": 0,
        "expanded": 0,
        "contracted": 0,
        "unchanged": 0,
        "unavailable": 0,
    }
    anchor_price_available_count = 0
    auction_change_available_count = 0
    opening_change_available_count = 0
    transition_comparable_count = 0

    for symbol in sorted(expected):
        anchor = anchors.get(symbol, {})
        opening_row = dict(opening_rows.get(symbol, {}))
        opening_row.setdefault("symbol", symbol)

        # An absent anchor row does not prove the producer explicitly observed
        # this symbol and declared its anchor missing. Preserve MISSING only
        # when the source fact says so; otherwise leave source availability
        # UNKNOWN while the rest of the opening cohort continues to compute.
        anchor_status = str(anchor.get("status") or "UNKNOWN").upper()
        anchor_price = _number(anchor.get("price_milli"))
        anchor_price_available = (
            anchor_status == "AVAILABLE" and anchor_price is not None and anchor_price > 0
        )
        if anchor_price_available:
            anchor_price_available_count += 1
        auction_change_pct = (
            compute_open_change_pct(
                anchor_price,
                opening_row.get("previous_close_milli"),
            )
            if anchor_price_available
            else None
        )
        if auction_change_pct is not None:
            auction_change_available_count += 1

        fact = build_opening_transition_fact(auction_change_pct, opening_row)
        if fact["opening_change_pct"] is not None:
            opening_change_available_count += 1
        if fact["status"] == "available":
            transition_comparable_count += 1
        delta_state = str(fact["delta_state"])
        sign_state = str(fact["sign_state"])
        delta_state_counts[delta_state] = delta_state_counts.get(delta_state, 0) + 1
        sign_state_counts[sign_state] = sign_state_counts.get(sign_state, 0) + 1
        fact.update(
            {
                "auction_anchor_status": anchor_status,
                "auction_source_time_ms": anchor.get("source_time_ms"),
                "opening_source_time_ms": opening_row.get("timestamp_ms"),
            }
        )
        facts_by_symbol[symbol] = fact

    status = (
        "UNAVAILABLE"
        if transition_comparable_count == 0
        else "READY"
        if expected and transition_comparable_count == len(expected)
        else "PARTIAL"
    )
    summary: dict[str, Any] = {
        "contract": OPENING_TRANSITION_SUMMARY_CONTRACT_VERSION,
        "scope": scope,
        "scope_authority": "Q2FRAME_INPUT_COHORT_ONLY_NOT_FULL_MARKET",
        "full_market_coverage": "UNPROVEN",
        "auction_source_field": "AuctionAnchorFactV1.price_milli",
        "opening_source_fields": ["Q2.px", "Q2.pc"],
        "auction_change_calculation": (
            "((frozen_0925_anchor_price_milli / opening_pre_close_milli) - 1) * 100; "
            "percentage points"
        ),
        "expected_count": len(expected),
        "observed_count": len(observed_symbols),
        "missing_symbol_count": len(expected - observed_symbols),
        "symbol_coverage": len(observed_symbols) / float(len(expected)) if expected else None,
        "auction_anchor_price_available_count": anchor_price_available_count,
        "auction_change_available_count": auction_change_available_count,
        "opening_change_available_count": opening_change_available_count,
        "transition_comparable_count": transition_comparable_count,
        "transition_unavailable_count": len(expected) - transition_comparable_count,
        "delta_state_counts": delta_state_counts,
        "sign_state_counts": sign_state_counts,
        "status": status,
        "facts_by_symbol": facts_by_symbol,
        "facts_by_symbol_hash": semantic_hash(facts_by_symbol),
    }
    if out_of_scope_symbols:
        summary["out_of_scope_symbol_count"] = len(out_of_scope_symbols)
        summary["out_of_scope_symbols"] = out_of_scope_symbols
    summary["content_hash"] = semantic_hash(summary)
    return summary
