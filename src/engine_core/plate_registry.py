"""Pure, deterministic registry and compact relation-array helpers for plates."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from .plate_kind import classify_plate_kind
from .plate_rules import is_generic_plate


_VERIFIED_PLATE_NAMES = {"801827": "AI视频"}
_VERIFIED_PLATE_ALIASES = {"801827": ["AI应用(AI视频)"]}


def _family_representatives(
    members_by_plate: Mapping[str, Collection[str]],
) -> dict[str, str]:
    codes = sorted(str(code) for code in members_by_plate)
    member_sets = {code: set(map(str, members_by_plate[code])) for code in codes}
    eligible = {code for code in codes if len(member_sets[code]) >= 5}
    parent = {code: code for code in eligible}

    def find(code: str) -> str:
        while parent[code] != code:
            parent[code] = parent[parent[code]]
            code = parent[code]
        return code

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            # Root selection is stable; the public representative is chosen below.
            if left_root < right_root:
                parent[right_root] = left_root
            else:
                parent[left_root] = right_root

    inverted: dict[str, list[str]] = defaultdict(list)
    for code in sorted(eligible):
        for symbol in sorted(member_sets[code]):
            inverted[symbol].append(code)

    candidates: set[tuple[str, str]] = set()
    for sharing_codes in inverted.values():
        for i, left in enumerate(sharing_codes):
            for right in sharing_codes[i + 1:]:
                candidates.add((left, right))

    for left, right in sorted(candidates):
        shared = len(member_sets[left] & member_sets[right])
        union_size = len(member_sets[left]) + len(member_sets[right]) - shared
        if union_size and shared / union_size >= 0.8:
            union(left, right)

    groups: dict[str, list[str]] = defaultdict(list)
    for code in sorted(eligible):
        groups[find(code)].append(code)
    representative_by_root = {
        root: min(group, key=lambda code: (-len(member_sets[code]), code))
        for root, group in groups.items()
    }
    return {
        code: representative_by_root[find(code)] if code in eligible else code
        for code in codes
    }


def build_registry(
    members_by_plate: Mapping[str, Collection[str]],
    names: Mapping[str, str | None],
    parent_of: Mapping[str, str] | None = None,
    *,
    generic_names: Collection[str] | None = None,
) -> dict[str, Any]:
    """Build stable plate records; ``parent_of`` maps child code to parent code."""
    parent_map = {str(child): str(parent) for child, parent in (parent_of or {}).items()}
    codes = set(map(str, members_by_plate)) | set(map(str, names))
    for child, parent in parent_map.items():
        codes.update((child, parent))
    sizes = {code: (len(set(map(str, members_by_plate[code]))) if code in members_by_plate else None)
             for code in codes}
    children_by_parent: dict[str, list[str]] = defaultdict(list)
    for child, parent in parent_map.items():
        if child != parent:
            children_by_parent[parent].append(child)
    generic = set(generic_names or ())
    family_by_code = _family_representatives(members_by_plate)

    rows = []
    for code in sorted(codes):
        name = names.get(code) or _VERIFIED_PLATE_NAMES.get(code)
        if name is not None:
            name = str(name)
        size = sizes[code]
        children = sorted(set(children_by_parent.get(code, ())))
        flags: list[str] = []
        if name is not None and classify_plate_kind(name) in {"EVENT", "NOISE"}:
            flags.append("EVENT")
        if name is not None and (is_generic_plate(name) or name in generic):
            flags.append("GENERIC_FACTOR")
        # At least 300 members is the explicit broad-industry cut. Parent rows
        # with children qualify at 150; large themes never become NICHE.
        if size is not None and (size >= 300 or (bool(children) and size >= 150)):
            flags.append("BROAD")
        if size is not None and size <= 15 and "BROAD" not in flags:
            flags.append("NICHE")
        priority = ("EVENT", "GENERIC_FACTOR", "BROAD", "NICHE", "THEME")
        plate_class = (next((candidate for candidate in priority if candidate in flags), "THEME")
                       if name is not None else "UNKNOWN")
        if plate_class == "UNKNOWN":
            flags.append("UNKNOWN")
        rows.append({
            "code": code,
            "name": name,
            "aliases": list(_VERIFIED_PLATE_ALIASES.get(code, ())),
            "size": size,
            "class": plate_class,
            "parent": parent_map.get(code),
            "children": children,
            "family": family_by_code.get(code, code),
            "flags": flags,
        })
    return {"plates": rows}


def build_relation_arrays(
    members_by_plate: Mapping[str, Collection[str]],
    plate_order: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return sorted symbol/plate dictionaries and lexicographically ordered pairs."""
    available = set(map(str, members_by_plate))
    if plate_order is None:
        plates = sorted(available)
    else:
        requested = list(dict.fromkeys(map(str, plate_order)))
        plates = [code for code in requested if code in available]
        plates.extend(sorted(available - set(plates)))
    symbols = sorted({str(symbol) for values in members_by_plate.values() for symbol in values})
    symbol_index = {symbol: index for index, symbol in enumerate(symbols)}
    plate_index = {code: index for index, code in enumerate(plates)}
    pairs = sorted(
        (symbol_index[str(symbol)], plate_index[code])
        for code in plates
        for symbol in set(map(str, members_by_plate[code]))
    )
    return {"symbols": symbols, "plates": plates, "pairs": pairs}


def plate_degree_by_symbol(
    members_by_plate: Mapping[str, Collection[str]],
) -> dict[str, int]:
    """Count distinct associated plates for every symbol."""
    by_symbol: dict[str, set[str]] = defaultdict(set)
    for raw_code, members in members_by_plate.items():
        code = str(raw_code)
        for raw_symbol in members:
            by_symbol[str(raw_symbol)].add(code)
    return {symbol: len(codes) for symbol, codes in sorted(by_symbol.items())}


__all__ = ["build_registry", "build_relation_arrays", "plate_degree_by_symbol"]
