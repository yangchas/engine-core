"""Shared, point-in-time stock to primary-theme assignments."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


SOURCE_PRIORITY = ("POOL_LABEL", "REASON_LATEST", "STATIC", "HOT_PLATE_NAME")


def _symbol(value: Any) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits[-6:].zfill(6) if len(digits) >= 6 else ""


def _date(value: Any) -> str:
    return str(value or "")[:10]


def _candidate(value: Any) -> dict[str, str] | None:
    if isinstance(value, Mapping):
        identifier = str(value.get("id", value.get("code", value.get("plate_code", ""))) or "").strip()
        name = str(value.get("name", value.get("theme_name", value.get("plate_name", ""))) or "").strip()
    else:
        identifier, name = "", str(value or "").strip()
    if not identifier and not name:
        return None
    return {"id": identifier or name, "name": name or identifier}


def _as_candidates(value: Any) -> list[dict[str, str]]:
    values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Mapping)) else [value]
    result, seen = [], set()
    for item in values:
        candidate = _candidate(item)
        if candidate and candidate["id"] not in seen:
            result.append(candidate)
            seen.add(candidate["id"])
    return result


def build_stock_theme_assignments(
    *, trade_date: str, previous_trade_date: str,
    pool_labels: Mapping[str, Any] | None = None,
    reason_rows: Sequence[Mapping[str, Any]] = (),
    static_themes: Mapping[str, Any] | None = None,
    hot_plate_names: Mapping[str, Any] | None = None,
    members: Mapping[str, Sequence[Any]] | None = None,
    trading_dates: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build deterministic assignments, excluding reason records after the PIT date.

    ``members`` maps symbols to the PIT membership candidates available for the
    requested day. A source can only participate for symbols included by the
    caller's source input or membership map; missing evidence stays UNKNOWN.
    """
    pool_labels = pool_labels or {}
    static_themes = static_themes or {}
    hot_plate_names = hot_plate_names or {}
    members = members or {}
    calendar = sorted({_date(day) for day in (trading_dates or ())
                      if _date(day) and _date(day) <= previous_trade_date})
    reason_window = set(calendar[-20:] if trading_dates is not None else ())
    if not calendar or trading_dates is None:
        reason_window = {previous_trade_date} if previous_trade_date else set()
    symbols = {_symbol(key) for source in (pool_labels, static_themes, hot_plate_names, members)
               for key in source}
    latest_reason: dict[str, tuple[str, list[dict[str, str]]]] = {}
    for row in reason_rows:
        if not isinstance(row, Mapping):
            continue
        symbol = _symbol(row.get("symbol", row.get("code", row.get("StockID"))))
        event_day = _date(row.get("Date", row.get("date")))
        if not symbol or not event_day or event_day not in reason_window:
            continue
        if str(row.get("Type", row.get("type", "0")) or "0") != "0":
            continue
        codes = row.get("ZSCode", row.get("zs_code", row.get("themes", [])))
        candidates = _as_candidates(codes)
        if not candidates:
            continue
        old = latest_reason.get(symbol)
        if old is None or event_day > old[0]:
            latest_reason[symbol] = (event_day, candidates)
    for symbol in latest_reason:
        symbols.add(symbol)

    output: dict[str, dict[str, Any]] = {}
    for symbol in sorted(value for value in symbols if value):
        by_source: dict[str, list[dict[str, str]]] = {
            "POOL_LABEL": _as_candidates(pool_labels.get(symbol)),
            "REASON_LATEST": latest_reason.get(symbol, ("", []))[1],
            "STATIC": _as_candidates(static_themes.get(symbol)),
            "HOT_PLATE_NAME": _as_candidates(hot_plate_names.get(symbol)),
        }
        # Membership is an independent PIT guard. If supplied, candidates not
        # present in the symbol's dated membership list are not assignable.
        if symbol in members:
            allowed = set()
            for item in _as_candidates(members.get(symbol)):
                allowed.update((item["id"], item["name"]))
            by_source = {source: [item for item in candidates
                                  if item["id"] in allowed or item["name"] in allowed]
                         for source, candidates in by_source.items()}
        primary_source = next((source for source in SOURCE_PRIORITY if by_source[source]), "UNKNOWN")
        ordered, seen = [], set()
        for source in SOURCE_PRIORITY:
            for item in by_source[source]:
                if item["id"] not in seen:
                    ordered.append(item)
                    seen.add(item["id"])
        primary = by_source[primary_source][0] if primary_source != "UNKNOWN" else None
        output[symbol] = {
            "symbol": symbol, "primary_theme": primary,
            "secondary_themes": [item for item in ordered if primary is None or item["id"] != primary["id"]],
            "source": primary_source, "as_of": previous_trade_date,
            "used_date": previous_trade_date, "trade_date": trade_date,
        }
    version_payload = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version = "sha256-" + hashlib.sha256(version_payload.encode("utf-8")).hexdigest()[:16]
    for assignment in output.values():
        assignment["version"] = version
    return output
