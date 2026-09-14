"""Deterministic per-symbol auction-anchor delta facts.

The implementation mirrors the audited, pure ``engine_next`` helper for the
smallest Gate-B migration slice.  It accepts already-normalized anchor rows;
it does not read a provider, infer arrival order, fill missing values, or make
a strategy decision.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


ANCHOR_DELTA_CONTRACT_VERSION = "AnchorDeltaFactV1"
_REFERENCE_BUCKETS = (
    (500_000.0, "lt_500k"),
    (2_000_000.0, "500k_2m"),
    (5_000_000.0, "2m_5m"),
    (math.inf, "gte_5m"),
)


def build_anchor_shadow_evidence(
    rows: Iterable[Mapping[str, Any]],
    *,
    from_tag: str,
    to_tag: str,
) -> tuple[dict[str, Any], ...]:
    """Build sorted per-symbol evidence for one anchor transition.

    Rows are expected to be normalized mappings.  A symbol may have one or
    both anchor rows; missing sides remain ``unavailable``.  Duplicate rows
    are not deduplicated here: the upstream normalized-input contract must
    establish uniqueness, matching the legacy helper's last-row behavior.
    """

    grouped: dict[str, dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        symbol = _symbol(row.get("symbol") or row.get("s"))
        tag = str(row.get("tag") or "").strip()
        if not symbol or tag not in {from_tag, to_tag}:
            continue
        grouped.setdefault(symbol, {})[tag] = row
    return tuple(
        build_anchor_delta_evidence(
            pair.get(from_tag),
            pair.get(to_tag),
            symbol=symbol,
            from_tag=from_tag,
            to_tag=to_tag,
        )
        for symbol, pair in sorted(grouped.items())
    )


def build_anchor_delta_evidence(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
    *,
    symbol: str = "",
    from_tag: str = "0924",
    to_tag: str = "0925",
) -> dict[str, Any]:
    """Return the audited legacy-compatible two-anchor fact mapping.

    Required semantic inputs are price in milli-yuan, amounts in yuan, and
    finite non-negative values.  Missing data is not converted to zero.  The
    output's ``direction`` and ``labels`` are descriptive facts only; they are
    not a strategy authorization or net-flow claim.
    """

    result: dict[str, Any] = {
        "symbol": symbol,
        "from_anchor": from_tag,
        "to_anchor": to_tag,
        "amount_delta_yuan": None,
        "price_delta_milli": None,
        "rest_bid_delta_yuan": None,
        "rest_ask_delta_yuan": None,
        "pressure_delta_yuan": None,
        "amount_ratio": None,
        "withdrawal_yuan": 0.0,
        "auction_directional_pressure_yuan": 0.0,
        "direction": "unresolved",
        "status": "unavailable",
        "labels": [],
        "amount_reference_bucket": None,
        "reference_labels": [],
    }

    previous_values, previous_status = _snapshot_values(previous)
    current_values, current_status = _snapshot_values(current)
    if previous_status == "invalid" or current_status == "invalid":
        result["status"] = "invalid"
        return result
    if previous_status != "valid" or current_status != "valid":
        return result

    amount_delta = current_values["amount"] - previous_values["amount"]
    price_delta = current_values["price"] - previous_values["price"]
    bid_delta = current_values["bid"] - previous_values["bid"]
    ask_delta = current_values["ask"] - previous_values["ask"]
    pressure_delta = (current_values["bid"] - current_values["ask"]) - (
        previous_values["bid"] - previous_values["ask"]
    )
    result.update(
        {
            "amount_delta_yuan": amount_delta,
            "price_delta_milli": price_delta,
            "rest_bid_delta_yuan": bid_delta,
            "rest_ask_delta_yuan": ask_delta,
            "pressure_delta_yuan": pressure_delta,
            "amount_ratio": amount_delta / previous_values["amount"] + 1.0
            if previous_values["amount"] > 0
            else None,
            "withdrawal_yuan": max(-amount_delta, 0.0),
            "amount_reference_bucket": amount_reference_bucket(current_values["amount"]),
        }
    )

    direction = "unresolved"
    labels: list[str] = []
    pressure = 0.0
    if amount_delta > 0:
        if price_delta > 0:
            direction, pressure = "positive", amount_delta
            labels.append("volume_price_strengthening")
        elif price_delta < 0:
            direction, pressure = "negative", -amount_delta
            labels.append("volume_price_weakening")
        elif pressure_delta > 0:
            direction, pressure = "positive", amount_delta
            labels.append("buy_pressure_building")
        elif pressure_delta < 0:
            direction, pressure = "negative", -amount_delta
            labels.append("sell_pressure_building")
        else:
            labels.append("unresolved_direction")
    elif amount_delta < 0:
        labels.append("withdrawal_or_cooling")
    elif pressure_delta > 0:
        labels.append("buy_pressure_building")
    elif pressure_delta < 0:
        labels.append("sell_pressure_building")

    result["direction"] = direction
    result["status"] = "resolved" if direction != "unresolved" else (
        "unresolved" if amount_delta != 0 or pressure_delta != 0 else "balanced"
    )
    result["auction_directional_pressure_yuan"] = pressure
    result["labels"] = labels
    if result["amount_reference_bucket"] == "lt_500k":
        result["reference_labels"] = ["small_volume_unconfirmed"]
    return result


def amount_reference_bucket(amount_yuan: Any) -> str:
    """Return the non-binding display bucket used by the legacy helper."""

    value = _first_number({"value": amount_yuan}, ("value",))
    if value is None or value < 0:
        return "invalid"
    for upper_bound, name in _REFERENCE_BUCKETS:
        if value < upper_bound:
            return name
    return "gte_5m"


def _snapshot_values(row: Mapping[str, Any] | None) -> tuple[dict[str, float], str]:
    if row is None:
        return {}, "unavailable"
    price = _price_milli(row)
    amount = _first_number(row, ("auction_amount_yuan", "amount", "am"))
    bid = _first_number(row, ("bid_amount_yuan", "bid_amount", "br"))
    ask = _first_number(row, ("ask_amount_yuan", "ask_amount", "ar"))
    if row.get("ask_amount_present", True) is False:
        ask = None
    values = {"price": price, "amount": amount, "bid": bid, "ask": ask}
    if any(value is None for value in values.values()):
        return {}, "unavailable"
    if any(not math.isfinite(float(value)) or float(value) < 0 for value in values.values()):
        return {}, "invalid"
    if float(values["price"]) <= 0:
        return {}, "invalid"
    return {name: float(value) for name, value in values.items()}, "valid"


def _price_milli(row: Mapping[str, Any]) -> float | None:
    value = _first_number(row, ("price_milli", "px"))
    if value is not None:
        return value
    price = _first_number(row, ("price",))
    return price * 1000.0 if price is not None else None


def _first_number(row: Mapping[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        if name in row and row[name] is not None and str(row[name]).strip() != "":
            try:
                value = float(row[name])
            except (TypeError, ValueError):
                continue
            return value if math.isfinite(value) else None
    return None


def _symbol(value: Any) -> str:
    text = str(value or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text if len(text) == 6 and text.isdigit() else ""
