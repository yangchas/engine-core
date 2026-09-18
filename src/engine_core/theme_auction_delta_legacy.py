"""Explicit compatibility-only aggregation for the legacy theme consumer.

This module is deliberately separate from :mod:`theme_auction_delta`.
``ThemeAuctionDeltaFactV1`` is the Core fact contract: it preserves missing
values and uses explicit weighted averages.  The deployed legacy consumer has
different historical behavior, including zero-filling, dividing weighted
change contributions by symbol count, and averaging positive amount ratios
without weights.  That behavior is useful for differential Shadow work, but
must not silently become the new Core fact definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Mapping, Sequence, Tuple

from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    deep_freeze,
    evidence_hash,
    semantic_hash,
)
from .theme_auction_delta import _normalize_weights


LEGACY_THEME_AUCTION_DELTA_CONTRACT_VERSION = "LegacyThemeAuctionDeltaCompatV1"


@dataclass(frozen=True)
class LegacyThemeAuctionDeltaCompatFact:
    """One old-style theme aggregate, for parity and Shadow only."""

    theme_id: str
    symbol_count: int
    amount_0925: float
    amount_delta_24_25: float
    amount_ratio_avg: float
    bid_amount_delta_24_25: float
    change_pct_delta_avg: float
    positive_delta_count: int
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
        numeric_fields = (
            "amount_0925",
            "amount_delta_24_25",
            "amount_ratio_avg",
            "bid_amount_delta_24_25",
            "change_pct_delta_avg",
        )
        for field_name in numeric_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(field_name + " must be finite")
        refs = tuple(sorted(set(self.evidence_refs)))
        object.__setattr__(self, "evidence_refs", deep_freeze(refs))
        semantic_payload = {
            "contract_version": LEGACY_THEME_AUCTION_DELTA_CONTRACT_VERSION,
            "theme_id": self.theme_id,
            "symbol_count": self.symbol_count,
            "amount_0925": self.amount_0925,
            "amount_delta_24_25": self.amount_delta_24_25,
            "amount_ratio_avg": self.amount_ratio_avg,
            "bid_amount_delta_24_25": self.bid_amount_delta_24_25,
            "change_pct_delta_avg": self.change_pct_delta_avg,
            "positive_delta_count": self.positive_delta_count,
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
            },
        }
        evidence_payload = {
            "contract_version": LEGACY_THEME_AUCTION_DELTA_CONTRACT_VERSION,
            "theme_id": self.theme_id,
            "evidence_refs": self.evidence_refs,
            "field_lineage": {
                "amount_0925": ("normalized_auction_delta",),
                "amount_delta_24_25": ("normalized_auction_delta",),
                "amount_ratio_avg": ("normalized_auction_delta",),
                "bid_amount_delta_24_25": ("normalized_auction_delta",),
                "change_pct_delta_avg": ("normalized_auction_delta",),
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
            "symbol_count": self.symbol_count,
            "amount_0925": self.amount_0925,
            "amount_delta_24_25": self.amount_delta_24_25,
            "amount_ratio_avg": self.amount_ratio_avg,
            "bid_amount_delta_24_25": self.bid_amount_delta_24_25,
            "change_pct_delta_avg": self.change_pct_delta_avg,
            "positive_delta_count": self.positive_delta_count,
            "evidence_refs": self.evidence_refs,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
        }


def build_legacy_theme_auction_delta_compat_facts(
    rows: Iterable[Mapping[str, Any]],
    theme_weights_by_symbol: Mapping[str, Sequence[Tuple[str, float]]],
    *,
    from_tag: str = "0924",
    to_tag: str = "0925",
) -> Tuple[LegacyThemeAuctionDeltaCompatFact, ...]:
    """Reproduce the legacy numeric aggregate without strategy labels.

    This function is for differential tests and fact-only Shadow evidence. It
    intentionally preserves the legacy zero-fill and averaging behavior, so it
    must not replace the conservative Core fact builder.
    """

    if not isinstance(theme_weights_by_symbol, Mapping):
        raise TypeError("theme_weights_by_symbol must be a mapping")
    buckets: dict[str, list[tuple[str, float, Mapping[str, Any]]]] = {}
    seen_symbols: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TypeError(f"row {index} must be a mapping")
        if str(row.get("tag") or "") != to_tag or str(row.get("previous_tag") or "") != from_tag:
            continue
        symbol = _required_text(row.get("symbol"), "symbol")
        if symbol in seen_symbols:
            raise ValueError("duplicate normalized auction delta row: " + symbol)
        seen_symbols.add(symbol)
        for theme_id, weight in _normalize_weights(theme_weights_by_symbol.get(symbol, ()), symbol=symbol):
            buckets.setdefault(theme_id, []).append((symbol, weight, row))

    return tuple(
        _build_legacy_theme_fact(theme_id, contributions)
        for theme_id, contributions in sorted(buckets.items())
    )


def _build_legacy_theme_fact(
    theme_id: str,
    contributions: Sequence[tuple[str, float, Mapping[str, Any]]],
) -> LegacyThemeAuctionDeltaCompatFact:
    amount = amount_delta = bid_delta = change_delta = 0.0
    ratio_sum = 0.0
    ratio_count = 0
    positive_delta_count = 0
    evidence_refs: set[str] = set()
    for symbol, weight, row in contributions:
        del symbol
        amount += _legacy_number(row.get("amount_yuan")) * weight
        delta = _legacy_number(row.get("amount_delta_yuan"))
        amount_delta += delta * weight
        bid_delta += _legacy_number(row.get("bid_amount_delta_yuan")) * weight
        change_delta += _legacy_number(row.get("change_pct_delta")) * weight
        ratio = _legacy_number(row.get("amount_ratio"))
        if ratio > 0:
            ratio_sum += ratio
            ratio_count += 1
        if delta > 0:
            positive_delta_count += 1
        evidence_ref = row.get("evidence_ref")
        if evidence_ref is not None:
            evidence_refs.add(_required_text(evidence_ref, "evidence_ref"))
    symbol_count = len(contributions)
    return LegacyThemeAuctionDeltaCompatFact(
        theme_id=theme_id,
        symbol_count=symbol_count,
        amount_0925=round(amount, 2),
        amount_delta_24_25=round(amount_delta, 2),
        amount_ratio_avg=round(ratio_sum / ratio_count, 4) if ratio_count else 0.0,
        bid_amount_delta_24_25=round(bid_delta, 2),
        change_pct_delta_avg=round(change_delta / symbol_count, 4),
        positive_delta_count=positive_delta_count,
        evidence_refs=tuple(sorted(evidence_refs)),
    )


def _legacy_number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field_name + " must be a non-empty string")
    return value.strip()


__all__ = [
    "LEGACY_THEME_AUCTION_DELTA_CONTRACT_VERSION",
    "LegacyThemeAuctionDeltaCompatFact",
    "build_legacy_theme_auction_delta_compat_facts",
]
