"""Pure theme-level aggregation for the first Gate-B theme fact slice.

The legacy consumer resolves theme names from several mutable objects and
silently treats missing numeric values as zero.  Core deliberately accepts a
canonical, explicit boundary instead: callers provide one normalized
0924->0925 row per symbol and an audited symbol-to-theme weight mapping.  This
module only aggregates facts; it does not rank themes, infer a strategy
signal, or claim net capital flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    evidence_hash,
    deep_freeze,
    semantic_hash,
)
from .facts import FactStatus


THEME_AUCTION_DELTA_CONTRACT_VERSION = "ThemeAuctionDeltaFactV1"
_REQUIRED_FIELDS = (
    "amount_yuan",
    "amount_delta_yuan",
    "bid_amount_delta_yuan",
    "change_pct_delta",
    "amount_ratio",
)


@dataclass(frozen=True)
class ThemeAuctionDeltaFact:
    """One theme's weighted, field-quality-aware auction delta facts."""

    theme_id: str
    status: FactStatus
    symbol_count: int
    amount_yuan: Optional[float]
    amount_delta_yuan: Optional[float]
    bid_amount_delta_yuan: Optional[float]
    change_pct_delta_avg: Optional[float]
    amount_ratio_avg: Optional[float]
    positive_delta_count: int
    field_counts: Mapping[str, int]
    missing_fields: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.theme_id, str) or not self.theme_id.strip():
            raise ValueError("theme_id is required")
        if isinstance(self.symbol_count, bool) or self.symbol_count < 1:
            raise ValueError("symbol_count must be positive")
        if isinstance(self.positive_delta_count, bool) or self.positive_delta_count < 0:
            raise ValueError("positive_delta_count must be non-negative")
        field_counts = {str(key): int(value) for key, value in self.field_counts.items()}
        missing_fields = tuple(sorted(set(self.missing_fields)))
        evidence_refs = tuple(sorted(set(self.evidence_refs)))
        object.__setattr__(self, "field_counts", deep_freeze(field_counts))
        object.__setattr__(self, "missing_fields", missing_fields)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        semantic_payload = {
            "contract_version": THEME_AUCTION_DELTA_CONTRACT_VERSION,
            "theme_id": self.theme_id,
            "status": self.status,
            "symbol_count": self.symbol_count,
            "amount_yuan": self.amount_yuan,
            "amount_delta_yuan": self.amount_delta_yuan,
            "bid_amount_delta_yuan": self.bid_amount_delta_yuan,
            "change_pct_delta_avg": self.change_pct_delta_avg,
            "amount_ratio_avg": self.amount_ratio_avg,
            "positive_delta_count": self.positive_delta_count,
            "field_counts": self.field_counts,
            "missing_fields": self.missing_fields,
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
            },
        }
        evidence_payload = {
            "contract_version": THEME_AUCTION_DELTA_CONTRACT_VERSION,
            "theme_id": self.theme_id,
            "evidence_refs": self.evidence_refs,
            "field_lineage": {
                field_name: ("normalized_auction_delta",)
                for field_name in _REQUIRED_FIELDS
            },
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
            },
        }
        object.__setattr__(self, "content_hash", semantic_hash(semantic_payload))
        object.__setattr__(self, "evidence_hash", evidence_hash(evidence_payload))

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "theme_id": self.theme_id,
            "status": self.status,
            "symbol_count": self.symbol_count,
            "amount_yuan": self.amount_yuan,
            "amount_delta_yuan": self.amount_delta_yuan,
            "bid_amount_delta_yuan": self.bid_amount_delta_yuan,
            "change_pct_delta_avg": self.change_pct_delta_avg,
            "amount_ratio_avg": self.amount_ratio_avg,
            "positive_delta_count": self.positive_delta_count,
            "field_counts": self.field_counts,
            "missing_fields": self.missing_fields,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
            "evidence_refs": self.evidence_refs,
        }


def build_theme_auction_delta_facts(
    rows: Iterable[Mapping[str, Any]],
    theme_weights_by_symbol: Mapping[str, Sequence[Tuple[str, float]]],
    *,
    from_tag: str = "0924",
    to_tag: str = "0925",
) -> Tuple[ThemeAuctionDeltaFact, ...]:
    """Aggregate canonical auction-delta rows by explicit theme weights.

    ``rows`` must already be normalized and use the exact field names listed
    in :data:`_REQUIRED_FIELDS`.  Missing values remain missing: a metric is
    emitted only when every contributing symbol has that metric.  This makes
    a partially observed theme explicit instead of silently adding zero.
    """

    if not isinstance(theme_weights_by_symbol, Mapping):
        raise TypeError("theme_weights_by_symbol must be a mapping")
    buckets: dict[str, list[tuple[str, float, Mapping[str, Any]]]] = {}
    seen_rows: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TypeError(f"row {index} must be a mapping")
        if str(row.get("tag") or "") != to_tag:
            continue
        if str(row.get("previous_tag") or "") != from_tag:
            continue
        symbol = _required_text(row.get("symbol"), "symbol")
        if symbol in seen_rows:
            raise ValueError("duplicate normalized auction delta row: " + symbol)
        seen_rows.add(symbol)
        weights = theme_weights_by_symbol.get(symbol, ())
        for theme_id, weight in _normalize_weights(weights, symbol=symbol):
            buckets.setdefault(theme_id, []).append((symbol, weight, row))

    facts = tuple(
        _build_theme_fact(theme_id, contributions)
        for theme_id, contributions in sorted(buckets.items())
    )
    return facts


