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
        metadata = snapshot.source_observation_metadata
        stale_symbols = metadata.get("stale_symbols", ()) or ()
        missing_symbols = metadata.get("missing_symbols", ()) or ()
        trace: Dict[str, Any] = {
            "state": "OBSERVE",
            "decision_status": "FACT_ONLY",
            # READY describes only the primary change fact and its own source
            # timestamp. Optional/independently usable fields are reported
            # below and must not be inferred from this summary status.
            "fact_status_scope": "change_pct_and_source_time",
            "strategy_id": self.strategy_id,
            "opening_fact_contract_version": OPENING_FACT_CONTRACT_VERSION,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.content_hash,
            "bundle_hash": bundle.content_hash,
            "submission_hash": bundle.submission_hash,
            "trigger_id": snapshot.trigger_id,
            "logical_time_ms": snapshot.logical_time_ms,
            "phase": snapshot.phase,
            # Keep cohort quality visible without degrading an unrelated
            # symbol's independently observed opening fact.
            "snapshot_quality": {
                "completeness": snapshot.completeness,
                "coverage": snapshot.coverage,
                "stale_symbol_count": len(stale_symbols),
                "missing_symbol_count": len(missing_symbols),
            },
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
            field_errors = values.get("field_errors", ()) or ()
            if isinstance(field_errors, str):
                field_errors = (field_errors,)
            field_errors = tuple(sorted({str(item) for item in field_errors}))
            raw_speed_1m_bp = values.get("speed_1m_bp")
            if "spd1m" in field_errors:
                speed_1m = None
                speed_1m_status = "INVALID"
            elif raw_speed_1m_bp is None:
                speed_1m = None
                speed_1m_status = "UNAVAILABLE"
            elif isinstance(raw_speed_1m_bp, int) and not isinstance(
                raw_speed_1m_bp, bool
            ):
                # t1-v2 publishes spd1m as basis points; engine-next's
                # strategy-facing speed_1m is a decimal ratio.
                speed_1m = raw_speed_1m_bp / 10000.0
                speed_1m_status = "AVAILABLE"
            else:
                speed_1m = None
                speed_1m_status = "INVALID"
            row = {
                "symbol": self.scope_id,
                # Every symbol can advance on a different source tick.  The
                # cross-section's newest timestamp is useful cohort metadata,
                # but must not be attributed to this symbol's fact.
                "timestamp_ms": values.get("source_record_time_ms"),
                "price_milli": values.get("price_milli"),
                "previous_close_milli": values.get("pre_close_milli"),
                "amount_2m_yuan": values.get("amount_2m_yuan"),
                "limit_state": values.get("limit_state"),
                "speed_1m": speed_1m,
                "name": values.get("name"),
            }
            fact = build_open_fact(row)
            fact_available = fact["status"] == "available"
            source_time = values.get("source_record_time_ms")
            source_time_valid = (
                isinstance(source_time, int) and not isinstance(source_time, bool)
            )
            time_quality_errors = {"ts", "future_ts", "trade_date", "stale"}
            relevant_time_errors = tuple(
                sorted(time_quality_errors.intersection(field_errors))
            )
            fact_quality_ready = (
                fact_available and source_time_valid and not relevant_time_errors
            )
            amount_2m_status = (
                "AVAILABLE"
                if fact["amount_2m_yuan"] is not None
                else "INVALID"
                if "amt2m" in field_errors
                else "UNAVAILABLE"
            )
            trace.update(
                {
                    # Source quality is symbol-local for this fact.  The
                    # aggregate cohort state remains in snapshot_quality, but
                    # a different symbol's stale/missing quote does not make
                    # this symbol's current fact unusable.
                    "fact_status": "READY" if fact_quality_ready else "PARTIAL",
                    "opening_fact": fact,
                    "opening_fact_field_status": {
                        "change_pct": (
                            "AVAILABLE"
                            if fact["change_pct"] is not None
                            else "UNAVAILABLE"
                        ),
                        "amount_2m_yuan": amount_2m_status,
                        "limit_state": str(fact["limit_state_status"]).upper(),
                        "speed_1m": speed_1m_status,
                    },
                    "symbol_source_quality": {
                        "source_record_time_ms": source_time,
                        "field_errors": field_errors,
                        "time_quality_errors": relevant_time_errors,
                    },
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
