"""Fact-only Engine strategy for the verified auction-anchor delta wheel.

This is the first concrete strategy-shaped migration slice after the numeric
anchor fact was verified.  It stores only the declared auction snapshots and
delegates all calculations to :func:`build_anchor_delta_evidence`; it never
adds a threshold, candidate, order, notification, or other side effect.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from .anchor_delta import build_anchor_delta_evidence
from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    SUBMISSION_HASH_CONTRACT_VERSION,
    EngineSnapshot,
    FrozenDataBundle,
    StrategyResult,
    deep_freeze,
    evidence_hash,
    semantic_hash,
)


ANCHOR_DELTA_STRATEGY_CONTRACT_VERSION = "AnchorDeltaShadowStrategyV1"
DEFAULT_ANCHOR_PAIRS = (
    ("AUCTION_0920", "AUCTION_0924"),
    ("AUCTION_0924", "AUCTION_0925"),
)


class AnchorDeltaShadowStrategy:
    """Compose verified anchor deltas and emit fact-only Engine traces."""

    strategy_id = "auction-anchor-delta-shadow-v1"

    def __init__(
        self,
        *,
        scope_id: str,
        anchor_pairs: Tuple[Tuple[str, str], ...] = DEFAULT_ANCHOR_PAIRS,
    ) -> None:
        if not isinstance(scope_id, str) or not scope_id:
            raise ValueError("scope_id is required")
        pairs = tuple(tuple(pair) for pair in anchor_pairs)
        if not pairs:
            raise ValueError("anchor_pairs are required")
        if any(len(pair) != 2 or any(not isinstance(item, str) or not item for item in pair) for pair in pairs):
            raise ValueError("anchor_pairs must contain two non-empty trigger ids")
        if len(set(pairs)) != len(pairs):
            raise ValueError("anchor_pairs must not contain duplicates")
        self.scope_id = scope_id
        self.anchor_pairs = pairs
        self._session_id: Optional[str] = None
        self._snapshots: dict[str, EngineSnapshot] = {}

    def evaluate(
        self,
        snapshot: EngineSnapshot,
        bundle: FrozenDataBundle,
    ) -> StrategyResult:
        if not isinstance(snapshot, EngineSnapshot):
            raise TypeError("snapshot must be EngineSnapshot")
        if not isinstance(bundle, FrozenDataBundle):
            raise TypeError("bundle must be FrozenDataBundle")
        if self._session_id is None:
            self._session_id = snapshot.session_id
        elif snapshot.session_id != self._session_id:
            raise ValueError("anchor delta strategy cannot mix sessions")

        watched = {trigger for pair in self.anchor_pairs for trigger in pair}
        if snapshot.trigger_id in watched:
            previous = self._snapshots.get(snapshot.trigger_id)
            if previous is not None and previous.content_hash != snapshot.content_hash:
                raise ValueError("conflicting snapshot for anchor trigger")
            self._snapshots[snapshot.trigger_id] = snapshot

        declared_triggers = tuple(
            dict.fromkeys(trigger for pair in self.anchor_pairs for trigger in pair)
        )
        missing = tuple(
            trigger for trigger in declared_triggers if trigger not in self._snapshots
        )
        facts = []
        evidence_refs = set()
        for from_trigger, to_trigger in self.anchor_pairs:
            previous = self._snapshots.get(from_trigger)
            current = self._snapshots.get(to_trigger)
            if previous is None or current is None:
                continue
            if previous.logical_time_ms >= current.logical_time_ms:
                raise ValueError("anchor snapshots must be strictly time ordered")
            previous_row = _snapshot_row(previous, scope_id=self.scope_id)
            current_row = _snapshot_row(current, scope_id=self.scope_id)
            from_tag = _tag_from_trigger(from_trigger)
            to_tag = _tag_from_trigger(to_trigger)
            fact = build_anchor_delta_evidence(
                previous_row,
                current_row,
                symbol=self.scope_id,
                from_tag=from_tag,
                to_tag=to_tag,
            )
            facts.append(
                {
                    "from_trigger_id": from_trigger,
                    "to_trigger_id": to_trigger,
                    "fact": fact,
                    "previous_snapshot_hash": previous.content_hash,
                    "current_snapshot_hash": current.content_hash,
                }
            )
            evidence_refs.update(previous.evidence_refs)
            evidence_refs.update(current.evidence_refs)

        semantic_trace = {
            "contract_version": ANCHOR_DELTA_STRATEGY_CONTRACT_VERSION,
            "state": "OBSERVE",
            "decision_status": "FACT_ONLY",
            "strategy_id": self.strategy_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.content_hash,
            "bundle_hash": bundle.content_hash,
            "trigger_id": snapshot.trigger_id,
            "logical_time_ms": snapshot.logical_time_ms,
            "phase": snapshot.phase,
            "fact_status": "PENDING" if missing else "OBSERVED",
            "missing_trigger_ids": missing,
            "anchor_deltas": tuple(facts),
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
                "submission": SUBMISSION_HASH_CONTRACT_VERSION,
            },
        }
        lineage = tuple(
            {
                "from_trigger_id": item["from_trigger_id"],
                "to_trigger_id": item["to_trigger_id"],
                "previous_snapshot_hash": item["previous_snapshot_hash"],
                "current_snapshot_hash": item["current_snapshot_hash"],
            }
            for item in facts
        )
        fact_evidence_hash = evidence_hash(
            {
                "contract_version": ANCHOR_DELTA_STRATEGY_CONTRACT_VERSION,
                "scope_id": self.scope_id,
                "lineage": lineage,
                "evidence_refs": tuple(sorted(evidence_refs)),
            }
        )
        trace = dict(semantic_trace)
        # Submission identity is audit context, not business semantic
        # identity.  Keep it visible in the trace without making the fact
        # content hash vary merely because the same bundle arrived under a
        # different evaluation/submission context.
        trace["submission_hash"] = bundle.submission_hash
        trace["evidence_hash"] = fact_evidence_hash
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=deep_freeze(trace),
            evidence_refs=tuple(sorted(evidence_refs)),
            content_hash=semantic_hash(semantic_trace),
        )


def _tag_from_trigger(trigger_id: str) -> str:
    if not trigger_id.startswith("AUCTION_"):
        raise ValueError("anchor trigger must use AUCTION_ prefix")
    tag = trigger_id.removeprefix("AUCTION_")
    if len(tag) != 4 or not tag.isdigit():
        raise ValueError("anchor trigger must end with a four-digit tag")
    return tag


def _snapshot_row(snapshot: EngineSnapshot, *, scope_id: str) -> Mapping[str, Any]:
    state = snapshot.symbol_states.get(scope_id, {})
    ask = state.get("auction_ask_amount_yuan")
    return {
        "symbol": scope_id,
        "tag": _tag_from_trigger(snapshot.trigger_id),
        "price_milli": state.get("price_milli"),
        "auction_amount_yuan": state.get("auction_amount_yuan"),
        "bid_amount_yuan": state.get("auction_bid_amount_yuan"),
        "ask_amount_yuan": ask,
        "ask_amount_present": ask is not None,
    }


__all__ = [
    "ANCHOR_DELTA_STRATEGY_CONTRACT_VERSION",
    "DEFAULT_ANCHOR_PAIRS",
    "AnchorDeltaShadowStrategy",
]
