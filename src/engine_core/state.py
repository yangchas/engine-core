"""Current market state and its deterministic reducer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .contracts import EngineSnapshot, canonical_hash, semantic_hash
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

        previous_session_id = self.state.session_id
        previous_observation_metadata = dict(self.state.source_observation_metadata)
        same_session = previous_session_id == session_id
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
        source_anomaly_hashes = tuple(
            getattr(projection, "source_anomaly_hashes", ())
        )
        if source_anomaly_hashes:
            # A malformed member is evidence of a partial source cohort, not a
            # reason to discard valid sibling quotes or stop the Engine.
            # Keep only stable hashes in state; never copy malformed payloads.
            self.state.source_observation_metadata.update({
                "source_anomaly_count": len(source_anomaly_hashes),
                "source_anomaly_hashes": source_anomaly_hashes,
                "source_evidence_hash": getattr(projection, "evidence_hash", ""),
            })
        out_of_scope_symbols = tuple(getattr(projection, "out_of_scope_symbols", ()))
        if out_of_scope_symbols:
            self.state.source_observation_metadata.update({
                "out_of_scope_symbols": out_of_scope_symbols,
                "out_of_scope_event_hashes": tuple(
                    getattr(projection, "out_of_scope_event_hashes", ())
                ),
                "out_of_scope_events": tuple(
                    getattr(projection, "out_of_scope_events", ())
                ),
                "evidence_hash": getattr(
                    projection,
                    "evidence_hash",
                    getattr(getattr(projection, "cross_section", None), "evidence_hash", ""),
                ),
            })
        previous_out_of_scope_symbols = set(
            previous_observation_metadata.get("out_of_scope_symbols", ())
        ) if same_session else set()
        current_out_of_scope_symbols = set(out_of_scope_symbols)
        accumulated_out_of_scope_symbols = previous_out_of_scope_symbols | current_out_of_scope_symbols
        if accumulated_out_of_scope_symbols:
            event_evidence_by_symbol = {}
            if same_session:
                for item in previous_observation_metadata.get("out_of_scope_event_evidence", ()):
                    symbol = item.get("symbol")
                    if symbol:
                        event_evidence_by_symbol[symbol] = dict(item)
            current_event_hashes = tuple(getattr(projection, "out_of_scope_event_hashes", ()))
            current_events = tuple(getattr(projection, "out_of_scope_events", ()))
            for index, event in enumerate(current_events):
                symbol = event.get("symbol") if isinstance(event, Mapping) else None
                if not symbol and len(current_out_of_scope_symbols) == 1:
                    symbol = next(iter(current_out_of_scope_symbols))
                if symbol:
                    event_evidence_by_symbol[symbol] = {
                        "symbol": symbol,
                        "event_hash": current_event_hashes[index] if index < len(current_event_hashes) else "",
                        "event": event,
                    }
            ordered_event_evidence = tuple(
                event_evidence_by_symbol[symbol]
                for symbol in sorted(event_evidence_by_symbol)
            )
            prior_event_count = (
                int(previous_observation_metadata.get("out_of_scope_event_count", 0))
                if same_session
                else 0
            )
            prior_chain_hash = (
                previous_observation_metadata.get("out_of_scope_evidence_hash", "")
                if same_session
                else ""
            )
            new_symbols = current_out_of_scope_symbols - previous_out_of_scope_symbols
            if current_event_hashes or new_symbols:
                chain_hash = semantic_hash({
                    "contract": "OutOfScopeEvidenceChainV1",
                    "previous_hash": prior_chain_hash,
                    "source_id": projection.envelope.source_id,
                    "new_symbols": tuple(sorted(new_symbols)),
                    "event_hashes": current_event_hashes,
                })
            else:
                chain_hash = prior_chain_hash
            self.state.source_observation_metadata.update({
                "out_of_scope_symbols": tuple(sorted(accumulated_out_of_scope_symbols)),
                "out_of_scope_event_hashes": tuple(
                    item["event_hash"] for item in ordered_event_evidence if item.get("event_hash")
                ),
                "out_of_scope_events": tuple(
                    item["event"] for item in ordered_event_evidence if item.get("event") is not None
                ),
                "out_of_scope_event_evidence": ordered_event_evidence,
                "out_of_scope_event_count": prior_event_count + len(current_event_hashes),
                "out_of_scope_evidence_hash": chain_hash,
            })
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
                    "historical_available_at_ms": getattr(
                        cross_section, "historical_available_at_ms", None
                    ),
                    "replay_status": getattr(cross_section, "replay_status", "READY"),
                    "replay_reasons": getattr(cross_section, "replay_reasons", ()),
                    "skipped_symbols": getattr(cross_section, "skipped_symbols", ()),
                    "batch_quality": getattr(cross_section, "batch_quality", "UNKNOWN"),
                    "same_event_order_ambiguity": getattr(
                        cross_section, "same_event_order_ambiguity", False
                    ),
                    "replay_order_status": getattr(
                        cross_section, "replay_order_status", "UNKNOWN"
                    ),
                    "source_batch_ids": getattr(cross_section, "source_batch_ids", ()),
                    "source_sequences": getattr(cross_section, "source_sequences", ()),
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
        field_errors = values.get("field_errors", ()) or ()
        if isinstance(field_errors, str):
            field_errors = (field_errors,)
        field_errors = set(field_errors)
        # Keep the raw per-symbol observation in state, but do not present a
        # cross-date, stale, future-dated, or malformed price pair as current
        # market breadth. This is a per-observation quality downgrade, not a
        # projection/run gate: the symbol remains observed and counts as
        # unknown until a usable price pair arrives.
        if field_errors.intersection(
            {
                "ts",
                "future_ts",
                "trade_date",
                "stale",
                "px",
                "px_non_positive",
                "pc",
                "pc_non_positive",
            }
        ):
            unknown += 1
            continue
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
