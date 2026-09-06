"""Read-only probe strategy for the first vertical slice."""

from __future__ import annotations

from typing import Any, Dict

from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    SUBMISSION_HASH_CONTRACT_VERSION,
    EngineSnapshot,
    FrozenDataBundle,
    StrategyResult,
    semantic_hash,
)


class ProbeStrategy:
    """Emit an evidence trace without making a trading decision."""

    strategy_id = "probe"

    def evaluate(
        self,
        snapshot: EngineSnapshot,
        bundle: FrozenDataBundle,
    ) -> StrategyResult:
        trace: Dict[str, Any] = {
            "state": "OBSERVE",
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.content_hash,
            "bundle_hash": bundle.content_hash,
            "submission_hash": bundle.submission_hash,
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
                "submission": SUBMISSION_HASH_CONTRACT_VERSION,
            },
            "trigger_id": snapshot.trigger_id,
            "logical_time_ms": snapshot.logical_time_ms,
            "phase": snapshot.phase,
            "market_state_revision": snapshot.market_state_revision,
            "observed_symbol_count": len(snapshot.symbol_states),
            "coverage": snapshot.coverage,
            "completeness": snapshot.completeness,
            "market_cross_section": dict(snapshot.raw_market_cross_section),
            "source": dict(snapshot.source_observation_metadata),
            "windows": {
                key: value.content_hash
                for key, value in snapshot.windows.items()
            },
        }
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=trace,
            evidence_refs=snapshot.evidence_refs,
            content_hash=semantic_hash(trace),
        )
