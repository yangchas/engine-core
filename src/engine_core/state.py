"""Current market state and its deterministic reducer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .contracts import EngineSnapshot, canonical_hash
from .q2 import Q2ProjectionSnapshot, classify_equity
from .windows import WindowManager


class CurrentMarketState:
    """Mutable reducer-owned state containing observations only.

    Strategy conclusions, segment comparisons, EV and risk decisions are
    intentionally absent from this object.
    """

    def __init__(self) -> None:
        self.revision = 0
        self.logical_time_ms = 0
        self.session_id = ""
        self.phase = "UNKNOWN"
        self.symbol_states: Dict[str, Mapping[str, Any]] = {}
        self.raw_market_cross_section: Dict[str, Any] = {}
        self.raw_theme_cross_section: Dict[str, Any] = {}
        self.source_observation_metadata: Dict[str, Any] = {}
        self.coverage = 0.0
        self.completeness = "MISSING"
        self.last_envelope_id = ""


class MarketStateReducer:
    """Apply one projection snapshot at a time to CurrentMarketState."""

    def __init__(self, state: Optional[CurrentMarketState] = None) -> None:
        self.state = state or CurrentMarketState()

    def apply_snapshot(
        self,
        projection: Q2ProjectionSnapshot,
        *,
        logical_time_ms: Optional[int] = None,
        session_id: str = "",
        phase: str = "UNKNOWN",
    ) -> CurrentMarketState:
        """Apply a Q2 projection without producing a strategy conclusion."""

        if logical_time_ms is None and projection.envelope.effective_time_ms is None:
            raise ValueError(
                "logical_time_ms is required when source effective time is unavailable"
            )
        self.state.revision += 1
        self.state.logical_time_ms = (
            projection.envelope.effective_time_ms
            if logical_time_ms is None
            else logical_time_ms
        )
        self.state.session_id = session_id
        self.state.phase = phase
        self.state.symbol_states = {
            symbol: quote.to_mapping()
            for symbol, quote in sorted(projection.quotes.items())
        }
        self.state.raw_market_cross_section = _market_cross_section(
            self.state.symbol_states
        )
        self.state.raw_theme_cross_section = {}
        self.state.source_observation_metadata = {
            "source_id": projection.envelope.source_id,
            "source_schema": projection.envelope.provenance.source_schema,
            "envelope_id": projection.envelope.envelope_id,
            "observation_time_ms": projection.envelope.observed_time_ms,
            "effective_time_ms": projection.envelope.effective_time_ms,
            "oldest_source_time_ms": projection.oldest_source_time_ms,
            "newest_source_time_ms": projection.newest_source_time_ms,
            "generation": projection.envelope.generation,
            "generation_kind": projection.envelope.generation_kind,
            "consistency_status": projection.consistency_status,
            "missing_symbols": projection.missing_symbols,
            "stale_symbols": projection.stale_symbols,
            "content_hash": projection.content_hash,
        }
        cross_section = getattr(projection, "cross_section", None)
        if cross_section is not None:
            self.state.source_observation_metadata.update(
                {
                    "cross_section_contract": "CrossSectionStateV1",
                    "cross_section_hash": cross_section.content_hash,
                    "cross_section_evidence_hash": cross_section.evidence_hash,
                    "frame_no": cross_section.frame_no,
                    "frame_completeness": cross_section.frame_completeness,
                    "updated_symbols": cross_section.updated_symbols,
                    "frame_missing_symbols": cross_section.missing_symbols,
                    "source_sequence_status": cross_section.source_sequence_status,
                    "rabbit_arrival_order": cross_section.rabbit_arrival_order,
                    "historical_available_at": cross_section.historical_available_at,
                }
            )
        self.state.coverage = projection.coverage
        cross_section = getattr(projection, "cross_section", None)
        self.state.completeness = (
            cross_section.frame_completeness
            if cross_section is not None
            else projection.status.value
        )
        self.state.last_envelope_id = projection.envelope.envelope_id
        return self.state

    def build_snapshot(
        self,
        trigger_id: str,
        *,
        logical_time_ms: Optional[int] = None,
        windows: Optional[WindowManager] = None,
        phase: Optional[str] = None,
    ) -> EngineSnapshot:
        """Freeze current observations with an optional trigger-time phase.

        A supplied phase describes the snapshot's logical instant.  It does
        not rewrite the phase attached to the most recent market observation.
        """

        logical = self.state.logical_time_ms if logical_time_ms is None else logical_time_ms
        snapshot_phase = self.state.phase if phase is None else phase
        window_views = windows.views() if windows is not None else {}
        payload = {
            "trigger_id": trigger_id,
            "logical_time_ms": logical,
            "session_id": self.state.session_id,
            "phase": snapshot_phase,
            "revision": self.state.revision,
            "source": self.state.source_observation_metadata,
            "symbols": self.state.symbol_states,
            "market": self.state.raw_market_cross_section,
            "themes": self.state.raw_theme_cross_section,
            "windows": window_views,
            "coverage": self.state.coverage,
            "completeness": self.state.completeness,
        }
        envelope_id = str(self.state.source_observation_metadata.get("envelope_id", ""))
        return EngineSnapshot(
            snapshot_id=canonical_hash(payload),
            trigger_id=trigger_id,
            logical_time_ms=logical,
            session_id=self.state.session_id,
            phase=snapshot_phase,
            market_state_revision=self.state.revision,
            source_observation_metadata=dict(self.state.source_observation_metadata),
            symbol_states={
                symbol: dict(values)
                for symbol, values in self.state.symbol_states.items()
            },
            raw_market_cross_section=dict(self.state.raw_market_cross_section),
            raw_theme_cross_section=dict(self.state.raw_theme_cross_section),
            windows=window_views,
            coverage=self.state.coverage,
            completeness=self.state.completeness,
            content_hash=canonical_hash(payload),
            evidence_refs=(envelope_id,) if envelope_id else (),
        )


def _market_cross_section(symbol_states: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    up = down = flat = unknown = 0
    observed = 0
    excluded = 0
    for symbol, values in symbol_states.items():
        if not classify_equity(symbol, values):
            excluded += 1
            continue
        observed += 1
        price = values.get("price_milli")
        pre_close = values.get("pre_close_milli")
        if price is None or pre_close is None:
            unknown += 1
        elif price > pre_close:
            up += 1
        elif price < pre_close:
            down += 1
        else:
            flat += 1
    return {
        "observed_symbol_count": observed,
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "unknown_count": unknown,
        "excluded_non_equity_count": excluded,
    }
