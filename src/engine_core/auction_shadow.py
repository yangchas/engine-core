"""Read-only auction fact shadow for the first Gate B slice.

This module deliberately stops at the fact boundary.  The legacy
``bid_amount > ask_amount * 1.5`` branch and the old turn-strong/turn-weak
rules are not verified consumer contracts yet, so this object must not emit a
trading conclusion.  It only packages the already-tested adjacent segment
facts in a stable, traceable shape for shadow review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple, Union

from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    EngineSnapshot,
    deep_freeze,
    evidence_hash,
    semantic_hash,
)
from .facts import (
    FactStatus,
    SegmentFrame,
    build_segment_frame,
    compare_adjacent_segments,
)

Number = Union[int, float]


@dataclass(frozen=True)
class AuctionFactShadow:
    """A fact-only, read-only shadow of two adjacent auction segments.

    ``status`` describes whether the requested fact bundle is complete.  It
    is not a strategy state.  ``changes`` contains the existing dimension
    labels from :class:`SegmentComparison`; labels such as
    ``PRESSURE_IMPROVING`` remain local facts and must not be interpreted as
    net capital flow or a trade authorization.
    """

    shadow_kind: str
    scope_type: str
    scope_id: str
    previous_segment_id: str
    current_segment_id: str
    status: FactStatus
    coverage_status: str
    quality_status: FactStatus
    metrics: Mapping[str, Optional[Number]]
    changes: Mapping[str, str]
    reason_codes: Tuple[str, ...]
    evidence_refs: Tuple[str, ...]
    comparison_hash: str
    content_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", deep_freeze(self.metrics))
        object.__setattr__(self, "changes", deep_freeze(self.changes))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "evidence_refs", tuple(sorted(set(self.evidence_refs))))

    def as_trace(self) -> Mapping[str, Any]:
        """Return a deterministic trace payload without strategy wording."""

        return {
            "shadow_kind": self.shadow_kind,
            "state": "OBSERVE",
            "decision_status": "FACT_ONLY",
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
            },
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "previous_segment_id": self.previous_segment_id,
            "current_segment_id": self.current_segment_id,
            "status": self.status,
            "coverage_status": self.coverage_status,
            "quality_status": self.quality_status,
            "metrics": self.metrics,
            "changes": self.changes,
            "reason_codes": self.reason_codes,
            "comparison_hash": self.comparison_hash,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
            "evidence_refs": self.evidence_refs,
        }


def build_auction_fact_shadow(
    previous: SegmentFrame,
    current: SegmentFrame,
) -> AuctionFactShadow:
    """Build the minimal adjacent auction fact shadow.

    The function accepts only adjacent, same-scope frames and delegates
    dimension labels to the already verified ``compare_adjacent_segments``
    wheel.  It exposes endpoint changes that are useful for audit:

    * ``price_delta_milli``: current end price minus previous end price;
    * ``amount_delta_yuan``: the current segment's observed auction amount
      change;
    * ``rest_bid_delta_yuan`` / ``rest_ask_delta_yuan``: endpoint changes;
    * ``pressure_delta_yuan``: endpoint ``RB - RA`` change.

    Missing values remain ``None``.  No fallback, total score, threshold or
    capital-flow conclusion is introduced here.
    """

    comparison = compare_adjacent_segments(previous, current)
    evidence_refs = tuple(sorted(set(comparison.evidence_refs)))
    metrics = {
        "price_delta_milli": _subtract(
            current.price.end_price_milli,
            previous.price.end_price_milli,
        ),
        "amount_delta_yuan": current.volume.amount_delta_yuan,
        "rest_bid_delta_yuan": _subtract(
            current.order_book.resting_bid_end_yuan,
            previous.order_book.resting_bid_end_yuan,
        ),
        "rest_ask_delta_yuan": _subtract(
            current.order_book.resting_ask_end_yuan,
            previous.order_book.resting_ask_end_yuan,
        ),
        "pressure_delta_yuan": _subtract(
            current.order_book.directional_pressure_yuan,
            previous.order_book.directional_pressure_yuan,
        ),
    }
    changes = {
        "price": comparison.price_change,
        "amount": comparison.volume_change,
        "order_book": comparison.order_book_change,
        "breadth": comparison.breadth_change,
        "theme": comparison.theme_change,
    }
    reason_codes = tuple(comparison.reason_codes)
    coverage_status = _coverage_status(previous.coverage_status, current.coverage_status)
    quality_status = _quality_status(previous, current, metrics)
    status = _shadow_status(quality_status, coverage_status)
    semantic_content = {
        "shadow_kind": "AUCTION_ADJACENT_FACT_V1",
        "scope_type": previous.scope_type,
        "scope_id": previous.scope_id,
        "previous_segment_id": previous.segment_id,
        "current_segment_id": current.segment_id,
        "status": status,
        "coverage_status": coverage_status,
        "quality_status": quality_status,
        "metrics": metrics,
        "changes": changes,
        "reason_codes": reason_codes,
        "comparison_hash": comparison.content_hash,
    }
    lineage = {
        "previous": {
            "price": previous.price.field_lineage,
            "volume": previous.volume.field_lineage,
            "order_book": previous.order_book.field_lineage,
        },
        "current": {
            "price": current.price.field_lineage,
            "volume": current.volume.field_lineage,
            "order_book": current.order_book.field_lineage,
        },
    }
    evidence_content = {
        "shadow_kind": "AUCTION_ADJACENT_FACT_V1",
        "previous_segment_id": previous.segment_id,
        "current_segment_id": current.segment_id,
        "comparison_evidence_hash": comparison.evidence_hash,
        "evidence_refs": evidence_refs,
        "field_lineage": lineage,
    }
    return AuctionFactShadow(
        shadow_kind="AUCTION_ADJACENT_FACT_V1",
        scope_type=previous.scope_type,
        scope_id=previous.scope_id,
        previous_segment_id=previous.segment_id,
        current_segment_id=current.segment_id,
        status=status,
        coverage_status=coverage_status,
        quality_status=quality_status,
        metrics=metrics,
        changes=changes,
        reason_codes=reason_codes,
        evidence_refs=evidence_refs,
        comparison_hash=comparison.content_hash,
        content_hash=semantic_hash(semantic_content),
        evidence_hash=evidence_hash(evidence_content),
    )


def build_auction_fact_shadow_from_snapshots(
    start_snapshot: EngineSnapshot,
    middle_snapshot: EngineSnapshot,
    end_snapshot: EngineSnapshot,
    *,
    scope_type: str,
    scope_id: str,
    previous_segment_id: str,
    current_segment_id: str,
    amount_semantics: str = "OBSERVED_STATE",
    volume_semantics: str = "UNKNOWN",
    previous_coverage_status: str = "UNKNOWN",
    current_coverage_status: str = "UNKNOWN",
    previous_observed_start_time_ms: Optional[int] = None,
    previous_observed_end_time_ms: Optional[int] = None,
    current_observed_start_time_ms: Optional[int] = None,
    current_observed_end_time_ms: Optional[int] = None,
) -> AuctionFactShadow:
    """Compose the fact-only shadow from three Engine snapshots.

    This is intentionally a small adapter at the Engine observation boundary,
    not a second strategy executor.  The snapshots represent the business
    anchors ``start -> middle -> end``; source observation ranges are supplied
    separately so a delayed Q2 cohort cannot be mistaken for the business
    interval.  All calculation remains in ``build_segment_frame`` and
    ``build_auction_fact_shadow``.
    """

    snapshots = (start_snapshot, middle_snapshot, end_snapshot)
    if any(not isinstance(item, EngineSnapshot) for item in snapshots):
        raise TypeError("auction shadow snapshots must be EngineSnapshot values")
    if not previous_segment_id or not current_segment_id:
        raise ValueError("segment ids are required")
    if previous_segment_id == current_segment_id:
        raise ValueError("segment ids must be distinct")
    if start_snapshot.session_id != middle_snapshot.session_id:
        raise ValueError("auction shadow snapshots must share a session")
    if middle_snapshot.session_id != end_snapshot.session_id:
        raise ValueError("auction shadow snapshots must share a session")
    if not (
        start_snapshot.logical_time_ms
        < middle_snapshot.logical_time_ms
        < end_snapshot.logical_time_ms
    ):
        raise ValueError("auction shadow snapshot times must be strictly increasing")

    previous = build_segment_frame(
        previous_segment_id,
        start_snapshot,
        middle_snapshot,
        scope_type=scope_type,
        scope_id=scope_id,
        amount_semantics=amount_semantics,
        volume_semantics=volume_semantics,
        coverage_status=previous_coverage_status,
        observed_start_time_ms=previous_observed_start_time_ms,
        observed_end_time_ms=previous_observed_end_time_ms,
    )
    current = build_segment_frame(
        current_segment_id,
        middle_snapshot,
        end_snapshot,
        scope_type=scope_type,
        scope_id=scope_id,
        amount_semantics=amount_semantics,
        volume_semantics=volume_semantics,
        coverage_status=current_coverage_status,
        observed_start_time_ms=current_observed_start_time_ms,
        observed_end_time_ms=current_observed_end_time_ms,
    )
    return build_auction_fact_shadow(previous, current)


def _subtract(current: Optional[Number], previous: Optional[Number]) -> Optional[Number]:
    if current is None or previous is None:
        return None
    return current - previous


def _coverage_status(previous: str, current: str) -> str:
    if previous == "READY" and current == "READY":
        return "READY"
    if previous in {"MISSING", "UNKNOWN"} or current in {"MISSING", "UNKNOWN"}:
        return "PARTIAL"
    return "PARTIAL"


def _quality_status(
    previous: SegmentFrame,
    current: SegmentFrame,
    metrics: Mapping[str, Optional[Number]],
) -> FactStatus:
    if previous.quality.status is FactStatus.INVALID or current.quality.status is FactStatus.INVALID:
        return FactStatus.INVALID
    if (
        previous.quality.status is FactStatus.MISSING
        or current.quality.status is FactStatus.MISSING
    ):
        return FactStatus.MISSING
    if any(value is None for value in metrics.values()):
        return FactStatus.PARTIAL
    if previous.quality.status is not FactStatus.READY or current.quality.status is not FactStatus.READY:
        return FactStatus.PARTIAL
    return FactStatus.READY


def _shadow_status(quality_status: FactStatus, coverage_status: str) -> FactStatus:
    if quality_status is FactStatus.INVALID:
        return FactStatus.INVALID
    if quality_status is FactStatus.MISSING:
        return FactStatus.MISSING
    if quality_status is FactStatus.UNAVAILABLE:
        return FactStatus.UNAVAILABLE
    if coverage_status != "READY" or quality_status is FactStatus.PARTIAL:
        return FactStatus.PARTIAL
    return FactStatus.READY
