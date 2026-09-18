"""Pure normalization of the explicit A2 09:25 market summary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any, Mapping, Optional, Tuple

from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    evidence_hash,
    semantic_hash,
)
from .facts import FactStatus


AUCTION_MARKET_SUMMARY_CONTRACT_VERSION = "AuctionMarketSummaryFactV1"
_STRICT_TRADE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_FIELD_SPECS = (
    ("stock_count", "total_stocks"),
    ("valid_stock_count", "valid_stock_count"),
    ("unavailable_stock_count", "unavailable_stock_count"),
    ("positive_count", "high_open_count"),
    ("negative_count", "low_open_count"),
    ("flat_count", "flat_open_count"),
    ("auction_amount_yuan", "total_auction_amount_yuan"),
    ("limit_up_count", "limit_up_count"),
    ("limit_down_count", "limit_down_count"),
    ("limit_up_seal_amount_yuan", "total_limit_up_bid_amount_yuan"),
)


@dataclass(frozen=True)
class AuctionMarketSummaryFact:
    """Canonical A2 summary; no plate or strategy interpretation."""

    trade_date: str
    status: FactStatus
    source_id: str
    source: str
    source_table: str
    observation_time_ms: Optional[int]
    stock_count: Optional[int]
    valid_stock_count: Optional[int]
    unavailable_stock_count: Optional[int]
    positive_count: Optional[int]
    negative_count: Optional[int]
    flat_count: Optional[int]
    auction_amount_yuan: Optional[int]
    limit_up_count: Optional[int]
    limit_down_count: Optional[int]
    limit_up_seal_amount_yuan: Optional[int]
    missing_fields: Tuple[str, ...] = ()
    invalid_fields: Tuple[str, ...] = ()
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)
    evidence_refs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing_fields", tuple(self.missing_fields))
        object.__setattr__(self, "invalid_fields", tuple(self.invalid_fields))
        object.__setattr__(self, "evidence_refs", tuple(sorted(set(self.evidence_refs))))
        semantic_payload = {
            "contract_version": AUCTION_MARKET_SUMMARY_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "status": self.status,
            "stock_count": self.stock_count,
            "valid_stock_count": self.valid_stock_count,
            "unavailable_stock_count": self.unavailable_stock_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "flat_count": self.flat_count,
            "auction_amount_yuan": self.auction_amount_yuan,
            "limit_up_count": self.limit_up_count,
            "limit_down_count": self.limit_down_count,
            "limit_up_seal_amount_yuan": self.limit_up_seal_amount_yuan,
            "missing_fields": self.missing_fields,
            "invalid_fields": self.invalid_fields,
        }
        evidence_payload = {
            "contract_version": AUCTION_MARKET_SUMMARY_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "source_id": self.source_id,
            "source": self.source,
            "source_table": self.source_table,
            "observation_time_ms": self.observation_time_ms,
            "evidence_refs": self.evidence_refs,
            "field_lineage": {
                field_name: (self.source_table,)
                for field_name, _ in _FIELD_SPECS
            },
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
            },
        }
        object.__setattr__(self, "content_hash", semantic_hash(semantic_payload))
        object.__setattr__(self, "evidence_hash", evidence_hash(evidence_payload))

    def as_mapping(self) -> Mapping[str, Any]:
        """Return canonical names without source-specific raw aliases."""

        return {
            "trade_date": self.trade_date,
            "status": self.status,
            "source_id": self.source_id,
            "source": self.source,
            "source_table": self.source_table,
            "observation_time_ms": self.observation_time_ms,
            "stock_count": self.stock_count,
            "valid_stock_count": self.valid_stock_count,
            "unavailable_stock_count": self.unavailable_stock_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "flat_count": self.flat_count,
            "auction_amount_yuan": self.auction_amount_yuan,
            "limit_up_count": self.limit_up_count,
            "limit_down_count": self.limit_down_count,
            "limit_up_seal_amount_yuan": self.limit_up_seal_amount_yuan,
            "missing_fields": self.missing_fields,
            "invalid_fields": self.invalid_fields,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
            "evidence_refs": self.evidence_refs,
        }


def normalize_auction_market_summary(
    raw: Mapping[str, Any],
    *,
    trade_date: str,
    source_id: str,
    source_table: str = "redis:market:auction:0925",
    observation_time_ms: Optional[int] = None,
    evidence_refs: Tuple[str, ...] = (),
) -> AuctionMarketSummaryFact:
    """Normalize the legacy Redis A2 summary without inventing values.

    The legacy aliases are accepted only at this boundary.  All downstream
    fields use explicit canonical names and yuan/count units.  A missing field
    remains ``None``; an explicit numeric zero is preserved as zero.
    """

    if not isinstance(raw, Mapping):
        raise TypeError("auction market summary raw value must be a mapping")
    _validate_trade_date(trade_date)
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id is required")
    if not isinstance(source_table, str) or not source_table.strip():
        raise ValueError("source_table is required")
    _validate_timestamp("observation_time_ms", observation_time_ms)

    values: dict[str, Optional[int]] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for canonical_name, raw_name in _FIELD_SPECS:
        if raw_name not in raw or raw.get(raw_name) in (None, ""):
            values[canonical_name] = None
            missing.append(canonical_name)
            continue
        parsed = _nonnegative_int(raw.get(raw_name))
        if parsed is None:
            values[canonical_name] = None
            invalid.append(canonical_name)
        else:
            values[canonical_name] = parsed

    if not raw:
        status = FactStatus.UNAVAILABLE
    elif invalid:
        status = FactStatus.INVALID
    elif missing:
        status = FactStatus.PARTIAL
    else:
        status = FactStatus.READY
    return AuctionMarketSummaryFact(
        trade_date=trade_date,
        status=status,
        source_id=source_id.strip(),
        source="a2_0925_summary",
        source_table=source_table,
        observation_time_ms=observation_time_ms,
        **values,
        missing_fields=tuple(missing),
        invalid_fields=tuple(invalid),
        evidence_refs=evidence_refs,
    )


def _validate_trade_date(value: str) -> None:
    if not isinstance(value, str) or not _STRICT_TRADE_DATE.fullmatch(value):
        raise ValueError("trade_date must use strict YYYY-MM-DD form")
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("trade_date must use strict YYYY-MM-DD form")


def _validate_timestamp(name: str, value: Optional[int]) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        raise ValueError(f"{name} must be a positive epoch-millisecond integer")


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and value != number:
        return None
    if isinstance(value, str) and str(number) != value.strip():
        return None
    return number if number >= 0 else None


__all__ = [
    "AUCTION_MARKET_SUMMARY_CONTRACT_VERSION",
    "AuctionMarketSummaryFact",
    "normalize_auction_market_summary",
]
