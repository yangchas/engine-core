"""Fact-only opening strategy composition for the next migration slice.

This module deliberately wraps the already verified opening fact wheel only.
It does not infer auction state, apply strategy thresholds, or perform I/O.
"""

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
from .opening import OPENING_FACT_CONTRACT_VERSION, build_open_fact


class OpeningShadowStrategy:
    """Compose one current-state opening fact and emit observation only."""

    strategy_id = "opening-shadow-v1"

    def __init__(self, *, scope_id: str) -> None:
        if not isinstance(scope_id, str) or len(scope_id) != 6 or not scope_id.isdigit():
            raise ValueError("scope_id must be a six-digit code")
        self.scope_id = scope_id

    def evaluate(
        self,
        snapshot: EngineSnapshot,
        bundle: FrozenDataBundle,
    ) -> StrategyResult:
        if not isinstance(snapshot, EngineSnapshot):
            raise TypeError("snapshot must be EngineSnapshot")
        if not isinstance(bundle, FrozenDataBundle):
            raise TypeError("bundle must be FrozenDataBundle")
        values = snapshot.symbol_states.get(self.scope_id)
        trace: Dict[str, Any] = {
            "state": "OBSERVE",
            "decision_status": "FACT_ONLY",
            "strategy_id": self.strategy_id,
            "opening_fact_contract_version": OPENING_FACT_CONTRACT_VERSION,
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
        if values is None:
            trace.update(
                {
                    "fact_status": "MISSING",
                    "reason_codes": ("SYMBOL_STATE_MISSING",),
                }
            )
        else:
            # Q2's ``speed_1m_bp`` is deliberately not mapped to the opening
            # wheel's ``speed_1m`` field: the units are not proven identical.
            row = {
                "symbol": self.scope_id,
                "timestamp_ms": snapshot.source_observation_metadata.get(
                    "newest_source_time_ms"
                ),
                "price_milli": values.get("price_milli"),
                "previous_close_milli": values.get("pre_close_milli"),
                "amount_2m_yuan": values.get("amount_2m_yuan"),
                "limit_state": values.get("limit_state"),
                "name": values.get("name"),
            }
            fact = build_open_fact(row)
            trace.update(
                {
                    "fact_status": "READY" if fact["status"] == "available" else "PARTIAL",
                    "opening_fact": fact,
                    "source_time_range": {
                        "oldest": snapshot.source_observation_metadata.get(
                            "oldest_source_time_ms"
                        ),
                        "newest": snapshot.source_observation_metadata.get(
                            "newest_source_time_ms"
                        ),
                    },
                }
            )
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=trace,
            evidence_refs=tuple(sorted(set(snapshot.evidence_refs))),
            content_hash=semantic_hash(trace),
        )


__all__ = ["OpeningShadowStrategy"]
