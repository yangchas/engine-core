"""Pure parsers and model for the uploaded Kaipan static plate relation."""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping, Sequence

from .plate_membership import PlateHierarchy
from .plate_rules import normalize_plate_name


@dataclass(frozen=True)
class StaticRelation:
    by_symbol: Mapping[str, tuple[str, ...]]
    hierarchy: PlateHierarchy
    plate_names: Mapping[str, str]
    unnamed_codes: tuple[str, ...]
    content_hash: str
    asof: None = None
    flags: tuple[str, ...] = ("STATIC_ASOF_UNKNOWN",)


def _text(value: str | bytes) -> str:
    return value.decode("utf-8-sig", errors="replace") if isinstance(value, bytes) else value


def _code(value: Any) -> str:
    result = str(value or "").strip()
    return result if re.fullmatch(r"\d{6}", result) else ""


def _symbol(value: Any) -> str:
    result = str(value or "").strip()
    if not re.fullmatch(r"\d+", result):
        return ""
    return result.zfill(6) if len(result) <= 6 else ""


def parse_stock_plate_csv(
    text: str | bytes, *, counts: MutableMapping[str, int] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Parse plateid/stockid pairs, normalizing symbols and removing duplicates."""
    reader = csv.DictReader(io.StringIO(_text(text), newline=""))
    members: dict[str, set[str]] = {}
    rows = discarded = duplicates = 0
    for row in reader:
        rows += 1
        plate, symbol = _code(row.get("plateid")), _symbol(row.get("stockid"))
        if not plate or not symbol:
            discarded += 1
            continue
        bucket = members.setdefault(plate, set())
        if symbol in bucket:
            duplicates += 1
        bucket.add(symbol)
    if counts is not None:
        counts.update(rows=rows, discarded_rows=discarded, duplicate_pairs=duplicates)
    return {plate: tuple(sorted(symbols)) for plate, symbols in sorted(members.items())}


def parse_plate_inner_edges(
    text: str | bytes, *, counts: MutableMapping[str, int] | None = None,
) -> list[tuple[str, str]]:
    """Read parent/child code edges from the CSV's Python-literal inner column."""
    reader = csv.reader(io.StringIO(_text(text), newline=""))
    header = next(reader, [])
    inner_index = header.index("inner") if "inner" in header else len(header) - 1
    edges: set[tuple[str, str]] = set()
    rows = invalid = 0
    for row in reader:
        if not row:
            continue
        rows += 1
        parent = _code(row[0] if row else "")
        try:
            inner = ast.literal_eval(row[inner_index])
        except (ValueError, SyntaxError, IndexError):
            invalid += 1
            continue
        if not parent or not isinstance(inner, (list, tuple)):
            continue
        for item in inner:
            if isinstance(item, (list, tuple)) and item:
                child = _code(item[0])
                if child and child != parent:
                    edges.add((parent, child))
    if counts is not None:
        counts.update(rows=rows, invalid_literal_rows=invalid)
    return sorted(edges)


def build_static_relation(
    relation_by_plate: Mapping[str, Sequence[str]], names: Mapping[str, str],
    edges: Sequence[Sequence[str]], *, unnamed_codes: Sequence[str] = (),
) -> StaticRelation:
    """Build normalized direct memberships and code-derived hierarchy facts."""
    unnamed = tuple(sorted({_code(code) for code in unnamed_codes if _code(code)}))
    unnamed_set = set(unnamed)
    normalized_names: dict[str, str] = {}
    for raw_code, raw_name in names.items():
        code = _code(raw_code)
        name = normalize_plate_name(raw_name)
        if code and name and code not in unnamed_set:
            normalized_names[code] = name

    all_codes = {_code(code) for code in relation_by_plate}
    all_codes.discard("")
    for edge in edges:
        if len(edge) >= 2:
            all_codes.update(code for code in (_code(edge[0]), _code(edge[1])) if code)
    plate_names = {code: normalized_names.get(code, f"P{code}") for code in sorted(all_codes)}
    for code in unnamed:
        plate_names[code] = f"P{code}"

    by_symbol: dict[str, set[str]] = {}
    for raw_code, symbols in relation_by_plate.items():
        code = _code(raw_code)
        if not code:
            continue
        name = plate_names.get(code, f"P{code}")
        for raw_symbol in symbols:
            symbol = _symbol(raw_symbol)
            if symbol:
                by_symbol.setdefault(symbol, set()).add(name)

    parent_by_child: dict[str, str] = {}
    source_by_child: dict[str, str] = {}
    # Stable input order resolves duplicate child edges; caller orders preferred evidence first.
    for edge in edges:
        if len(edge) < 2:
            continue
        parent_code, child_code = _code(edge[0]), _code(edge[1])
        parent, child = plate_names.get(parent_code), plate_names.get(child_code)
        if parent and child and parent != child:
            parent_by_child.setdefault(child, parent)
            source_by_child.setdefault(child, "kaipan_inner")

    payload = {
        "by_symbol": {symbol: sorted(names) for symbol, names in sorted(by_symbol.items())},
        "hierarchy": dict(sorted(parent_by_child.items())),
        "plate_names": dict(sorted(plate_names.items())),
        "unnamed_codes": list(unnamed),
    }
    content_hash = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return StaticRelation(
        by_symbol={symbol: tuple(sorted(names)) for symbol, names in sorted(by_symbol.items())},
        hierarchy=PlateHierarchy(parent_by_child, source_by_child),
        plate_names=plate_names, unnamed_codes=unnamed, content_hash=content_hash,
    )


def static_relation_to_dict(relation: StaticRelation) -> dict[str, Any]:
    return {
        "by_symbol": {symbol: list(names) for symbol, names in sorted(relation.by_symbol.items())},
        "hierarchy": {
            "source": "kaipan_inner",
            "parent_by_child": dict(sorted(relation.hierarchy.parent_by_child.items())),
            "source_by_child": dict(sorted(relation.hierarchy.source_by_child.items())),
        },
        "plate_names": dict(sorted(relation.plate_names.items())),
        "unnamed_codes": list(relation.unnamed_codes),
        "content_hash": relation.content_hash,
        "asof": relation.asof,
        "flags": list(relation.flags),
        "stats": {
            "symbol_count": len(relation.by_symbol),
            "member_pair_count": sum(map(len, relation.by_symbol.values())),
            "plate_count": len(relation.plate_names),
            "hierarchy_edge_count": len(relation.hierarchy.parent_by_child),
            "unnamed_code_count": len(relation.unnamed_codes),
        },
    }


__all__ = [
    "StaticRelation", "build_static_relation", "parse_plate_inner_edges",
    "parse_stock_plate_csv", "static_relation_to_dict",
]
