"""Pure multi-membership and hierarchy facts for stock-to-plate mapping."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from .plate_rules import (
    PLATE_RULES_VERSION,
    PRIMARY_PLATE_ALIAS_EXACT,
    PRIMARY_PLATE_ALIAS_KEYWORDS,
    choose_runtime_primary_plate,
    is_generic_plate,
    is_hard_generic_plate,
    is_valid_plate_candidate,
    normalize_plate_name,
    order_runtime_theme_candidates,
    split_plate_tokens,
)
from .plate_kind import classify_plate_kind

MEMBERSHIP_POLICY_USER_PRIORITY_V1 = "USER_PRIORITY_V1"
MEMBERSHIP_POLICY_LEGACY_V1 = "LEGACY_V1"


@dataclass(frozen=True)
class PlateMembership:
    symbol: str
    plate: str
    tier: int
    rank_in_stock: int
    source: str
    generic: bool
    hard_generic: bool
    ancestors: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    kind: str = "THEME"


@dataclass(frozen=True)
class PlateHierarchy:
    parent_by_child: Mapping[str, str]
    source_by_child: Mapping[str, str]


@dataclass(frozen=True)
class StockPlateMemberships:
    by_symbol: Mapping[str, tuple[PlateMembership, ...]]
    policy: str
    rule_version: str
    content_hash: str
    legacy_weights: Mapping[str, tuple[tuple[str, float], ...]]


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _membership_payload(
    by_symbol: Mapping[str, tuple[PlateMembership, ...]], policy: str,
    legacy_weights: Mapping[str, tuple[tuple[str, float], ...]],
) -> dict[str, Any]:
    return {
        "policy": policy,
        "rule_version": PLATE_RULES_VERSION,
        "by_symbol": {
            symbol: [
                {
                    "plate": row.plate, "tier": row.tier, "rank_in_stock": row.rank_in_stock,
                    "source": row.source, "generic": row.generic, "hard_generic": row.hard_generic,
                    "ancestors": list(row.ancestors), "flags": list(row.flags), "kind": row.kind,
                }
                for row in rows
            ]
            for symbol, rows in sorted(by_symbol.items())
        },
        "legacy_weights": {
            symbol: [[plate, weight] for plate, weight in rows]
            for symbol, rows in sorted(legacy_weights.items())
        },
    }


def _result(
    by_symbol: Mapping[str, tuple[PlateMembership, ...]], policy: str,
    legacy_weights: Mapping[str, tuple[tuple[str, float], ...]],
) -> StockPlateMemberships:
    payload = _membership_payload(by_symbol, policy, legacy_weights)
    return StockPlateMemberships(
        by_symbol=dict(by_symbol), policy=policy, rule_version=PLATE_RULES_VERSION,
        content_hash=_canonical_hash(payload), legacy_weights=dict(legacy_weights),
    )


def _tokens(value: Any) -> list[str]:
    if isinstance(value, str) or value is None:
        values = (value,)
    else:
        try:
            values = tuple(value)
        except TypeError:
            values = (value,)
    result: list[str] = []
    for raw in values:
        for plate in split_plate_tokens(raw):
            if plate != "其他" and plate not in result:
                result.append(plate)
    return result


def _legacy_theme_names(pool: list[str], reasons: list[str], static: list[str]) -> list[str]:
    # This mirrors the old builder's candidate bucket order and its primary/theme
    # helpers, while keeping every fact in the new membership result.
    candidates: list[str] = []
    if pool:
        candidates.append(pool[0])
    candidates.extend(reasons)
    candidates.extend(pool)
    candidates.extend(static)
    candidates = list(dict.fromkeys(candidates))
    primary = choose_runtime_primary_plate(
        candidates, pool_plate=pool[0] if pool else "", reason_candidates=reasons,
    )
    return order_runtime_theme_candidates(candidates, primary_plate=primary)


def build_memberships(
    *,
    symbols: Iterable[str],
    pool_plate_by_symbol: Mapping[str, Any],
    reason_candidates_by_symbol: Mapping[str, Any],
    static_themes_by_symbol: Mapping[str, Any],
    hot_plate_names: Iterable[str] = (),
    policy: str = MEMBERSHIP_POLICY_USER_PRIORITY_V1,
) -> StockPlateMemberships:
    """Build deterministic, untruncated stock-to-plate memberships."""
    if policy not in (MEMBERSHIP_POLICY_USER_PRIORITY_V1, MEMBERSHIP_POLICY_LEGACY_V1):
        raise ValueError(f"unsupported membership policy: {policy}")
    hot = set(_tokens(hot_plate_names))
    output: dict[str, tuple[PlateMembership, ...]] = {}
    legacy_weights: dict[str, tuple[tuple[str, float], ...]] = {}

    for symbol in sorted({str(value) for value in symbols}):
        pool = _tokens(pool_plate_by_symbol.get(symbol, ()))
        reasons = _tokens(reason_candidates_by_symbol.get(symbol, ()))
        static = _tokens(static_themes_by_symbol.get(symbol, ()))
        # Preserve first occurrence and all contributing sources, but the best
        # (lowest) tier owns a plate hit by more than one source.
        entries: dict[str, dict[str, Any]] = {}
        for tier, source, names in ((1, "pool", pool), (2, "reason", reasons), (3, "static", static)):
            for name in names:
                entry = entries.setdefault(name, {"tier": tier, "sources": []})
                entry["tier"] = min(entry["tier"], tier)
                if source not in entry["sources"]:
                    entry["sources"].append(source)
        for name in entries:
            if name in hot:
                entries[name]["sources"].append("hot_plate")

        names = list(entries)
        if policy == MEMBERSHIP_POLICY_USER_PRIORITY_V1:
            # User-priority ranks semantic plate kinds before source tier. Keep
            # the first source encounter stable within each kind and tier.
            kind_order = {"THEME": 0, "GENERIC": 1, "EVENT": 2, "NOISE": 3}
            ordered = sorted(
                names,
                key=lambda name: (
                    kind_order[classify_plate_kind(name)], entries[name]["tier"], names.index(name),
                ),
            )
        else:
            # Keep the legacy fallback order byte-for-byte equivalent to the
            # pre-kind implementation; the dedicated legacy order is applied
            # below and retains its historical weights.
            ordered = sorted(
                names, key=lambda name: (is_generic_plate(name), entries[name]["tier"], names.index(name)),
            )
            if ordered and all(is_generic_plate(name) for name in ordered):
                ordered = sorted(names, key=lambda name: (entries[name]["tier"], names.index(name)))

        if policy == MEMBERSHIP_POLICY_LEGACY_V1:
            legacy_order = _legacy_theme_names(pool, reasons, static)
            for name in legacy_order:
                if name not in entries:
                    # Legacy alias collapse can select a normalized family name
                    # that was not itself present in source inputs.
                    entries[name] = {"tier": 4, "sources": ["legacy"]}
            ordered = legacy_order + [name for name in ordered if name not in legacy_order]
            legacy_two = legacy_order[:2]
            legacy_weights[symbol] = tuple(
                (name, (1.0 if index == 0 else 0.6) * (0.18 if is_generic_plate(name) else 1.0))
                for index, name in enumerate(legacy_two)
            )

        output[symbol] = tuple(
            PlateMembership(
                symbol=symbol, plate=name, tier=entries[name]["tier"], rank_in_stock=index,
                source=",".join(entries[name]["sources"]), generic=is_generic_plate(name),
                hard_generic=is_hard_generic_plate(name),
                kind=classify_plate_kind(name),
            )
            for index, name in enumerate(ordered, start=1)
        )
    return _result(output, policy, legacy_weights)


def apply_hierarchy(
    memberships: StockPlateMemberships, hierarchy: PlateHierarchy,
) -> StockPlateMemberships:
    """Attach ancestor paths to memberships; truncate and flag hierarchy cycles."""
    parents = {
        normalize_plate_name(child): normalize_plate_name(parent)
        for child, parent in hierarchy.parent_by_child.items()
        if normalize_plate_name(child) and normalize_plate_name(parent)
    }
    updated: dict[str, tuple[PlateMembership, ...]] = {}
    for symbol, rows in memberships.by_symbol.items():
        result_rows: list[PlateMembership] = []
        for row in rows:
            ancestors: list[str] = []
            seen = {row.plate}
            current = row.plate
            cycle = False
            while current in parents:
                parent = parents[current]
                if parent in seen:
                    cycle = True
                    break
                ancestors.append(parent)
                seen.add(parent)
                current = parent
            flags = tuple(dict.fromkeys((*row.flags, *(('cycle',) if cycle else ()))))
            result_rows.append(replace(row, ancestors=tuple(ancestors), flags=flags))
        updated[symbol] = tuple(result_rows)
    return _result(updated, memberships.policy, memberships.legacy_weights)


def default_alias_hierarchy() -> PlateHierarchy:
    """Return the static child-to-parent aliases encoded by PlateRulesV1."""
    parents: dict[str, str] = {}
    for child, parent in PRIMARY_PLATE_ALIAS_EXACT.items():
        child_name, parent_name = normalize_plate_name(child), normalize_plate_name(parent)
        if child_name and parent_name and child_name != parent_name:
            parents[child_name] = parent_name
    for keyword, parent in PRIMARY_PLATE_ALIAS_KEYWORDS:
        child_name, parent_name = normalize_plate_name(keyword), normalize_plate_name(parent)
        if child_name and parent_name and child_name != parent_name:
            parents.setdefault(child_name, parent_name)
    return PlateHierarchy(parents, {child: "static_alias" for child in parents})


def members_of(
    plate: str, memberships: StockPlateMemberships, hierarchy: PlateHierarchy,
) -> set[str]:
    """Return distinct symbols in a plate or any descendant plate."""
    target = normalize_plate_name(plate)
    if not target:
        return set()
    children_by_parent: dict[str, set[str]] = {}
    for child, parent in hierarchy.parent_by_child.items():
        child_name, parent_name = normalize_plate_name(child), normalize_plate_name(parent)
        if child_name and parent_name:
            children_by_parent.setdefault(parent_name, set()).add(child_name)
    accepted = {target}
    pending = [target]
    while pending:
        parent = pending.pop()
        for child in children_by_parent.get(parent, ()):
            if child not in accepted:
                accepted.add(child)
                pending.append(child)
    return {
        symbol for symbol, rows in memberships.by_symbol.items()
        if any(row.plate in accepted for row in rows)
    }