def _build_theme_fact(
    theme_id: str,
    contributions: Sequence[tuple[str, float, Mapping[str, Any]]],
) -> ThemeAuctionDeltaFact:
    field_values: dict[str, list[tuple[float, float]]] = {field_name: [] for field_name in _REQUIRED_FIELDS}
    evidence_refs: set[str] = set()
    positive_delta_count = 0
    for symbol, weight, row in contributions:
        del symbol
        evidence_ref = row.get("evidence_ref")
        if evidence_ref is not None:
            evidence_refs.add(_required_text(evidence_ref, "evidence_ref"))
        for field_name in _REQUIRED_FIELDS:
            value = _finite_number(row.get(field_name))
            if value is not None:
                field_values[field_name].append((value, weight))
        delta = _finite_number(row.get("amount_delta_yuan"))
        if delta is not None and delta > 0:
            positive_delta_count += 1

    symbol_count = len(contributions)
    field_counts = {field_name: len(values) for field_name, values in field_values.items()}
    missing_fields = tuple(
        field_name
        for field_name in _REQUIRED_FIELDS
        if field_counts[field_name] != symbol_count
    )
    values = {
        "amount_yuan": _complete_value(
            field_values["amount_yuan"], symbol_count, _weighted_sum
        ),
        "amount_delta_yuan": _complete_value(
            field_values["amount_delta_yuan"], symbol_count, _weighted_sum
        ),
        "bid_amount_delta_yuan": _complete_value(
            field_values["bid_amount_delta_yuan"], symbol_count, _weighted_sum
        ),
        "change_pct_delta_avg": _complete_value(
            field_values["change_pct_delta"], symbol_count, _weighted_average
        ),
        "amount_ratio_avg": _complete_value(
            field_values["amount_ratio"], symbol_count, _weighted_positive_average
        ),
    }
    if missing_fields:
        status = FactStatus.PARTIAL
    else:
        status = FactStatus.READY
    return ThemeAuctionDeltaFact(
        theme_id=theme_id,
        status=status,
        symbol_count=symbol_count,
        positive_delta_count=positive_delta_count,
        field_counts=field_counts,
        missing_fields=missing_fields,
        evidence_refs=tuple(sorted(evidence_refs)),
        **values,
    )


def _normalize_weights(
    weights: Sequence[Tuple[str, float]],
    *,
    symbol: str,
) -> Tuple[Tuple[str, float], ...]:
    if isinstance(weights, (str, bytes)):
        raise TypeError(f"theme weights for {symbol} must be a sequence")
    normalized: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in weights:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError(f"theme weight for {symbol} must be (theme_id, weight)")
        theme_id = _required_text(item[0], "theme_id")
        if theme_id in seen:
            raise ValueError(f"duplicate theme weight for {symbol}: {theme_id}")
        weight = _finite_number(item[1])
        if weight is None or weight <= 0:
            raise ValueError(f"theme weight for {symbol} must be positive and finite")
        seen.add(theme_id)
        normalized.append((theme_id, weight))
    return tuple(normalized)


def _weighted_sum(values: Sequence[tuple[float, float]]) -> Optional[float]:
    if not values:
        return None
    return sum(value * weight for value, weight in values)


def _weighted_average(values: Sequence[tuple[float, float]]) -> Optional[float]:
    if not values:
        return None
    total_weight = sum(weight for _, weight in values)
    return sum(value * weight for value, weight in values) / total_weight


def _weighted_positive_average(values: Sequence[tuple[float, float]]) -> Optional[float]:
    positive = tuple(item for item in values if item[0] > 0)
    return _weighted_average(positive)


def _complete_value(
    values: Sequence[tuple[float, float]],
    expected_count: int,
    reducer: Any,
) -> Optional[float]:
    if len(values) != expected_count:
        return None
    return reducer(values)


def _finite_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field_name + " must be a non-empty string")
    return value.strip()


__all__ = [
    "THEME_AUCTION_DELTA_CONTRACT_VERSION",
    "ThemeAuctionDeltaFact",
    "build_theme_auction_delta_facts",
]
