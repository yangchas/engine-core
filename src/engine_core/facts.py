"""Small pure fact functions for the first 09:20 -> 09:24 slice."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Dict, Mapping, Optional, Tuple

from .contracts import EngineSnapshot, FrozenDataBundle, canonical_hash


class FactStatus(str, Enum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class PriceFacts:
    status: FactStatus
    start_price_milli: Optional[int]
    end_price_milli: Optional[int]
    high_price_milli: Optional[int]
    low_price_milli: Optional[int]
    return_bp: Optional[int]
    field_lineage: Mapping[str, Tuple[str, ...]]


@dataclass(frozen=True)
class VolumeFacts:
    status: FactStatus
    amount_delta_native: Optional[float]
    volume_delta_native: Optional[float]
    field_lineage: Mapping[str, Tuple[str, ...]]


@dataclass(frozen=True)
class OrderBookFacts:
    status: FactStatus
    directional_pressure_native: Optional[float]
    field_lineage: Mapping[str, Tuple[str, ...]]


@dataclass(frozen=True)
class BreadthFacts:
    status: FactStatus
    up_count: Optional[int]
    down_count: Optional[int]
    field_lineage: Mapping[str, Tuple[str, ...]]


@dataclass(frozen=True)
class ThemeFacts:
    status: FactStatus
    participation_ratio: Optional[int]
    field_lineage: Mapping[str, Tuple[str, ...]]


@dataclass(frozen=True)
class DataQuality:
    status: FactStatus
    missing_fields: Tuple[str, ...]
    unavailable_groups: Tuple[str, ...]


@dataclass(frozen=True)
class SegmentFrame:
    segment_id: str
    scope_type: str
    scope_id: str
    start_snapshot_id: str
    end_snapshot_id: str
    price: PriceFacts
    volume: VolumeFacts
    order_book: OrderBookFacts
    breadth: BreadthFacts
    theme: ThemeFacts
    quality: DataQuality
    content_hash: str


@dataclass(frozen=True)
class FactResult:
    fact_id: str
    status: FactStatus
    facts: Any
    snapshot_hash: str
    bundle_hash: str
    evidence_refs: Tuple[str, ...]
    reason_codes: Tuple[str, ...]
    content_hash: str


class SegmentFrameFactFunction:
    """Adapt the pure segment builder to the FactFunction contract."""

    function_id = "segment_frame"

    def __init__(
        self,
        segment_id: str,
        start_snapshot: EngineSnapshot,
        *,
        scope_type: str,
        scope_id: str,
        amount_semantics: str = "UNKNOWN",
        volume_semantics: str = "UNKNOWN",
    ) -> None:
        self.segment_id = segment_id
        self.start_snapshot = start_snapshot
        self.scope_type = scope_type
        self.scope_id = scope_id
        self.amount_semantics = amount_semantics
        self.volume_semantics = volume_semantics

    def evaluate(
        self,
        snapshot: EngineSnapshot,
        bundle: FrozenDataBundle,
    ) -> FactResult:
        frame = build_segment_frame(
            self.segment_id,
            self.start_snapshot,
            snapshot,
            scope_type=self.scope_type,
            scope_id=self.scope_id,
            amount_semantics=self.amount_semantics,
            volume_semantics=self.volume_semantics,
        )
        return FactResult(
            fact_id=self.segment_id,
            status=frame.quality.status,
            facts=frame,
            snapshot_hash=snapshot.content_hash,
            bundle_hash=bundle.content_hash,
            evidence_refs=(
                self.start_snapshot.snapshot_id,
                snapshot.snapshot_id,
            ),
            reason_codes=frame.quality.missing_fields,
            content_hash=frame.content_hash,
        )


@dataclass(frozen=True)
class SegmentComparison:
    previous_segment_id: str
    current_segment_id: str
    price_change: str
    volume_change: str
    order_book_change: str
    breadth_change: str
    theme_change: str
    reason_codes: Tuple[str, ...]
    evidence_refs: Tuple[str, ...]
    content_hash: str


def build_segment_frame(
    segment_id: str,
    start_snapshot: EngineSnapshot,
    end_snapshot: EngineSnapshot,
    *,
    scope_type: str,
    scope_id: str,
    amount_semantics: str = "UNKNOWN",
    volume_semantics: str = "UNKNOWN",
) -> SegmentFrame:
    """Build facts for one symbol segment without strategy interpretation.

    The first vertical slice supports ``scope_type="SYMBOL"`` only.  Market,
    theme and other scopes must get an explicit unavailable result until their
    aggregation semantics are implemented by a dedicated fact function.
    """

    if scope_type != "SYMBOL":
        return _unavailable_frame(
            segment_id,
            scope_type,
            scope_id,
            start_snapshot,
            end_snapshot,
        )

    start_values = start_snapshot.symbol_states.get(scope_id)
    end_values = end_snapshot.symbol_states.get(scope_id)
    start_ref = start_snapshot.snapshot_id
    end_ref = end_snapshot.snapshot_id
    if start_values is None or end_values is None:
        missing = ("symbol_state",)
        price = PriceFacts(
            FactStatus.MISSING,
            None,
            None,
            None,
            None,
            None,
            {"symbol_state": (start_ref, end_ref)},
        )
        volume = VolumeFacts(
            FactStatus.MISSING,
            None,
            None,
            {"symbol_state": (start_ref, end_ref)},
        )
        order_book = OrderBookFacts(
            FactStatus.MISSING,
            None,
            {"symbol_state": (start_ref, end_ref)},
        )
        quality = DataQuality(
            FactStatus.MISSING,
            missing,
            ("breadth", "theme"),
        )
        return _frame(
            segment_id,
            scope_type,
            scope_id,
            start_snapshot,
            end_snapshot,
            price,
            volume,
            order_book,
            _unavailable_breadth(start_ref, end_ref),
            _unavailable_theme(start_ref, end_ref),
            quality,
        )

    price_start = _as_int(start_values.get("price_milli"))
    price_end = _as_int(end_values.get("price_milli"))
    if price_start is None or price_end is None:
        price_status = FactStatus.MISSING
        return_bp = None
    elif price_start <= 0 or price_end <= 0:
        price_status = FactStatus.INVALID
        return_bp = None
    else:
        price_status = FactStatus.READY
        return_bp = ((price_end - price_start) * 10_000) // price_start
    price = PriceFacts(
        price_status,
        price_start,
        price_end,
        None,
        None,
        return_bp,
        {"start_price_milli": (start_ref,), "end_price_milli": (end_ref,)},
    )

    amount_start = _as_float(start_values.get("amount_native"))
    amount_end = _as_float(end_values.get("amount_native"))
    volume_start = _as_float(start_values.get("volume_native"))
    volume_end = _as_float(end_values.get("volume_native"))
    amount_delta, amount_status = _delta(
        amount_start,
        amount_end,
        amount_semantics,
    )
    volume_delta, volume_status_value = _delta(
        volume_start,
        volume_end,
        volume_semantics,
    )
    volume_status = _combine_status(amount_status, volume_status_value)
    volume = VolumeFacts(
        volume_status,
        amount_delta,
        volume_delta,
        {"amount_native": (start_ref, end_ref), "volume_native": (start_ref, end_ref)},
    )

    bid_start = _as_float(start_values.get("auction_bid_amount_native"))
    ask_start = _as_float(start_values.get("auction_ask_amount_native"))
    bid_end = _as_float(end_values.get("auction_bid_amount_native"))
    ask_end = _as_float(end_values.get("auction_ask_amount_native"))
    pressure_start = (
        bid_start - ask_start
        if bid_start is not None and ask_start is not None
        else None
    )
    pressure_end = (
        bid_end - ask_end
        if bid_end is not None and ask_end is not None
        else None
    )
    pressure = pressure_end
    order_status = (
        FactStatus.READY if pressure is not None else FactStatus.UNAVAILABLE
    )
    order_book = OrderBookFacts(
        order_status,
        pressure,
        {"auction_bid/ask": (start_ref, end_ref)},
    )

    breadth = _unavailable_breadth(start_ref, end_ref)
    theme = _unavailable_theme(start_ref, end_ref)
    missing_groups = []
    if price.status in (FactStatus.MISSING, FactStatus.INVALID):
        missing_groups.append("price")
    if volume.status in (FactStatus.MISSING, FactStatus.INVALID):
        missing_groups.append("volume")
    quality_status = FactStatus.READY if not missing_groups else FactStatus.PARTIAL
    unavailable_groups = ["breadth", "theme"]
    if order_book.status is FactStatus.UNAVAILABLE:
        unavailable_groups.append("order_book")
    if volume.status is FactStatus.UNAVAILABLE:
        unavailable_groups.append("volume")
    quality = DataQuality(
        quality_status,
        tuple(missing_groups),
        tuple(unavailable_groups),
    )
    return _frame(
        segment_id,
        scope_type,
        scope_id,
        start_snapshot,
        end_snapshot,
        price,
        volume,
        order_book,
        breadth,
        theme,
        quality,
    )


def compare_segments(
    previous: SegmentFrame,
    current: SegmentFrame,
) -> SegmentComparison:
    """Compare two segment fact frames without choosing a strategy state."""

    reasons = []
    price_change = _compare(
        previous.price.return_bp,
        current.price.return_bp,
        "PRICE_STRONGER",
        "PRICE_WEAKER",
        "PRICE_UNKNOWN",
    )
    if price_change != "PRICE_UNKNOWN":
        reasons.append("price.return_bp")
    volume_change = _compare(
        previous.volume.amount_delta_native,
        current.volume.amount_delta_native,
        "VOLUME_EXPANDING",
        "VOLUME_CONTRACTING",
        "VOLUME_UNKNOWN",
    )
    if volume_change != "VOLUME_UNKNOWN":
        reasons.append("volume.amount_delta_native")
    order_change = _compare(
        previous.order_book.directional_pressure_native,
        current.order_book.directional_pressure_native,
        "PRESSURE_IMPROVING",
        "PRESSURE_WEAKENING",
        "PRESSURE_UNKNOWN",
    )
    if order_change != "PRESSURE_UNKNOWN":
        reasons.append("order_book.directional_pressure_native")
    comparison = {
        "previous_segment_id": previous.segment_id,
        "current_segment_id": current.segment_id,
        "price_change": price_change,
        "volume_change": volume_change,
        "order_book_change": order_change,
        "breadth_change": "BREADTH_UNAVAILABLE",
        "theme_change": "THEME_UNAVAILABLE",
        "reason_codes": tuple(reasons),
        "evidence_refs": (
            previous.content_hash,
            current.content_hash,
        ),
    }
    return SegmentComparison(
        previous_segment_id=previous.segment_id,
        current_segment_id=current.segment_id,
        price_change=price_change,
        volume_change=volume_change,
        order_book_change=order_change,
        breadth_change="BREADTH_UNAVAILABLE",
        theme_change="THEME_UNAVAILABLE",
        reason_codes=tuple(reasons),
        evidence_refs=(previous.content_hash, current.content_hash),
        content_hash=canonical_hash(comparison),
    )


def _frame(
    segment_id: str,
    scope_type: str,
    scope_id: str,
    start_snapshot: EngineSnapshot,
    end_snapshot: EngineSnapshot,
    price: PriceFacts,
    volume: VolumeFacts,
    order_book: OrderBookFacts,
    breadth: BreadthFacts,
    theme: ThemeFacts,
    quality: DataQuality,
) -> SegmentFrame:
    content = {
        "segment_id": segment_id,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "start_snapshot_id": start_snapshot.snapshot_id,
        "end_snapshot_id": end_snapshot.snapshot_id,
        "price": price,
        "volume": volume,
        "order_book": order_book,
        "breadth": breadth,
        "theme": theme,
        "quality": quality,
    }
    return SegmentFrame(
        segment_id=segment_id,
        scope_type=scope_type,
        scope_id=scope_id,
        start_snapshot_id=start_snapshot.snapshot_id,
        end_snapshot_id=end_snapshot.snapshot_id,
        price=price,
        volume=volume,
        order_book=order_book,
        breadth=breadth,
        theme=theme,
        quality=quality,
        content_hash=canonical_hash(content),
    )


def _unavailable_frame(
    segment_id: str,
    scope_type: str,
    scope_id: str,
    start_snapshot: EngineSnapshot,
    end_snapshot: EngineSnapshot,
) -> SegmentFrame:
    start_ref = start_snapshot.snapshot_id
    end_ref = end_snapshot.snapshot_id
    lineage = {"scope": (start_ref, end_ref)}
    price = PriceFacts(FactStatus.UNAVAILABLE, None, None, None, None, None, lineage)
    volume = VolumeFacts(FactStatus.UNAVAILABLE, None, None, lineage)
    order_book = OrderBookFacts(FactStatus.UNAVAILABLE, None, lineage)
    quality = DataQuality(
        FactStatus.UNAVAILABLE,
        (),
        ("price", "volume", "order_book", "breadth", "theme"),
    )
    return _frame(
        segment_id,
        scope_type,
        scope_id,
        start_snapshot,
        end_snapshot,
        price,
        volume,
        order_book,
        _unavailable_breadth(start_ref, end_ref),
        _unavailable_theme(start_ref, end_ref),
        quality,
    )


def _unavailable_breadth(start_ref: str, end_ref: str) -> BreadthFacts:
    return BreadthFacts(
        FactStatus.UNAVAILABLE,
        None,
        None,
        {"scope": (start_ref, end_ref)},
    )


def _unavailable_theme(start_ref: str, end_ref: str) -> ThemeFacts:
    return ThemeFacts(
        FactStatus.UNAVAILABLE,
        None,
        {"scope": (start_ref, end_ref)},
    )


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _delta(
    start: Optional[float],
    end: Optional[float],
    semantics: str,
) -> Tuple[Optional[float], FactStatus]:
    if semantics == "UNKNOWN":
        return None, FactStatus.UNAVAILABLE
    if semantics == "INCREMENTAL":
        return None, FactStatus.UNAVAILABLE
    if semantics != "CUMULATIVE":
        return None, FactStatus.INVALID
    if start is None or end is None:
        return None, FactStatus.MISSING
    delta = end - start
    if delta < 0:
        return None, FactStatus.INVALID
    return delta, FactStatus.READY


def _combine_status(left: FactStatus, right: FactStatus) -> FactStatus:
    rank = {
        FactStatus.READY: 0,
        FactStatus.UNAVAILABLE: 1,
        FactStatus.MISSING: 2,
        FactStatus.PARTIAL: 3,
        FactStatus.INVALID: 4,
    }
    return left if rank[left] >= rank[right] else right


def _compare(
    previous: Optional[float],
    current: Optional[float],
    stronger: str,
    weaker: str,
    unknown: str,
) -> str:
    if previous is None or current is None:
        return unknown
    if current > previous:
        return stronger
    if current < previous:
        return weaker
    return "STABLE"
