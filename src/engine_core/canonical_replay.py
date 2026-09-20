"""Pure offline replay seam for Rabbit-primary canonical batches.

The adapter in this module accepts already-built :class:`TickBatchV1` values,
projects safe canonical ticks to the existing ``TDEventV1`` replay oracle, and
feeds one global 3-second frame at a time.  It deliberately owns no source
client, clock scheduler, persistence, recovery provider, or effect path.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping, Optional, Tuple

from .auction_timeline import AuctionAnchorRevisionV1, AuctionTimeline
from .clock import VirtualClock
from .contracts import semantic_hash
from .replay import TDEventV1
from .replay_frames import CrossSectionReplaySource, MarketFrameV1
from .canonical_ticks import FieldQuality, MarketTickV1, TickBatchV1


CANONICAL_OFFLINE_REPLAY_CONTRACT_VERSION = "CanonicalOfflineReplayV1"
OFFLINE_REPLAY_MODES = frozenset({"REPLAY", "OFFLINE", "HISTORICAL"})


class CanonicalReplayStatus(str, Enum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    EMPTY = "EMPTY"
    BLOCKED = "BLOCKED"


class CanonicalReplayBlocked(ValueError):
    """Raised when replay cannot safely consume a canonical batch."""


@dataclass(frozen=True)
class CanonicalBatchProjectionV1:
    """Safe projection of one canonical batch into the legacy replay shape."""

    trade_date: str
    source: str
    source_batch_id: str
    batch_content_hash: str
    status: CanonicalReplayStatus
    input_tick_count: int
    projected_event_count: int
    skipped_symbols: Tuple[str, ...]
    reasons: Tuple[str, ...]
    events: Tuple[TDEventV1, ...]
    skipped_events: Tuple[Tuple[int, str, str], ...] = ()
    batch_quality: str = "UNKNOWN"
    same_event_order_ambiguity: bool = False
    source_sequence_status: str = "UNKNOWN"
    arrival_order_status: str = "UNKNOWN"
    replay_order_status: str = "UNKNOWN"
    historical_available_at_ms: Optional[int] = None
    historical_available_at_status: str = "UNKNOWN"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", CanonicalReplayStatus(self.status))
        object.__setattr__(self, "skipped_symbols", tuple(sorted(set(self.skipped_symbols))))
        object.__setattr__(self, "reasons", tuple(sorted(set(self.reasons))))
        object.__setattr__(self, "events", tuple(self.events))
        skipped_events = tuple(
            (int(event_time_ms), str(symbol), str(reason))
            for event_time_ms, symbol, reason in self.skipped_events
        )
        object.__setattr__(self, "skipped_events", skipped_events)
        if self.input_tick_count < 0 or self.projected_event_count < 0:
            raise ValueError("canonical batch counts must be non-negative")
        if self.projected_event_count != len(self.events):
            raise ValueError("projected_event_count must match events")
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract": CANONICAL_OFFLINE_REPLAY_CONTRACT_VERSION,
                    "trade_date": self.trade_date,
                    "source": self.source,
                    "source_batch_id": self.source_batch_id,
                    "batch_content_hash": self.batch_content_hash,
                    "status": self.status.value,
                    "input_tick_count": self.input_tick_count,
                    "projected_event_count": self.projected_event_count,
                    "skipped_symbols": self.skipped_symbols,
                    "reasons": self.reasons,
                    "events": tuple(event.content_hash for event in self.events),
                    "skipped_events": self.skipped_events,
                    "batch_quality": self.batch_quality,
                    "same_event_order_ambiguity": self.same_event_order_ambiguity,
                }
            ),
        )


@dataclass(frozen=True)
class CanonicalFrameResultV1:
    """One frame plus source projection diagnostics."""

    frame: MarketFrameV1
    status: CanonicalReplayStatus
    source_batch_ids: Tuple[str, ...]
    skipped_symbols: Tuple[str, ...]
    reasons: Tuple[str, ...]
    batch_quality: str = "UNKNOWN"
    same_event_order_ambiguity: bool = False
    source_sequence_status: str = "UNKNOWN"
    arrival_order_status: str = "UNKNOWN"
    replay_order_status: str = "UNKNOWN"
    historical_available_at_ms: Optional[int] = None
    historical_available_at_status: str = "UNKNOWN"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", CanonicalReplayStatus(self.status))
        object.__setattr__(self, "source_batch_ids", tuple(sorted(set(self.source_batch_ids))))
        object.__setattr__(self, "skipped_symbols", tuple(sorted(set(self.skipped_symbols))))
        object.__setattr__(self, "reasons", tuple(sorted(set(self.reasons))))
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract": CANONICAL_OFFLINE_REPLAY_CONTRACT_VERSION,
                    "frame_hash": self.frame.content_hash,
                    "status": self.status.value,
                    "source_batch_ids": self.source_batch_ids,
                    "skipped_symbols": self.skipped_symbols,
                    "reasons": self.reasons,
                    "batch_quality": self.batch_quality,
                    "same_event_order_ambiguity": self.same_event_order_ambiguity,
                    "source_sequence_status": self.source_sequence_status,
                    "arrival_order_status": self.arrival_order_status,
                    "replay_order_status": self.replay_order_status,
                    "historical_available_at_ms": self.historical_available_at_ms,
                    "historical_available_at_status": self.historical_available_at_status,
                }
            ),
        )


def _required_field_reason(tick: MarketTickV1) -> Optional[str]:
    """Return the first unsafe legacy-replay field, without zero-filling."""

    for field_name in ("px_milli", "pc_milli", "amt_yuan"):
        value = getattr(tick, field_name)
        quality = tick.field_meta_map[field_name].quality
        if value is None:
            return "%s:MISSING_VALUE" % field_name
        if quality is not FieldQuality.PRESENT_VALUE:
            return "%s:%s" % (field_name, quality.value)
    return None


def _merge_evidence_status(values: Iterable[str], *, default: str = "UNKNOWN") -> str:
    """Merge source metadata conservatively without inventing certainty."""

    unique = tuple(sorted({str(value) for value in values if value is not None}))
    if not unique:
        return default
    return unique[0] if len(unique) == 1 else default


def _merge_batch_quality(values: Iterable[str], *, has_events: bool) -> str:
    unique = {str(value) for value in values if value is not None}
    if not unique:
        return "UNKNOWN" if has_events else "EMPTY"
    if "PARTIAL" in unique:
        return "PARTIAL"
    if "UNKNOWN" in unique:
        return "UNKNOWN"
    if unique == {"EMPTY"}:
        return "EMPTY"
    return "COMPLETE"


def _merge_optional_int(values: Iterable[Optional[int]]) -> Optional[int]:
    unique = {int(value) for value in values if value is not None}
    return next(iter(unique)) if len(unique) == 1 else None


class OfflineCanonicalReplay:
    """Compose canonical ticks, global frames, and fact-only auction timing."""

    def __init__(
        self,
        trade_date: str,
        expected_symbols: Iterable[Any],
        *,
        start_ms: int,
        end_exclusive_ms: int,
        slice_ms: int = 3_000,
    ) -> None:
        self._clock = VirtualClock(datetime.fromtimestamp(start_ms / 1000.0, timezone.utc))
        self.source = CrossSectionReplaySource(
            trade_date,
            expected_symbols,
            self._clock,
            slice_anchor_ms=start_ms,
            slice_ms=slice_ms,
            end_exclusive_ms=end_exclusive_ms,
        )
        self.auction_timeline = AuctionTimeline(trade_date)

    @staticmethod
    def project_batch(batch: TickBatchV1) -> CanonicalBatchProjectionV1:
        """Project a parsed canonical batch without changing its semantics."""

        if not isinstance(batch, TickBatchV1):
            raise TypeError("batch must be TickBatchV1")
        mode = str(batch.mode).upper()
        if mode not in OFFLINE_REPLAY_MODES:
            raise CanonicalReplayBlocked(
                "canonical offline replay requires REPLAY/OFFLINE/HISTORICAL mode; got %s" % batch.mode
            )
        events: list[TDEventV1] = []
        skipped: list[str] = []
        reasons: list[str] = []
        skipped_events: list[tuple[int, str, str]] = []
        for tick in batch.ticks:
            reason = _required_field_reason(tick)
            if reason is not None:
                skipped.append(tick.symbol)
                reasons.append("%s:%s" % (tick.symbol, reason))
                skipped_events.append((tick.event_time_ms, tick.symbol, reason))
                continue
            try:
                events.append(tick.to_tdevent())
            except (TypeError, ValueError) as exc:
                skipped.append(tick.symbol)
                reasons.append("%s:%s" % (tick.symbol, type(exc).__name__))
                skipped_events.append((tick.event_time_ms, tick.symbol, type(exc).__name__))
        if not batch.ticks:
            status = CanonicalReplayStatus.EMPTY
        elif not events and batch.ticks:
            status = CanonicalReplayStatus.BLOCKED
        elif skipped:
            status = CanonicalReplayStatus.PARTIAL
        else:
            status = CanonicalReplayStatus.READY
        return CanonicalBatchProjectionV1(
            trade_date=batch.trade_date,
            source=batch.source,
            source_batch_id=batch.source_batch_id,
            batch_content_hash=batch.batch_content_hash,
            status=status,
            input_tick_count=len(batch.ticks),
            projected_event_count=len(events),
            skipped_symbols=tuple(skipped),
            reasons=tuple(reasons),
            events=tuple(events),
            skipped_events=tuple(skipped_events),
            batch_quality=batch.batch_quality.value,
            same_event_order_ambiguity=batch.same_event_order_ambiguity,
            source_sequence_status=batch.source_sequence_status.value,
            arrival_order_status=batch.arrival_order_status.value,
            replay_order_status=batch.replay_order_status.value,
            historical_available_at_ms=batch.historical_available_at_ms,
            historical_available_at_status=batch.historical_available_at_status.value,
        )

    def iter_frame_results(self, batches: Iterable[TickBatchV1]) -> Iterator[CanonicalFrameResultV1]:
        """Stream batches into global frames, retaining only the current frame."""

        pending_frame_no = 0
        pending_events: list[TDEventV1] = []
        pending_batch_ids: list[str] = []
        pending_skipped: list[str] = []
        pending_reasons: list[str] = []
        pending_projections: list[CanonicalBatchProjectionV1] = []

        def emit(frame_no: int) -> CanonicalFrameResultV1:
            if pending_reasons and pending_events:
                status = CanonicalReplayStatus.PARTIAL
            elif pending_reasons:
                status = CanonicalReplayStatus.BLOCKED
            elif not pending_events:
                status = CanonicalReplayStatus.EMPTY
            else:
                status = CanonicalReplayStatus.READY
            qualities = [item.batch_quality for item in pending_projections]
            batch_quality = _merge_batch_quality(qualities, has_events=bool(pending_events))
            same_event_order_ambiguity = any(
                item.same_event_order_ambiguity for item in pending_projections
            )
            source_sequence_status = _merge_evidence_status(
                item.source_sequence_status for item in pending_projections
            )
            arrival_order_status = _merge_evidence_status(
                item.arrival_order_status for item in pending_projections
            )
            replay_order_status = _merge_evidence_status(
                item.replay_order_status for item in pending_projections
            )
            historical_available_at_ms = _merge_optional_int(
                item.historical_available_at_ms for item in pending_projections
            )
            historical_available_at_status = _merge_evidence_status(
                item.historical_available_at_status for item in pending_projections
            )
            frame = replace(
                self.source.frame_from_events(frame_no, pending_events),
                batch_quality=batch_quality,
                same_event_order_ambiguity=same_event_order_ambiguity,
                source_sequence_status=source_sequence_status,
                rabbit_arrival_order=arrival_order_status,
                replay_order_status=replay_order_status,
                historical_available_at=historical_available_at_status,
                historical_available_at_ms=historical_available_at_ms,
                source_batch_ids=tuple(pending_batch_ids),
            )
            return CanonicalFrameResultV1(
                frame=frame,
                status=status,
                source_batch_ids=tuple(pending_batch_ids),
                skipped_symbols=tuple(pending_skipped),
                reasons=tuple(pending_reasons),
                batch_quality=batch_quality,
                same_event_order_ambiguity=same_event_order_ambiguity,
                source_sequence_status=source_sequence_status,
                arrival_order_status=arrival_order_status,
                replay_order_status=replay_order_status,
                historical_available_at_ms=historical_available_at_ms,
                historical_available_at_status=historical_available_at_status,
            )

        for batch in batches:
            projection = self.project_batch(batch)
            if projection.trade_date != self.source.trade_date:
                raise CanonicalReplayBlocked("canonical batch trade_date does not match replay")
            ordered_items = [
                (event.event_time_ms, event.symbol, "event", event, None)
                for event in projection.events
            ] + [
                (event_time_ms, symbol, "skipped", None, reason)
                for event_time_ms, symbol, reason in projection.skipped_events
            ]
            ordered_items.sort(
                key=lambda item: (
                    item[0],
                    item[1],
                    item[2],
                    "" if item[4] is None else item[4],
                    "" if item[3] is None else item[3].content_hash,
                )
            )
            for event_time_ms, symbol, item_kind, event, reason in ordered_items:
                if (
                    event_time_ms < self.source.slice_anchor_ms
                    or event_time_ms >= self.source.end_exclusive_ms
                ):
                    raise CanonicalReplayBlocked(
                        "canonical event is outside the configured replay window"
                    )
                frame_no = (event_time_ms - self.source.slice_anchor_ms) // self.source.slice_ms
                if frame_no < pending_frame_no:
                    raise CanonicalReplayBlocked("canonical batches moved backwards in event time")
                while pending_frame_no < frame_no:
                    yield emit(pending_frame_no)
                    pending_frame_no += 1
                    pending_events.clear()
                    pending_batch_ids.clear()
                    pending_skipped.clear()
                    pending_reasons.clear()
                    pending_projections.clear()
                if projection not in pending_projections:
                    pending_projections.append(projection)
                if item_kind == "event":
                    pending_events.append(event)
                else:
                    pending_skipped.append(symbol)
                    pending_reasons.append("%s:%s" % (symbol, reason))
                if projection.source_batch_id not in pending_batch_ids:
                    pending_batch_ids.append(projection.source_batch_id)
        while pending_frame_no < self.source.frame_count:
            yield emit(pending_frame_no)
            pending_frame_no += 1
            pending_events.clear()
            pending_batch_ids.clear()
            pending_skipped.clear()
            pending_reasons.clear()
            pending_projections.clear()

    def observe_auction(self, tag: str, rows: Any, **kwargs: Any) -> AuctionAnchorRevisionV1:
        """Record already-observed auction facts through the existing timeline."""

        return self.auction_timeline.observe(tag, rows, **kwargs)

    def auction_analysis(self, tag: str = "0925") -> Mapping[str, Any]:
        """Return the existing fact-only analysis/recovery bundle."""

        return self.auction_timeline.build_analysis_bundle(tag)

    def replay(self, batches: Iterable[TickBatchV1], engine: Any, *, signal_prefix: str = "canonical-replay") -> None:
        """Submit one MARKET_UPDATE signal per global frame to one Engine."""

        latest_raw: dict[str, Mapping[str, Any]] = {}
        for result in self.iter_frame_results(batches):
            signal = self.source.signal_for_frame(
                result.frame,
                latest_raw,
                signal_prefix=signal_prefix,
                replay_status=result.status.value,
                replay_reasons=result.reasons,
                skipped_symbols=result.skipped_symbols,
                source_batch_ids=result.source_batch_ids,
                batch_quality=result.batch_quality,
                same_event_order_ambiguity=result.same_event_order_ambiguity,
                source_sequence_status=result.source_sequence_status,
                rabbit_arrival_order=result.arrival_order_status,
                replay_order_status=result.replay_order_status,
                historical_available_at=result.historical_available_at_status,
                historical_available_at_ms=result.historical_available_at_ms,
            )
            self._clock.advance_to(datetime.fromtimestamp(signal.logical_time_ms / 1000, timezone.utc))
            engine.submit(signal)
            engine.run_until_empty()


__all__ = [
    "CANONICAL_OFFLINE_REPLAY_CONTRACT_VERSION",
    "CanonicalReplayBlocked",
    "CanonicalReplayStatus",
    "CanonicalBatchProjectionV1",
    "CanonicalFrameResultV1",
    "OfflineCanonicalReplay",
]
