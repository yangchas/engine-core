"""Pure point-in-time stock labels for plate session reports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _feature_value(feature_set: Any, key: str, default: Any = None) -> Any:
    if feature_set is None:
        return default
    if isinstance(feature_set, Mapping):
        return feature_set.get(key, default)
    return getattr(feature_set, key, default)


def label_stock(
    symbol: str,
    selected_plates: Sequence[Mapping[str, Any]],
    feature_set: Any,
    registry: Mapping[str, Mapping[str, Any]],
    ret: float | None,
    is_limit_up: bool,
) -> dict[str, Any]:
    """Label a stock from selected plate membership and prebuilt static features.

    ``selected_plates`` includes selected rows with their member collections and
    may include registry rows without a direction to support EVENT_ONLY facts.
    """
    symbol = str(symbol)
    related: list[Mapping[str, Any]] = []
    dynamic: list[Mapping[str, Any]] = []
    for plate in selected_plates:
        members = plate.get("members", ())
        if symbol not in members:
            continue
        related.append(plate)
        if plate.get("direction") in {"STRONG", "WEAK"}:
            dynamic.append(plate)

    primary = _feature_value(feature_set, "primary")
    flags: list[str] = []
    evidence_flags = _feature_value(feature_set, "flags", ()) or ()
    primary_source = _feature_value(feature_set, "primary_source")
    primary_rows = [row for row in (_feature_value(feature_set, "plates", ()) or ())
                    if str(row.get("code")) == str(_feature_value(feature_set, "primary"))]
    if ("EVIDENCE_MISSING" in evidence_flags or
            (primary_source == "STATIC" and primary_rows and
             not set(primary_rows[0].get("reasons", ())).intersection({"POOL", "REASON", "LEADER"}))):
        flags.append("EVIDENCE_MISSING")
    all_memberships = related
    if all_memberships and all(
        str(row.get("class", registry.get(str(row.get("code", "")), {}).get("class", "")))
        in {"EVENT", "GENERIC_FACTOR"}
        for row in all_memberships
    ):
        flags.append("EVENT_ONLY")

    if dynamic:
        chosen = min(dynamic, key=lambda row: (int(row.get("member_count", len(row.get("members", ())))),
                                                str(row.get("code", ""))))
        code = str(chosen.get("code", ""))
        name = chosen.get("name") or registry.get(code, {}).get("name") or code
        source = "DYNAMIC"
    elif primary is not None:
        code = str(primary)
        feature_plates = _feature_value(feature_set, "plates", ()) or ()
        primary_row = next((row for row in feature_plates if str(row.get("code")) == code), {})
        name = primary_row.get("name") or registry.get(code, {}).get("name") or code
        source = "STATIC_NO_SIGNAL"
    else:
        code, name, source = None, None, "NO_PLATE"
        flags.append("NO_PLATE")

    direction = next((row.get("direction") for row in dynamic
                      if str(row.get("code")) == code), None)
    counter = (is_limit_up or any(symbol in row.get("counter_weak_symbols", ()) for row in dynamic)) and any(
        row.get("direction") == "WEAK" for row in dynamic
    )
    counter = counter or any(symbol in row.get("counter_strong_symbols", ()) for row in dynamic) and any(
        row.get("direction") == "STRONG" for row in dynamic
    )
    if counter:
        flags.append("COUNTER_TREND")
    return {"source": source, "plate": ({"code": code, "name": name} if code else None),
            "direction": direction, "flags": sorted(set(flags))}


__all__ = ["label_stock"]
