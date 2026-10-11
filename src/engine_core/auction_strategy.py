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
from .auction_timeline import build_auction_anchor_fact_v1
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
from .facts import build_segment_frame
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
        price_fields: Optional[Tuple[str, str, str]] = None,
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
        if price_fields is not None and (
            not isinstance(price_fields, tuple)
            or len(price_fields) != 3
            or any(not isinstance(item, str) or not item for item in price_fields)
        ):
            raise ValueError("price_fields must be a tuple of three non-empty field names")
        self.price_fields = price_fields
        if theme_delta_function_id is not None and (
            not isinstance(theme_delta_function_id, str) or not theme_delta_function_id.strip()
        ):
            raise ValueError("theme_delta_function_id must be non-empty when provided")
        self.theme_delta_function_id = theme_delta_function_id
        self._session_id: Optional[str] = None
        self._snapshots: Dict[str, EngineSnapshot] = {}
        self._snapshot_history: Dict[str, list[dict[str, Any]]] = {}
        self._snapshot_evidence_refs: Dict[str, list[Tuple[str, ...]]] = {}
        self._revision_diagnostics: list[str] = []

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
            self._record_anchor_snapshot(snapshot)
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
        if self.price_fields is not None:
            trace["price_fields"] = self.price_fields
        trace["auction_revision_state"] = self._auction_revision_state()

        # Emit the current anchor as a standalone fact before checking whether
        # adjacent snapshots are available.  In particular, a usable 0925
        # anchor must not wait for 0920/0924 or be confused with their deltas.
        if snapshot.trigger_id in self.required_trigger_ids:
            trigger_index = self.required_trigger_ids.index(snapshot.trigger_id)
            tag = snapshot.trigger_id.removeprefix("AUCTION_")
            expected_anchor_field = "auction_anchor_%s_price_milli" % tag
            if (
                tag in {"0920", "0924", "0925"}
                and self.price_fields is not None
                and self.price_fields[trigger_index] == expected_anchor_field
            ):
                values = snapshot.symbol_states.get(self.scope_id)
                metadata = snapshot.source_observation_metadata
                source_layer = metadata.get("source_id") or "UNKNOWN_SOURCE"
                anchor_fact = build_auction_anchor_fact_v1(
                    trade_date=snapshot.session_id,
                    tag=tag,
                    symbol=self.scope_id,
                    row=values,
                    evaluation_time_ms=snapshot.logical_time_ms,
                    freeze_time_ms=snapshot.logical_time_ms,
                    source_layer=str(source_layer),
                    # Keep this run's source-observation clock distinct from
                    # historical availability, which remains UNKNOWN unless
                    # the source provides direct evidence for it.
                    observed_at_ms=metadata.get("observation_time_ms"),
                )
                trace["auction_anchor_fact"] = anchor_fact.as_trace()

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
            invalid_pairs = []
            if start.logical_time_ms >= middle.logical_time_ms:
                invalid_pairs.append(("0920", "0924", start, middle))
            if middle.logical_time_ms >= end.logical_time_ms:
                invalid_pairs.append(("0924", "0925", middle, end))
            if invalid_pairs:
                valid_segments = self._valid_adjacent_segments(start, middle, end)
                order_diagnostics = {
                    "contract": "AuctionObservationOrderDiagnosticV1",
                    "status": "ORDER_NOT_COMPARABLE",
                    "diagnostic_code": "ANCHOR_OBSERVATION_ORDER_NOT_COMPARABLE",
                    "invalid_pairs": tuple(
                        {
                            "start_tag": start_tag,
                            "end_tag": end_tag,
                            "start_logical_time_ms": left.logical_time_ms,
                            "end_logical_time_ms": right.logical_time_ms,
                            "start_revision_ref": self._revision_ref(left),
                            "end_revision_ref": self._revision_ref(right),
                        }
                        for start_tag, end_tag, left, right in invalid_pairs
                    ),
                    "observation_times_preserved": True,
                    "diagnostic_only": True,
                    "blocks_engine": False,
                }
                fact_content = {
                    "contract": "AuctionFactShadowV1",
                    "status": "PARTIAL",
                    "reason_codes": ("ANCHOR_OBSERVATION_ORDER_NOT_COMPARABLE",),
                    "available_adjacent_segments": valid_segments,
                    "order_diagnostics": order_diagnostics,
                }
                trace.update(
                    {
                        "fact_status": "PARTIAL",
                        "reason_codes": (
                            "ANCHOR_OBSERVATION_ORDER_NOT_COMPARABLE",
                        ),
                        "auction_order_diagnostics": order_diagnostics,
                        "auction_fact_shadow": {
                            **fact_content,
                            "scope_type": self.scope_type,
                            "scope_id": self.scope_id,
                            "decision_status": "FACT_ONLY",
                            "content_hash": semantic_hash(fact_content),
                        },
                    }
                )
            else:
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
                    price_fields=self.price_fields,
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
                    for trigger_refs in self._snapshot_evidence_refs.values()
                    for snapshot_refs in trigger_refs
                    for evidence_ref in snapshot_refs
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
    def _revision_tag(trigger_id: str) -> Optional[str]:
        if trigger_id in {"AUCTION_0920", "AUCTION_0924", "AUCTION_0925"}:
            return trigger_id.removeprefix("AUCTION_")
        return None

    @staticmethod
    def _revision_ref(snapshot: EngineSnapshot) -> Optional[dict[str, Any]]:
        raw = snapshot.source_observation_metadata.get("auction_revision_ref")
        if not isinstance(raw, Mapping):
            return None
        revision = raw.get("revision")
        content_hash = raw.get("content_hash")
        tag = raw.get("tag")
        if (
            raw.get("contract") != "AuctionAnchorRevisionRefV1"
            or not isinstance(tag, str)
            or type(revision) is not int
            or revision <= 0
            or not isinstance(content_hash, str)
            or not content_hash
        ):
            return None
        return dict(raw)

    def _record_anchor_snapshot(self, snapshot: EngineSnapshot) -> None:
        trigger_id = snapshot.trigger_id
        previous = self._snapshots.get(trigger_id)
        if previous is not None and previous.content_hash == snapshot.content_hash:
            return

        tag = self._revision_tag(trigger_id)
        incoming_ref = self._revision_ref(snapshot)
        previous_ref = self._revision_ref(previous) if previous is not None else None

        if incoming_ref is None and tag is not None:
            self._revision_diagnostics.append("REVISION_REF_MISSING:%s" % trigger_id)
        elif incoming_ref is not None and incoming_ref.get("tag") != tag:
            self._revision_diagnostics.append("REVISION_REF_TAG_MISMATCH:%s" % trigger_id)

        if previous is not None:
            if (
                tag not in {"0920", "0924", "0925"}
                or snapshot.logical_time_ms <= previous.logical_time_ms
            ):
                raise ValueError("conflicting snapshot for auction trigger")
            if incoming_ref is not None and previous_ref is not None:
                incoming_revision = incoming_ref["revision"]
                previous_revision = previous_ref["revision"]
                if incoming_revision < previous_revision:
                    raise ValueError("auction timeline revision regressed")
                if (
                    incoming_revision == previous_revision
                    and incoming_ref.get("content_hash")
                    != previous_ref.get("content_hash")
                ):
                    self._revision_diagnostics.append(
                        "REVISION_REF_MISMATCH:%s" % trigger_id
                    )
                supersedes = incoming_ref.get("supersedes_revision")
                if (
                    incoming_revision > previous_revision
                    and supersedes is not None
                    and supersedes != previous_revision
                ):
                    self._revision_diagnostics.append(
                        "REVISION_REF_MISMATCH:%s" % trigger_id
                    )

        self._snapshots[trigger_id] = snapshot
        history = self._snapshot_history.setdefault(trigger_id, [])
        history.append(
            {
                "logical_time_ms": snapshot.logical_time_ms,
                "snapshot_hash": snapshot.content_hash,
                "timeline_revision_ref": incoming_ref,
            }
        )
        self._snapshot_evidence_refs.setdefault(trigger_id, []).append(
            tuple(snapshot.evidence_refs)
        )

    def _auction_revision_state(self) -> Mapping[str, Any]:
        refs: dict[str, dict[str, Any]] = {}
        history: dict[str, list[dict[str, Any]]] = {}
        for trigger_id in self.required_trigger_ids:
            tag = self._revision_tag(trigger_id)
            if tag is None:
                continue
            entries = self._snapshot_history.get(trigger_id, [])
            if entries:
                history[trigger_id] = [dict(item) for item in entries]
                latest_ref = entries[-1].get("timeline_revision_ref")
                if isinstance(latest_ref, Mapping):
                    refs[trigger_id] = dict(latest_ref)
        return {
            "contract": "AuctionStrategyRevisionTraceV1",
            "consumed_revision_refs": refs,
            "revision_history": history,
            "diagnostics": tuple(sorted(set(self._revision_diagnostics))),
            "diagnostic_only": True,
            "blocks_downstream": False,
        }

    def _valid_adjacent_segments(
        self,
        start: EngineSnapshot,
        middle: EngineSnapshot,
        end: EngineSnapshot,
    ) -> Mapping[str, Any]:
        fields = self.price_fields or ("price_milli", "price_milli", "price_milli")
        pairs = (
            (
                "0920_to_0924",
                start,
                middle,
                self.previous_segment_id,
                self.previous_coverage_status,
                fields[0],
                fields[1],
            ),
            (
                "0924_to_0925",
                middle,
                end,
                self.current_segment_id,
                self.current_coverage_status,
                fields[1],
                fields[2],
            ),
        )
        segments: dict[str, Any] = {}
        for name, first, last, segment_id, coverage, start_field, end_field in pairs:
            if first.logical_time_ms >= last.logical_time_ms:
                continue
            frame = build_segment_frame(
                segment_id,
                first,
                last,
                scope_type=self.scope_type,
                scope_id=self.scope_id,
                amount_semantics=self.amount_semantics,
                volume_semantics=self.volume_semantics,
                coverage_status=self._coverage_status(first, last, coverage),
                observed_start_time_ms=self._observed_start(first),
                observed_end_time_ms=self._observed_end(last),
                start_price_field=start_field,
                end_price_field=end_field,
            )
            segments[name] = {
                "segment_id": frame.segment_id,
                "start_snapshot_id": frame.start_snapshot_id,
                "end_snapshot_id": frame.end_snapshot_id,
                "start_time_ms": frame.start_time_ms,
                "end_time_ms": frame.end_time_ms,
                "price": {
                    "status": frame.price.status.value,
                    "start_price_milli": frame.price.start_price_milli,
                    "end_price_milli": frame.price.end_price_milli,
                    "return_bp": frame.price.return_bp,
                },
                "volume": {
                    "status": frame.volume.status.value,
                    "amount_delta_yuan": frame.volume.amount_delta_yuan,
                    "volume_delta_lots": frame.volume.volume_delta_lots,
                },
                "order_book": {
                    "status": frame.order_book.status.value,
                    "directional_pressure_yuan": (
                        frame.order_book.directional_pressure_yuan
                    ),
                    "pressure_delta_yuan": frame.order_book.pressure_delta_yuan,
                },
                "quality": {
                    "status": frame.quality.status.value,
                    "missing_fields": frame.quality.missing_fields,
                    "unavailable_groups": frame.quality.unavailable_groups,
                },
                "coverage_status": frame.coverage_status,
                "content_hash": frame.content_hash,
                "evidence_hash": frame.evidence_hash,
                "evidence_refs": frame.evidence_refs,
            }
        return segments

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
