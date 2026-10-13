"""Pure, date-scoped stock-to-plate facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .plate_rules import (
    PLATE_RULES_VERSION,
    build_plate_candidates_from_reason,
    build_yest_limit_theme_candidates,
    choose_runtime_primary_plate,
    collapse_runtime_primary_plate,
    is_generic_plate,
    is_valid_plate_candidate,
    merge_theme_lists,
    normalize_plate_name,
    order_runtime_theme_candidates,
    split_plate_tokens,
)


@dataclass(frozen=True)
class StockPlateAssignment:
    symbol: str
    trade_date: str
    primary_plate: str | None
    themes: tuple[str, ...]
    sources: tuple[str, ...]
    rule_version: str
    flags: tuple[str, ...]
    status: str
    content_hash: str


@dataclass(frozen=True)
class PlateAssignments:
    assignments: Mapping[str, StockPlateAssignment]
    notes: Mapping[str, Any]
    content_hash: str


def _clean_primary(value: Any) -> str | None:
    cleaned = normalize_plate_name(value)
    return cleaned if cleaned and is_valid_plate_candidate(cleaned) else None


def _themes(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return merge_theme_lists((), values)


def _pool_plate(row: Mapping[str, Any]) -> str:
    for key in ("plate", "pool_plate", "primary_plate", "theme"):
        value = row.get(key)
        if value is not None:
            return str(value)
    return ""


def _reason_candidates(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    result: list[str] = []
    for row in rows:
        result.extend(build_plate_candidates_from_reason(
            reason=str(row.get("reason") or ""),
            group_str=str(row.get("group_str") or ""),
            gnsm=str(row.get("gnsm") or ""),
        ))
    return merge_theme_lists((), result)


def _assignment_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_stock_plate_assignments(
    *,
    trade_date: str,
    static_primary_by_symbol: Mapping[str, Any],
    static_themes_by_symbol: Mapping[str, Any],
    pool_rows: Sequence[Mapping[str, Any]] = (),
    reasons_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    hot_plate_names: Sequence[str] = (),
) -> PlateAssignments:
    """Compose assignments without I/O, retaining missing values as missing."""
    pools: dict[str, Mapping[str, Any]] = {}
    duplicate_pool_symbols: list[str] = []
    for row in pool_rows:
        if row.get("symbol") is None:
            continue
        symbol = str(row["symbol"])
        if symbol in pools:
            duplicate_pool_symbols.append(symbol)
        pools[symbol] = row
    reasons = reasons_by_symbol or {}
    symbols = set(map(str, static_primary_by_symbol)) | set(map(str, static_themes_by_symbol)) | set(pools) | set(map(str, reasons))
    hot_names = set(split_plate_tokens(*hot_plate_names))
    assignments: dict[str, StockPlateAssignment] = {}
    rejected_pool = 0

    for symbol in sorted(symbols):
        static_primary = _clean_primary(static_primary_by_symbol.get(symbol))
        static_themes = _themes(static_themes_by_symbol.get(symbol, ()))
        pool_row = pools.get(symbol)
        pool_raw = _pool_plate(pool_row) if pool_row else ""
        pool_candidates = split_plate_tokens(pool_raw)
        pool_missing = pool_row is not None and (not pool_candidates or normalize_plate_name(pool_raw) == "其他")
        if pool_missing:
            rejected_pool += 1
        reason_rows = reasons.get(symbol, ())
        reason_candidates = _reason_candidates(reason_rows)
        sources: list[str] = []
        if static_primary or static_themes:
            sources.append("static")

        if pool_row is None:
            primary = static_primary
            themes = static_themes
        else:
            candidates = build_yest_limit_theme_candidates(
                pool_plate="" if pool_missing else pool_raw,
                reason_candidates=reason_candidates,
                existing_themes=static_themes,
            )
            primary_value = choose_runtime_primary_plate(
                candidates,
                fallback=static_primary or "",
                pool_plate="" if pool_missing else pool_raw,
                reason_candidates=reason_candidates,
            )
            primary = _clean_primary(primary_value)
            themes = order_runtime_theme_candidates(candidates, primary_plate=primary or "")
            themes = merge_theme_lists((), themes)
            if not pool_missing:
                sources.append("pool")
        if reason_candidates:
            sources.append("reason")
        if hot_names.intersection(themes) and "hot_plate" not in sources:
            sources.append("hot_plate")

        flags: list[str] = []
        if primary and is_generic_plate(primary):
            flags.append("generic_primary")
        if primary and themes and all(collapse_runtime_primary_plate(t) != collapse_runtime_primary_plate(primary) for t in themes):
            flags.append("primary_not_in_themes")
        if not themes:
            flags.append("themes_missing")
        if pool_missing:
            flags.append("pool_plate_missing")
        status = "READY" if primary and themes else "PARTIAL" if primary or themes else "MISSING"
        fields = {
            "symbol": symbol,
            "trade_date": trade_date,
            "primary_plate": primary,
            "themes": themes,
            "sources": sources,
            "rule_version": PLATE_RULES_VERSION,
            "flags": flags,
            "status": status,
        }
        assignments[symbol] = StockPlateAssignment(
            symbol=symbol,
            trade_date=trade_date,
            primary_plate=primary,
            themes=tuple(themes),
            sources=tuple(sources),
            rule_version=PLATE_RULES_VERSION,
            flags=tuple(flags),
            status=status,
            content_hash=_assignment_hash(fields),
        )

    notes = {
        "symbols": len(symbols),
        "pool_plate_missing": rejected_pool,
        "duplicate_pool_symbols": sorted(set(duplicate_pool_symbols)),
    }
    payload = {symbol: assignment.content_hash for symbol, assignment in assignments.items()}
    return PlateAssignments(assignments=assignments, notes=notes, content_hash=_assignment_hash(payload))
