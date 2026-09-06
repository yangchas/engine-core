"""Small pure fact functions for the first 09:20 -> 09:24 slice."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from .contracts import (
    EngineSnapshot,
    deep_freeze,
    evidence_hash as build_evidence_hash,
    semantic_hash,
    trunc_div,
)

Number = Union[int, float]


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_lineage", deep_freeze(self.field_lineage))


@dataclass(frozen=True)
class VolumeFacts:
    status: FactStatus
    amount_delta_yuan: Optional[Number]
    volume_delta_lots: Optional[Number]
    field_lineage: Mapping[str, Tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_lineage", deep_freeze(self.field_lineage))


@dataclass(frozen=True)
class OrderBookFacts:
    status: FactStatus
    directional_pressure_yuan: Optional[Number]
    field_lineage: Mapping[str, Tuple[str, ...]]
    resting_bid_start_yuan: Optional[Number] = None
    resting_ask_start_yuan: Optional[Number] = None
    resting_bid_end_yuan: Optional[Number] = None
    resting_ask_end_yuan: Optional[Number] = None
    pressure_delta_yuan: Optional[Number] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_lineage", deep_freeze(self.field_lineage))


@dataclass(frozen=True)
class BreadthFacts:
    status: FactStatus
    up_count: Optional[int]
    down_count: Optional[int]
    field_lineage: Mapping[str, Tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_lineage", deep_freeze(self.field_lineage))


@dataclass(frozen=True)
class ThemeFacts:
    status: FactStatus
    participation_ratio: Optional[int]
    field_lineage: Mapping[str, Tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_lineage", deep_freeze(self.field_lineage))


@dataclass(frozen=True)
class DataQuality:
    status: FactStatus
    missing_fields: Tuple[str, ...]
    unavailable_groups: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing_fields", tuple(self.missing_fields))
        object.__setattr__(self, "unavailable_groups", tuple(self.unavailable_groups))


@dataclass(frozen=True)
class SegmentFrame:
    segment_id: str
    scope_type: str
    scope_id: str
    start_snapshot_id: str
    end_snapshot_id: str
    start_time_ms: int
    end_time_ms: int
    price: PriceFacts
    volume: VolumeFacts
    order_book: OrderBookFacts
    breadth: BreadthFacts
    theme: ThemeFacts
    quality: DataQuality
    content_hash: str
    coverage_status: str = "UNKNOWN"
    observed_start_time_ms: Optional[int] = None
    observed_end_time_ms: Optional[int] = None
    evidence_refs: Tuple[str, ...] = ()
    evidence_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", deep_freeze(self.price))
        object.__setattr__(self, "volume", deep_freeze(self.volume))
        object.__setattr__(self, "order_book", deep_freeze(self.order_book))
        object.__setattr__(self, "breadth", deep_freeze(self.breadth))
        object.__setattr__(self, "theme", deep_freeze(self.theme))
        object.__setattr__(self, "evidence_refs", tuple(sorted(set(self.evidence_refs))))


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", deep_freeze(self.facts))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))


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
    evidence_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


def build_segment_frame(
    segment_id: str,
    start_snapshot: EngineSnapshot,
    end_snapshot: EngineSnapshot,
    *,
    scope_type: str,
    scope_id: str,
    amount_semantics: str = "UNKNOWN",
    volume_semantics: str = "UNKNOWN",
    coverage_status: str = "UNKNOWN",
    observed_start_time_ms: Optional[int] = None,
    observed_end_time_ms: Optional[int] = None,
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
            coverage_status=coverage_status,
            observed_start_time_ms=observed_start_time_ms,
            observed_end_time_ms=observed_end_time_ms,
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
            coverage_status=coverage_status,
            observed_start_time_ms=observed_start_time_ms,
            observed_end_time_ms=observed_end_time_ms,
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
        return_bp = trunc_div((price_end - price_start) * 10_000, price_start)
    price = PriceFacts(
        price_status,
        price_start,
        price_end,
        None,
        None,
        return_bp,
        {"start_price_milli": (start_ref,), "end_price_milli": (end_ref,)},
    )

    # Auction segments use the matched auction amount, not the intraday
    # cumulative trading amount.  There is deliberately no fallback here:
    # ``amount_yuan`` and ``auction_amount_yuan`` have different semantics.
    amount_start = _as_number(start_values.get("auction_amount_yuan"))
    amount_end = _as_number(end_values.get("auction_amount_yuan"))
    volume_start = _as_number(start_values.get("volume_lots"))
    volume_end = _as_number(end_values.get("volume_lots"))
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
        {"auction_amount_yuan": (start_ref, end_ref), "volume_lots": (start_ref, end_ref)},
    )

    bid_start = _as_number(start_values.get("auction_bid_amount_yuan"))
    ask_start = _as_number(start_values.get("auction_ask_amount_yuan"))
    bid_end = _as_number(end_values.get("auction_bid_amount_yuan"))
    ask_end = _as_number(end_values.get("auction_ask_amount_yuan"))
    pressure_start = compute_resting_order_pressure(bid_start, ask_start)
    pressure_end = compute_resting_order_pressure(bid_end, ask_end)
    pressure = pressure_end
    order_status = (
        FactStatus.READY if pressure is not None else FactStatus.UNAVAILABLE
    )
    order_book = OrderBookFacts(
        order_status,
        pressure,
        {"auction_bid/ask": (start_ref, end_ref)},
        resting_bid_start_yuan=bid_start,
        resting_ask_start_yuan=ask_start,
        resting_bid_end_yuan=bid_end,
        resting_ask_end_yuan=ask_end,
        pressure_delta_yuan=(
            None
            if pressure_start is None or pressure_end is None
            else pressure_end - pressure_start
        ),
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
    if missing_groups:
        quality_status = FactStatus.PARTIAL
    elif unavailable_groups:
        quality_status = FactStatus.PARTIAL
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
        coverage_status=coverage_status,
        observed_start_time_ms=observed_start_time_ms,
        observed_end_time_ms=observed_end_time_ms,
    )


def compare_segments(
    previous: SegmentFrame,
    current: SegmentFrame,
) -> SegmentComparison:
    """Backward-compatible alias for adjacent segment comparison."""

    return compare_adjacent_segments(previous, current)


def compare_adjacent_segments(
    previous: SegmentFrame,
    current: SegmentFrame,
) -> SegmentComparison:
    """Compare two adjacent, non-overlapping fact frames."""

    if previous.scope_type != current.scope_type or previous.scope_id != current.scope_id:
        raise ValueError("adjacent segments must have the same scope")
    if previous.end_time_ms != current.start_time_ms:
        raise ValueError("adjacent segments must share the previous end/current start")

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
        previous.volume.amount_delta_yuan,
        current.volume.amount_delta_yuan,
        "VOLUME_EXPANDING",
        "VOLUME_CONTRACTING",
        "VOLUME_UNKNOWN",
    )
    if volume_change != "VOLUME_UNKNOWN":
        reasons.append("volume.amount_delta_yuan")
    order_change = _compare(
        previous.order_book.directional_pressure_yuan,
        current.order_book.directional_pressure_yuan,
        "PRESSURE_IMPROVING",
        "PRESSURE_WEAKENING",
        "PRESSURE_UNKNOWN",
    )
    if order_change != "PRESSURE_UNKNOWN":
        reasons.append("order_book.directional_pressure_yuan")
    comparison = {
        "previous_segment_id": previous.segment_id,
        "current_segment_id": current.segment_id,
        "previous_end_time_ms": previous.end_time_ms,
        "current_start_time_ms": current.start_time_ms,
        "price_change": price_change,
        "volume_change": volume_change,
        "order_book_change": order_change,
        "breadth_change": "BREADTH_UNAVAILABLE",
        "theme_change": "THEME_UNAVAILABLE",
        "reason_codes": tuple(reasons),
        "evidence_refs": tuple(sorted(set(previous.evidence_refs + current.evidence_refs))),
    }
    comparison_evidence_refs = tuple(sorted(set(previous.evidence_refs + current.evidence_refs)))
    return SegmentComparison(
        previous_segment_id=previous.segment_id,
        current_segment_id=current.segment_id,
        price_change=price_change,
        volume_change=volume_change,
        order_book_change=order_change,
        breadth_change="BREADTH_UNAVAILABLE",
        theme_change="THEME_UNAVAILABLE",
        reason_codes=tuple(reasons),
        evidence_refs=comparison_evidence_refs,
        content_hash=semantic_hash(
            {
                key: value
                for key, value in comparison.items()
                if key != "evidence_refs"
            }
        ),
        evidence_hash=build_evidence_hash(
            {
                "previous_segment_id": previous.segment_id,
                "current_segment_id": current.segment_id,
                "evidence_refs": comparison_evidence_refs,
                "field_lineage": {
                    "previous": {
                        "price": previous.price.field_lineage,
                        "volume": previous.volume.field_lineage,
                        "order_book": previous.order_book.field_lineage,
                        "breadth": previous.breadth.field_lineage,
                        "theme": previous.theme.field_lineage,
                    },
                    "current": {
                        "price": current.price.field_lineage,
                        "volume": current.volume.field_lineage,
                        "order_book": current.order_book.field_lineage,
                        "breadth": current.breadth.field_lineage,
                        "theme": current.theme.field_lineage,
                    },
                },
            }
        ),
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
    *,
    coverage_status: str = "UNKNOWN",
    observed_start_time_ms: Optional[int] = None,
    observed_end_time_ms: Optional[int] = None,
) -> SegmentFrame:
    semantic_content = {
        "segment_id": segment_id,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "start_time_ms": start_snapshot.logical_time_ms,
        "end_time_ms": end_snapshot.logical_time_ms,
        "price": {
            "status": price.status,
            "start_price_milli": price.start_price_milli,
            "end_price_milli": price.end_price_milli,
            "high_price_milli": price.high_price_milli,
            "low_price_milli": price.low_price_milli,
            "return_bp": price.return_bp,
        },
        "volume": {
            "status": volume.status,
            "amount_delta_yuan": volume.amount_delta_yuan,
            "volume_delta_lots": volume.volume_delta_lots,
        },
        "order_book": {
            "status": order_book.status,
            "directional_pressure_yuan": order_book.directional_pressure_yuan,
            "pressure_delta_yuan": order_book.pressure_delta_yuan,
            "resting_bid_start_yuan": order_book.resting_bid_start_yuan,
            "resting_ask_start_yuan": order_book.resting_ask_start_yuan,
            "resting_bid_end_yuan": order_book.resting_bid_end_yuan,
            "resting_ask_end_yuan": order_book.resting_ask_end_yuan,
        },
        "breadth": {
            "status": breadth.status,
            "up_count": breadth.up_count,
            "down_count": breadth.down_count,
        },
        "theme": {
            "status": theme.status,
            "participation_ratio": theme.participation_ratio,
        },
        "quality": quality,
        "coverage_status": coverage_status,
    }
    evidence_refs = tuple(
        sorted(
            set(
                (start_snapshot.snapshot_id, end_snapshot.snapshot_id)
                + tuple(start_snapshot.evidence_refs)
                + tuple(end_snapshot.evidence_refs)
            )
        )
    )
    evidence_payload = {
        "segment_id": segment_id,
        "snapshot_ids": (start_snapshot.snapshot_id, end_snapshot.snapshot_id),
        "evidence_refs": evidence_refs,
        "field_lineage": {
            "price": price.field_lineage,
            "volume": volume.field_lineage,
            "order_book": order_book.field_lineage,
            "breadth": breadth.field_lineage,
            "theme": theme.field_lineage,
        },
        "observed_start_time_ms": observed_start_time_ms,
        "observed_end_time_ms": observed_end_time_ms,
    }
    return SegmentFrame(
        segment_id=segment_id,
        scope_type=scope_type,
        scope_id=scope_id,
        start_snapshot_id=start_snapshot.snapshot_id,
        end_snapshot_id=end_snapshot.snapshot_id,
        start_time_ms=start_snapshot.logical_time_ms,
        end_time_ms=end_snapshot.logical_time_ms,
        price=price,
        volume=volume,
        order_book=order_book,
        breadth=breadth,
        theme=theme,
        quality=quality,
        content_hash=semantic_hash(semantic_content),
        coverage_status=coverage_status,
        observed_start_time_ms=observed_start_time_ms,
        observed_end_time_ms=observed_end_time_ms,
        evidence_refs=evidence_refs,
        evidence_hash=build_evidence_hash(evidence_payload),
    )


def _unavailable_frame(
    segment_id: str,
    scope_type: str,
    scope_id: str,
    start_snapshot: EngineSnapshot,
    end_snapshot: EngineSnapshot,
    *,
    coverage_status: str = "UNKNOWN",
    observed_start_time_ms: Optional[int] = None,
    observed_end_time_ms: Optional[int] = None,
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
        coverage_status=coverage_status,
        observed_start_time_ms=observed_start_time_ms,
        observed_end_time_ms=observed_end_time_ms,
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


def _as_number(value: Any) -> Optional[Number]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def compute_resting_order_pressure(
    resting_bid_yuan: Optional[Number],
    resting_ask_yuan: Optional[Number],
) -> Optional[Number]:
    """Compute the resting-order pressure proxy ``bid - ask``.

    The result is an order-book proxy, not authoritative net capital inflow.
    Missing inputs stay missing; an observed zero remains zero.
    """

    bid = _as_number(resting_bid_yuan)
    ask = _as_number(resting_ask_yuan)
    if bid is None or ask is None:
        return None
    return bid - ask


def _delta(
    start: Optional[Number],
    end: Optional[Number],
    semantics: str,
) -> Tuple[Optional[Number], FactStatus]:
    if semantics == "UNKNOWN":
        return None, FactStatus.UNAVAILABLE
    if semantics == "OBSERVED_STATE":
        if start is None or end is None:
            return None, FactStatus.MISSING
        # Point-in-time state fields (for example auction matched amount) are
        # differenced without claiming that the source is a cumulative counter.
        return end - start, FactStatus.READY
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
