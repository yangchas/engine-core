"""Small, side-effect-free opening facts extracted from ``engine_next``.

The first migration slice deliberately contains only the single-stock facts
that are already defined by the deployed opening reader.  It does not fetch
Q2, infer a limit state, or make a strategy decision.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional


OPENING_FACT_CONTRACT_VERSION = "OpeningFactV1"
_VALID_LIMIT_STATES = {-1, 0, 1}


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
