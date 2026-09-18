"""Fact-only auction strategy composition for the first Core migration.

This module is deliberately an observation strategy, not a trading strategy.
It wires the already verified auction fact wheels into ``DeterministicEngine``
without introducing thresholds, votes, candidates, effects, or I/O.  A
runtime integration can therefore use the same class as a fixture test while
the production ``engine-next`` process remains the owner of decisions.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from .auction_shadow import build_auction_fact_shadow_from_snapshots
from .contracts import (
    DataStatus,
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    SUBMISSION_HASH_CONTRACT_VERSION,
    EngineSnapshot,
    FrozenDataBundle,
    StrategyResult,
    semantic_hash,
)
from .theme_auction_delta_strategy import build_legacy_theme_delta_shadow_trace


class AuctionShadowStrategy:
    """Compose adjacent auction facts and emit an ``OBSERVE`` trace only.

    The strategy remembers snapshots by their declared trigger id for one
    engine/session lifetime.  The trigger names and scope are explicit so a
    caller cannot accidentally compare arbitrary snapshots.  Source ranges
    come from snapshot metadata; if only a source record timestamp exists it
    is preserved as a one-point observed range rather than being fabricated.

    ``decision_status`` is always ``FACT_ONLY``.  In particular, this class
    never emits BUY/PASS/EV/risk/candidate conclusions and never performs I/O.
    """

    strategy_id = "auction-shadow-v1"

    def __init__(
        self,
        *,
        scope_type: str = "SYMBOL",
        scope_id: str,
        start_trigger_id: str = "PRE_AUCTION_0915",
        middle_trigger_id: str = "AUCTION_0920",
        end_trigger_id: str = "AUCTION_0924",
        previous_segment_id: Optional[str] = None,
        current_segment_id: Optional[str] = None,
        amount_semantics: str = "OBSERVED_STATE",
        volume_semantics: str = "UNKNOWN",
        previous_coverage_status: Optional[str] = None,
        current_coverage_status: Optional[str] = None,
        theme_delta_function_id: Optional[str] = None,
    ) -> None:
        if scope_type != "SYMBOL":
            raise ValueError("the first auction strategy slice supports SYMBOL only")
        if not scope_id:
            raise ValueError("scope_id is required")
        triggers = (start_trigger_id, middle_trigger_id, end_trigger_id)
        if any(not isinstance(item, str) or not item for item in triggers):
            raise ValueError("auction trigger ids must be non-empty strings")
        if len(set(triggers)) != len(triggers):
            raise ValueError("auction trigger ids must be distinct")
        previous_segment_id = previous_segment_id or "auction_trial_" + scope_id
        current_segment_id = current_segment_id or "auction_reprice_" + scope_id
        if previous_segment_id == current_segment_id:
            raise ValueError("auction segment ids must be distinct")
        self.scope_type = scope_type
        self.scope_id = scope_id
        self.start_trigger_id = start_trigger_id
        self.middle_trigger_id = middle_trigger_id
        self.end_trigger_id = end_trigger_id
        self.previous_segment_id = previous_segment_id
        self.current_segment_id = current_segment_id
        self.amount_semantics = amount_semantics
        self.volume_semantics = volume_semantics
        self.previous_coverage_status = previous_coverage_status
        self.current_coverage_status = current_coverage_status
        if theme_delta_function_id is not None and (
            not isinstance(theme_delta_function_id, str) or not theme_delta_function_id.strip()
        ):
            raise ValueError("theme_delta_function_id must be non-empty when provided")
        self.theme_delta_function_id = theme_delta_function_id
        self._session_id: Optional[str] = None
        self._snapshots: Dict[str, EngineSnapshot] = {}

    @property
    def required_trigger_ids(self) -> Tuple[str, str, str]:
        return (
            self.start_trigger_id,
            self.middle_trigger_id,
            self.end_trigger_id,
        )

    def evaluate(
        self,
        snapshot: EngineSnapshot,
        bundle: FrozenDataBundle,
    ) -> StrategyResult:
        """Record one frozen snapshot and return a fact-only strategy trace."""

        if not isinstance(snapshot, EngineSnapshot):
            raise TypeError("snapshot must be EngineSnapshot")
        if not isinstance(bundle, FrozenDataBundle):
            raise TypeError("bundle must be FrozenDataBundle")
        if self._session_id is None:
            self._session_id = snapshot.session_id
        elif snapshot.session_id != self._session_id:
            raise ValueError("auction strategy cannot mix sessions")
        if snapshot.trigger_id in self.required_trigger_ids:
            previous = self._snapshots.get(snapshot.trigger_id)
            if previous is not None and previous.content_hash != snapshot.content_hash:
                raise ValueError("conflicting snapshot for auction trigger")
            self._snapshots[snapshot.trigger_id] = snapshot
        trace: Dict[str, Any] = {
            "state": "OBSERVE",
            "decision_status": "FACT_ONLY",
            "strategy_id": self.strategy_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.content_hash,
            "bundle_hash": bundle.content_hash,
            "submission_hash": bundle.submission_hash,
            "trigger_id": snapshot.trigger_id,
            "logical_time_ms": snapshot.logical_time_ms,
            "phase": snapshot.phase,
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
                "submission": SUBMISSION_HASH_CONTRACT_VERSION,
            },
        }

        missing = [
            trigger_id
            for trigger_id in self.required_trigger_ids
            if trigger_id not in self._snapshots
        ]
        if missing:
            trace.update(
                {
                    "fact_status": "PENDING",
                    "reason_codes": ("WAITING_FOR_AUCTION_ANCHORS",),
                    "missing_trigger_ids": tuple(missing),
                }
            )
        else:
            start, middle, end = (
                self._snapshots[trigger_id]
                for trigger_id in self.required_trigger_ids
            )
            fact = build_auction_fact_shadow_from_snapshots(
                start,
                middle,
                end,
                scope_type=self.scope_type,
                scope_id=self.scope_id,
                previous_segment_id=self.previous_segment_id,
                current_segment_id=self.current_segment_id,
                amount_semantics=self.amount_semantics,
                volume_semantics=self.volume_semantics,
                previous_coverage_status=self._coverage_status(
                    start, middle, self.previous_coverage_status
                ),
                current_coverage_status=self._coverage_status(
                    middle, end, self.current_coverage_status
                ),
                previous_observed_start_time_ms=self._observed_start(start),
                previous_observed_end_time_ms=self._observed_end(middle),
                current_observed_start_time_ms=self._observed_start(middle),
                current_observed_end_time_ms=self._observed_end(end),
            )
            trace.update(
                {
                    "fact_status": fact.status,
                    "auction_fact_shadow": fact.as_trace(),
                }
            )
            if self.theme_delta_function_id is not None:
                result = bundle.results_by_function.get(self.theme_delta_function_id)
                if result is not None:
                    theme_trace: Optional[Mapping[str, Any]] = None
                    if result.status in (DataStatus.READY, DataStatus.PARTIAL):
                        data = result.data
                        if not isinstance(data, Mapping):
                            raise TypeError("theme delta DataResult.data must be a mapping")
                        theme_facts = data.get("facts", ())
                        theme_trace = build_legacy_theme_delta_shadow_trace(theme_facts)
                    trace["theme_delta_shadow"] = {
                        "function_id": self.theme_delta_function_id,
                        "data_status": result.status,
                        "data_result_hash": result.content_hash,
                        "shadow": theme_trace,
                        "reason_codes": ()
                        if theme_trace is not None
                        else ("THEME_DATA_NOT_READY",),
                    }

        # A completed three-anchor fact is only auditable when the top-level
        # result carries every participating snapshot reference.  Keeping
        # only the current snapshot here would make the nested fact trace
        # complete while consumers of StrategyResult.evidence_refs lost the
        # 09:15/09:20/09:24 lineage.
        evidence_refs = tuple(
            sorted(
                {
                    evidence_ref
                    for item in self._snapshots.values()
                    for evidence_ref in item.evidence_refs
                }
            )
        )
        theme_shadow = trace.get("theme_delta_shadow")
        if isinstance(theme_shadow, Mapping):
            shadow = theme_shadow.get("shadow")
            if isinstance(shadow, Mapping):
                evidence_refs = tuple(
                    sorted(set(evidence_refs).union(shadow.get("evidence_refs", ())))
                )
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=trace,
            evidence_refs=evidence_refs,
            content_hash=semantic_hash(trace),
        )

    @staticmethod
    def _coverage_status(
        start: EngineSnapshot,
        end: EngineSnapshot,
        explicit: Optional[str],
    ) -> str:
        """Use the snapshot quality contract without promoting partial data."""

        if explicit is not None:
            if explicit not in {"READY", "PARTIAL", "MISSING", "UNKNOWN"}:
                raise ValueError("invalid auction coverage status")
            return explicit
        values = (start.completeness, end.completeness)
        return "READY" if all(value == "READY" for value in values) else "PARTIAL"

    @staticmethod
    def _observed_start(snapshot: EngineSnapshot) -> Optional[int]:
        metadata = snapshot.source_observation_metadata
        value = metadata.get("oldest_source_time_ms")
        if value is None:
            value = metadata.get("source_record_time_ms")
        return value

    @staticmethod
    def _observed_end(snapshot: EngineSnapshot) -> Optional[int]:
        metadata = snapshot.source_observation_metadata
        value = metadata.get("newest_source_time_ms")
        if value is None:
            value = metadata.get("source_record_time_ms")
        return value


__all__ = ["AuctionShadowStrategy"]
