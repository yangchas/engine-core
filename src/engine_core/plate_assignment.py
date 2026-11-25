"""Build compact, deterministic premarket stock-to-plate feature sets."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from math import log
from typing import Any


@dataclass(frozen=True)
class FeatureEvidence:
    """Evidence known before the conclusion time; callers enforce point-in-time use."""

    yesterday_pool_plates: Mapping[str, str] = field(default_factory=dict)
    recent_reason_plates: Mapping[str, list[tuple[str, int]]] = field(default_factory=dict)
    leader_tags: Mapping[tuple[str, str], str] = field(default_factory=dict)
    hot_prior_rank: Mapping[str, int] = field(default_factory=dict)


@dataclass
class StockFeatureSet:
    symbol: str
    plates: list[dict[str, Any]]
    primary: str | None
    primary_source: str | None
    generic_plates: list[str]
    degree_total: int
    flags: list[str] = field(default_factory=list)


def _evidence_value(evidence: Any, key: str) -> Mapping[Any, Any]:
    if evidence is None:
        return {}
    if isinstance(evidence, Mapping):
        value = evidence.get(key, {})
    else:
        value = getattr(evidence, key, {})
    return value or {}


def build_feature_sets(
    registry: Mapping[str, Any],
    members_by_plate: Mapping[str, Collection[str]],
    evidence: FeatureEvidence | Mapping[str, Any] | None,
    *,
    max_per_stock: int = 3,
    min_size: int = 10,
) -> dict[str, StockFeatureSet]:
    """Build up to ``max_per_stock`` scored plates per member symbol in O(nnz)."""
    rows = registry.get("plates", ())
    plate_rows = {str(row["code"]): row for row in rows}
    pool_by_symbol = _evidence_value(evidence, "yesterday_pool_plates")
    reasons_by_symbol = _evidence_value(evidence, "recent_reason_plates")
    leader_tags = _evidence_value(evidence, "leader_tags")
    hot_prior_rank = _evidence_value(evidence, "hot_prior_rank")
    evidence_missing = evidence is None

    symbols_by_plate: dict[str, set[str]] = {}
    plates_by_symbol: dict[str, list[str]] = defaultdict(list)
    for raw_code, raw_symbols in members_by_plate.items():
        code = str(raw_code)
        symbols = set(map(str, raw_symbols))
        symbols_by_plate[code] = symbols
        for symbol in symbols:
            plates_by_symbol[symbol].append(code)

    result: dict[str, StockFeatureSet] = {}
    allowed_classes = {"THEME", "NICHE", "BROAD"}
    for symbol in sorted(plates_by_symbol):
        all_codes = sorted(plates_by_symbol[symbol])
        generic_codes: list[str] = []
        candidate_codes: list[str] = []
        for code in all_codes:
            row = plate_rows.get(code)
            if row is None:
                continue
            plate_class = str(row.get("class", "THEME"))
            if plate_class in {"GENERIC_FACTOR", "EVENT"}:
                generic_codes.append(code)
                continue
            if plate_class not in allowed_classes:
                continue
            has_evidence = (
                pool_by_symbol.get(symbol) == code
                or any(str(item[0]) == code for item in reasons_by_symbol.get(symbol, ()))
                or bool(leader_tags.get((code, symbol), ""))
                or (code in hot_prior_rank and int(hot_prior_rank[code]) <= 20)
            )
            size = int(row.get("size", len(symbols_by_plate.get(code, ()))))
            if size >= min_size or (plate_class == "NICHE" and size < min_size and has_evidence):
                candidate_codes.append(code)

        # A candidate descendant supersedes each candidate ancestor for this symbol.
        candidate_set = set(candidate_codes)
        for code in candidate_codes:
            parent = plate_rows[code].get("parent")
            seen: set[str] = set()
            while parent is not None:
                parent_code = str(parent)
                if parent_code in candidate_set:
                    candidate_set.discard(parent_code)
                if parent_code in seen or parent_code not in plate_rows:
                    break
                seen.add(parent_code)
                parent = plate_rows[parent_code].get("parent")

        scored: list[dict[str, Any]] = []
        for code in sorted(candidate_set):
            row = plate_rows[code]
            size = int(row.get("size", len(symbols_by_plate.get(code, ()))))
            specificity = 1.0 / log(max(size, 1) + 1.0)
            tags = ["STATIC"]
            score_evidence = 1.0
            if pool_by_symbol.get(symbol) == code:
                tags.append("POOL")
                score_evidence += 3.0
            reason_days = [
                int(item[1])
                for item in reasons_by_symbol.get(symbol, ())
                if len(item) >= 2 and str(item[0]) == code
            ]
            if reason_days:
                tags.append("REASON")
                score_evidence += 2.0 * (0.9 ** min(reason_days))
            if leader_tags.get((code, symbol), ""):
                tags.append("LEADER")
                score_evidence += 1.5
            if code in hot_prior_rank and int(hot_prior_rank[code]) <= 20:
                tags.append("HOT_PRIOR")
                score_evidence += 0.2
            scored.append({
                "code": code,
                "name": row.get("name"),
                "score": specificity * score_evidence,
                "reasons": tags,
                "size": size,
            })

        # Family deduplication retains highest score, then the lowest code.
        best_by_family: dict[str, dict[str, Any]] = {}
        for item in scored:
            family = str(plate_rows[item["code"]].get("family") or item["code"])
            current = best_by_family.get(family)
            if current is None or (-item["score"], item["code"]) < (-current["score"], current["code"]):
                best_by_family[family] = item
        ordered = sorted(best_by_family.values(), key=lambda item: (-item["score"], item["code"]))
        if max_per_stock >= 0:
            ordered = ordered[:max_per_stock]
        primary = ordered[0]["code"] if ordered else None
        if primary is None:
            primary_source = None
        elif pool_by_symbol.get(symbol) == primary:
            primary_source = "POOL"
        elif any(str(item[0]) == primary for item in reasons_by_symbol.get(symbol, ())):
            primary_source = "REASON"
        else:
            primary_source = "STATIC"
        flags: list[str] = []
        if evidence_missing:
            flags.append("EVIDENCE_MISSING")
        if not ordered:
            flags.append("NO_CANDIDATE")
        result[symbol] = StockFeatureSet(
            symbol=symbol,
            plates=ordered,
            primary=primary,
            primary_source=primary_source,
            generic_plates=generic_codes,
            degree_total=len(all_codes),
            flags=flags,
        )
    return result


def summarize_feature_sets(sets: Mapping[str, StockFeatureSet]) -> dict[str, Any]:
    """Return stable aggregate counts for a feature-set mapping."""
    symbols = len(sets)
    primary_source_counts: dict[str, int] = {}
    with_primary = 0
    no_candidate = 0
    evidence_missing = 0
    total_plates = 0
    for item in sets.values():
        total_plates += len(item.plates)
        if item.primary is not None:
            with_primary += 1
            source = item.primary_source or "None"
            primary_source_counts[source] = primary_source_counts.get(source, 0) + 1
        if "NO_CANDIDATE" in item.flags:
            no_candidate += 1
        if "EVIDENCE_MISSING" in item.flags:
            evidence_missing += 1
    return {
        "symbols": symbols,
        "with_primary": with_primary,
        "primary_source_counts": dict(sorted(primary_source_counts.items())),
        "avg_plates": total_plates / symbols if symbols else 0.0,
        "no_candidate": no_candidate,
        "evidence_missing": evidence_missing,
    }


__all__ = ["FeatureEvidence", "StockFeatureSet", "build_feature_sets", "summarize_feature_sets"]
