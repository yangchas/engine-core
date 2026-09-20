"""Deterministic session-timeline ledger for bounded replay.

The ledger joins already-built cross-sectional frames, auction revisions,
timer firings, and a final checkpoint without owning any clock, provider,
Rabbit consumer, persistence, or effect.  It stores hashes and timing
metadata only; raw frame events remain owned by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .auction_timeline import AuctionAnchorRevisionV1, AuctionTimeline
from .calendar import parse_trade_date
from .contracts import evidence_hash, semantic_hash
from .replay_frames import FrameManifestV1, MarketFrameV1
from .timers import TimerFiring


REPLAY_SESSION_TIMELINE_CONTRACT_VERSION = "ReplaySessionTimelineV1"
REPLAY_SESSION_NODE_CONTRACT_VERSION = "ReplaySessionNodeV1"

NODE_FRAME = "FRAME"
NODE_AUCTION = "AUCTION"
NODE_TIMER = "TIMER"
NODE_CHECKPOINT = "CHECKPOINT"


def _positive_ms(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ReplaySessionNodeV1:
    """One hash-addressed node in the replay session timeline."""

    trade_date: str
    node_id: str
    kind: str
    business_anchor_ms: int
    evaluation_time_ms: int
    state: str
    source_content_hash: str
    source_layers: tuple[str, ...] = ()
    source_time_min_ms: Optional[int] = None
    source_time_max_ms: Optional[int] = None
    late_execution: bool = False
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        parsed_trade_date = parse_trade_date(self.trade_date)
        if parsed_trade_date.isoformat() != self.trade_date:
            raise ValueError("trade_date must be strict YYYY-MM-DD")
        if not isinstance(self.node_id, str) or not self.node_id:
            raise ValueError("node_id is required")
        if self.kind not in {NODE_FRAME, NODE_AUCTION, NODE_TIMER, NODE_CHECKPOINT}:
            raise ValueError("unsupported replay session node kind")
        _positive_ms(self.business_anchor_ms, "business_anchor_ms")
        _positive_ms(self.evaluation_time_ms, "evaluation_time_ms")
        if self.evaluation_time_ms < self.business_anchor_ms and self.kind != NODE_FRAME:
            raise ValueError("evaluation_time_ms cannot precede business anchor")
        if not isinstance(self.state, str) or not self.state:
            raise ValueError("state is required")
        if not isinstance(self.source_content_hash, str) or not self.source_content_hash:
            raise ValueError("source_content_hash is required")
        if self.source_time_min_ms is not None:
            _positive_ms(self.source_time_min_ms, "source_time_min_ms")
        if self.source_time_max_ms is not None:
            _positive_ms(self.source_time_max_ms, "source_time_max_ms")
        if (
            self.source_time_min_ms is not None
            and self.source_time_max_ms is not None
            and self.source_time_min_ms > self.source_time_max_ms
        ):
            raise ValueError("source time range is inverted")
        layers = tuple(self.source_layers)
        if any(not isinstance(item, str) or not item for item in layers):
            raise ValueError("source_layers must contain non-empty strings")
        object.__setattr__(self, "source_layers", layers)
        object.__setattr__(
            self,
            "content_hash",
            semantic_hash(
                {
                    "contract_version": REPLAY_SESSION_NODE_CONTRACT_VERSION,
                    "trade_date": self.trade_date,
                    "node_id": self.node_id,
                    "kind": self.kind,
                    "business_anchor_ms": self.business_anchor_ms,
                    "state": self.state,
                    "source_content_hash": self.source_content_hash,
                }
            ),
        )
        object.__setattr__(
            self,
            "evidence_hash",
            evidence_hash(
                {
                    "evaluation_time_ms": self.evaluation_time_ms,
                    "source_layers": layers,
                    "source_time_min_ms": self.source_time_min_ms,
                    "source_time_max_ms": self.source_time_max_ms,
                    "late_execution": self.late_execution,
                }
            ),
        )


class ReplaySessionTimeline:
    """Hash-only timeline joining one market replay with session milestones."""

    def __init__(self, manifest: FrameManifestV1, *, auction_timeline: Optional[AuctionTimeline] = None) -> None:
        if not isinstance(manifest, FrameManifestV1):
            raise TypeError("manifest must be FrameManifestV1")
        self.manifest = manifest
        self.trade_date = manifest.trade_date
        self.auction_timeline = auction_timeline or AuctionTimeline(self.trade_date)
        if self.auction_timeline.trade_date != self.trade_date:
            raise ValueError("auction timeline trade_date does not match manifest")
        self._nodes: dict[str, ReplaySessionNodeV1] = {}
        self._frame_ids: list[str] = []

    @property
    def frame_count(self) -> int:
        return len(self._frame_ids)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def content_hash(self) -> str:
        return semantic_hash(
            {
                "contract_version": REPLAY_SESSION_TIMELINE_CONTRACT_VERSION,
                "manifest_hash": self.manifest.content_hash,
                "nodes": tuple(
                    node.content_hash
                    for node in self._ordered_nodes()
                ),
            }
        )

    @property
    def evidence_hash(self) -> str:
        return evidence_hash(
            {
                "manifest_evidence_hash": self.manifest.evidence_hash,
                "nodes": tuple(
                    node.evidence_hash
                    for node in self._ordered_nodes()
                ),
            }
        )

    def _ordered_nodes(self) -> tuple[ReplaySessionNodeV1, ...]:
        kind_order = {NODE_FRAME: 0, NODE_AUCTION: 1, NODE_TIMER: 2, NODE_CHECKPOINT: 3}
        return tuple(
            sorted(
                self._nodes.values(),
                key=lambda node: (
                    node.business_anchor_ms,
                    node.evaluation_time_ms,
                    kind_order[node.kind],
                    node.node_id,
                ),
            )
        )

    def _put(self, node: ReplaySessionNodeV1) -> ReplaySessionNodeV1:
        previous = self._nodes.get(node.node_id)
        if previous is not None:
            if previous.source_content_hash != node.source_content_hash:
                raise ValueError(f"conflicting replay session node: {node.node_id}")
            # A repeated auction cohort may retain the same source content
            # revision while its timing-derived state and evidence advance
            # across the soft cutoff.  Keep the newest node without inventing
            # a source revision or losing the idempotent node identity.
            if previous.content_hash != node.content_hash or previous.evidence_hash != node.evidence_hash:
                self._nodes[node.node_id] = node
                return node
            return previous
        self._nodes[node.node_id] = node
        return node

    def record_frame(self, frame: MarketFrameV1) -> ReplaySessionNodeV1:
        """Record one sequential frame while retaining no raw events."""

        if not isinstance(frame, MarketFrameV1):
            raise TypeError("frame must be MarketFrameV1")
        if frame.trade_date != self.trade_date:
            raise ValueError("frame trade_date does not match manifest")
        if frame.frame_no != self.frame_count:
            raise ValueError("frames must be recorded sequentially, including EMPTY frames")
        expected_start = self.manifest.start_ms + frame.frame_no * self.manifest.frame_interval_ms
        if frame.start_ms != expected_start:
            raise ValueError("frame start does not match manifest")
        if frame.end_exclusive_ms > self.manifest.end_exclusive_ms:
            raise ValueError("frame exceeds manifest boundary")
        node = ReplaySessionNodeV1(
            trade_date=self.trade_date,
            node_id=f"FRAME:{frame.frame_no:04d}",
            kind=NODE_FRAME,
            business_anchor_ms=frame.logical_ts_ms,
            evaluation_time_ms=frame.logical_ts_ms,
            state=frame.completeness,
            source_content_hash=frame.content_hash,
            source_layers=("cross_section_frame",),
            source_time_min_ms=frame.source_time_min_ms,
            source_time_max_ms=frame.source_time_max_ms,
        )
        recorded = self._put(node)
        self._frame_ids.append(recorded.node_id)
        return recorded

    def observe_auction(
        self,
        tag: str,
        rows: Any,
        *,
        evaluation_time_ms: int,
        expected_symbols: tuple[str, ...] = (),
        observed_at_ms: Optional[int] = None,
        source_layers: tuple[str, ...] = (),
        recovery_state: str = "NOT_REQUESTED",
    ) -> AuctionAnchorRevisionV1:
        """Record an auction revision from already-observed rows."""

        revision = self.auction_timeline.observe(
            tag,
            rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=expected_symbols or self.manifest.expected_symbols,
            observed_at_ms=observed_at_ms,
            source_layers=source_layers,
            recovery_state=recovery_state,
        )
        self._put(
            ReplaySessionNodeV1(
                trade_date=self.trade_date,
                node_id=f"AUCTION:{tag}:r{revision.revision}",
                kind=NODE_AUCTION,
                business_anchor_ms=revision.business_anchor_ms,
                evaluation_time_ms=revision.evaluation_time_ms,
                state=revision.state,
                source_content_hash=revision.content_hash,
                source_layers=revision.source_layers,
                source_time_min_ms=revision.source_time_min_ms,
                source_time_max_ms=revision.source_time_max_ms,
                late_execution=revision.late_execution,
            )
        )
        return revision

    def record_timer(self, firing: TimerFiring) -> ReplaySessionNodeV1:
        """Record one already-computed timer firing; no timer is scheduled here."""

        if not isinstance(firing, TimerFiring):
            raise TypeError("firing must be TimerFiring")
        return self._put(
            ReplaySessionNodeV1(
                trade_date=self.trade_date,
                node_id=f"TIMER:{firing.timer_id}",
                kind=NODE_TIMER,
                business_anchor_ms=firing.scheduled_time_ms,
                evaluation_time_ms=firing.fired_time_ms,
                state="FIRED",
                source_content_hash=firing.content_hash,
                source_layers=(f"timer:{firing.origin.lower()}",),
                late_execution=firing.late_by_ms > 0,
            )
        )

    def record_checkpoint(
        self,
        checkpoint_id: str,
        *,
        evaluation_time_ms: int,
        business_anchor_ms: Optional[int] = None,
        state: str = "RECORDED",
        source_content_hash: Optional[str] = None,
    ) -> ReplaySessionNodeV1:
        """Record a deterministic checkpoint such as the 09:40 boundary."""

        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("checkpoint_id is required")
        anchor = evaluation_time_ms if business_anchor_ms is None else business_anchor_ms
        return self._put(
            ReplaySessionNodeV1(
                trade_date=self.trade_date,
                node_id=f"CHECKPOINT:{checkpoint_id}",
                kind=NODE_CHECKPOINT,
                business_anchor_ms=anchor,
                evaluation_time_ms=evaluation_time_ms,
                state=state,
                source_content_hash=source_content_hash or semantic_hash({"checkpoint_id": checkpoint_id}),
                source_layers=("replay_checkpoint",),
            )
        )

    def finalize(self, *, evaluation_time_ms: Optional[int] = None) -> ReplaySessionNodeV1:
        """Close the frame ledger at the manifest boundary and record 09:40."""

        if self.frame_count != self.manifest.frame_count:
            raise ValueError("cannot finalize before every manifest frame is recorded")
        evaluation = self.manifest.end_exclusive_ms if evaluation_time_ms is None else evaluation_time_ms
        return self.record_checkpoint(
            "0940",
            evaluation_time_ms=evaluation,
            business_anchor_ms=self.manifest.end_exclusive_ms,
            state="COMPLETE",
            source_content_hash=semantic_hash({"manifest_hash": self.manifest.content_hash}),
        )

    def snapshot(self) -> Mapping[str, Any]:
        """Return a small evidence snapshot containing hashes, not raw rows."""

        nodes = self._ordered_nodes()
        anchors = {
            tag: {
                "revision": revision.revision,
                "state": revision.state,
                "content_hash": revision.content_hash,
                "evidence_hash": revision.evidence_hash,
            }
            for tag in ("0920", "0924", "0925")
            if (revision := self.auction_timeline.latest(tag)) is not None
        }
        return {
            "contract_version": REPLAY_SESSION_TIMELINE_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "manifest_hash": self.manifest.content_hash,
            "frame_count": self.frame_count,
            "expected_frame_count": self.manifest.frame_count,
            "node_count": len(nodes),
            "node_ids": tuple(node.node_id for node in nodes),
            "anchors": anchors,
            "timers": tuple(node.node_id for node in nodes if node.kind == NODE_TIMER),
            "checkpoints": tuple(node.node_id for node in nodes if node.kind == NODE_CHECKPOINT),
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
        }


__all__ = [
    "NODE_AUCTION",
    "NODE_CHECKPOINT",
    "NODE_FRAME",
    "NODE_TIMER",
    "REPLAY_SESSION_NODE_CONTRACT_VERSION",
    "REPLAY_SESSION_TIMELINE_CONTRACT_VERSION",
    "ReplaySessionNodeV1",
    "ReplaySessionTimeline",
]
