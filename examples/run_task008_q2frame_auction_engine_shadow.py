"""Replay real t1-v2 Q2Frame data through one Core auction Engine session.

The runner reads a frozen Q2FrameV1 JSONL artifact sequentially.  It applies
the current AuctionTimingPolicyV1 first-observable times as event-time timer
signals, including an empty source gap at 09:25:06.  Source timestamps retain
their raw milliseconds in the Q2 payload; replay logical time is floored to
whole seconds so every ``06.xxx`` event is assigned to the 09:25:06 second.

This is replay development evidence, not Rabbit arrival or NORMAL acceptance.
The input is produced by t1-v2; Core does not recompute Q2 or access external
services.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    AuctionShadowStrategy,
    AuctionTimeline,
    AuctionTimingPolicyV1,
    CrossSectionStateV1,
    DeterministicEngine,
    EngineSignal,
    FreshnessPolicy,
    FrozenDataBundle,
    MarketStateReducer,
    OpeningShadowStrategy,
    Q2FrameReplaySource,
    Q2FrameV1,
    SignalKind,
    VirtualClock,
    WindowManager,
    WindowSpec,
    canonical_hash,
    canonical_json,
    build_cross_section_facts,
    build_opening_amount_summary,
    build_opening_limit_state_summary,
    build_opening_plate_amount_summary,
    semantic_hash,
    validate_opening_plate_amount_context,
)
from engine_core.contracts import StrategyResult  # noqa: E402
from engine_core.market_summary import derive_q2_auction_summary  # noqa: E402
from engine_core.q2 import normalize_q2  # noqa: E402


SHANGHAI = ZoneInfo("Asia/Shanghai")
AUCTION_TAGS = ("0920", "0924", "0925")
OPENING_TAG = "OPENING_0932"
OPENING_EVALUATION_LOCAL = "09:32:10"
OPENING_STALE_AFTER_MS = 60_000
AUCTION_PRICE_FIELDS = (
    "auction_anchor_0920_price_milli",
    "auction_anchor_0924_price_milli",
    "auction_anchor_0925_price_milli",
)


def _floor_second(timestamp_ms: int) -> int:
    return timestamp_ms - timestamp_ms % 1000


def _local_datetime(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(SHANGHAI)


def _opening_evaluation_ms(trade_date: str) -> int:
    local = datetime.fromisoformat(f"{trade_date}T{OPENING_EVALUATION_LOCAL}")
    if local.tzinfo is None:
        local = local.replace(tzinfo=SHANGHAI)
    return int(local.astimezone(timezone.utc).timestamp() * 1000)


def _iter_raw(path: Path) -> Iterable[Mapping[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"Q2Frame line {line_no} must be an object")
            yield value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(path: Path, trade_date: str) -> dict[str, Any]:
    frame_count = update_count = empty_frame_count = 0
    missing_update_ts_count = subsecond_update_ts_count = 0
    frame_ts_mismatch_count = 0
    duplicate_symbol_occurrences = 0
    frame_minus_source_time = {
        "future": 0,
        "zero_to_under_3s": 0,
        "3s_to_under_60s": 0,
        "60s_or_more": 0,
        "min": None,
        "max": None,
    }
    symbols: set[str] = set()
    first_raw_ms = last_raw_ms = None
    previous_seq = 0
    previous_raw_ms = 0
    for raw in _iter_raw(path):
        frame = Q2FrameV1.from_mapping(raw)
        if frame.seq_no != previous_seq + 1:
            raise ValueError("Q2Frame seq_no must be continuous from 1")
        if frame.logical_ts_ms < previous_raw_ms:
            raise ValueError("Q2Frame logical timestamps moved backwards")
        if _local_datetime(frame.logical_ts_ms).date().isoformat() != trade_date:
            raise ValueError("Q2Frame logical time is outside requested trade_date")
        previous_seq = frame.seq_no
        previous_raw_ms = frame.logical_ts_ms
        frame_count += 1
        update_count += len(frame.q2_updates)
        if not frame.q2_updates:
            empty_frame_count += 1
        if first_raw_ms is None:
            first_raw_ms = frame.logical_ts_ms
        last_raw_ms = frame.logical_ts_ms
        frame_second = _floor_second(frame.logical_ts_ms)
        symbols_in_frame: set[str] = set()
        for update in frame.q2_updates:
            symbol = str(update["symbol"])
            symbols.add(symbol)
            if symbol in symbols_in_frame:
                duplicate_symbol_occurrences += 1
            symbols_in_frame.add(symbol)
            source_ms = update.get("ts")
            if isinstance(source_ms, bool) or not isinstance(source_ms, int):
                missing_update_ts_count += 1
                continue
            if source_ms % 1000:
                subsecond_update_ts_count += 1
            if _floor_second(source_ms) != frame_second:
                frame_ts_mismatch_count += 1
            delta_ms = frame.logical_ts_ms - source_ms
            if delta_ms < 0:
                frame_minus_source_time["future"] += 1
            elif delta_ms < 3_000:
                frame_minus_source_time["zero_to_under_3s"] += 1
            elif delta_ms < 60_000:
                frame_minus_source_time["3s_to_under_60s"] += 1
            else:
                frame_minus_source_time["60s_or_more"] += 1
            min_delta = frame_minus_source_time["min"]
            max_delta = frame_minus_source_time["max"]
            frame_minus_source_time["min"] = (
                delta_ms if min_delta is None else min(min_delta, delta_ms)
            )
            frame_minus_source_time["max"] = (
                delta_ms if max_delta is None else max(max_delta, delta_ms)
            )
    if frame_count == 0:
        raise ValueError("Q2Frame artifact is empty")
    return {
        "frame_count": frame_count,
        "update_count": update_count,
        "empty_frame_count": empty_frame_count,
        "non_empty_frame_count": frame_count - empty_frame_count,
        "symbol_count": len(symbols),
        "symbols": tuple(sorted(symbols)),
        "first_logical_ts_ms": first_raw_ms,
        "last_logical_ts_ms": last_raw_ms,
        "missing_update_ts_count": missing_update_ts_count,
        "subsecond_update_ts_count": subsecond_update_ts_count,
        "frame_second_mismatch_count": frame_ts_mismatch_count,
        "duplicate_symbol_occurrences_within_frame": duplicate_symbol_occurrences,
        "frame_minus_source_time_ms": {
            **frame_minus_source_time,
            "meaning": (
                "frame logical timestamp minus per-symbol source timestamp; "
                "not arrival latency"
            ),
        },
        "universe_basis": "UNIQUE_SYMBOLS_IN_FROZEN_Q2FRAME_ONLY",
    }


def _groups_by_replay_second(path: Path) -> Iterable[tuple[int, tuple[Q2FrameV1, ...]]]:
    group_second: int | None = None
    group: list[Q2FrameV1] = []
    for raw in _iter_raw(path):
        frame = Q2FrameV1.from_mapping(raw)
        replay_second = _floor_second(frame.logical_ts_ms)
        if group_second is not None and replay_second != group_second:
            yield group_second, tuple(group)
            group = []
        group_second = replay_second
        group.append(frame)
    if group_second is not None:
        yield group_second, tuple(group)


class _AllSymbolAuctionShadow:
    """Compose auction and optional opening facts under one Engine."""

    strategy_id = "q2frame-all-symbol-auction-shadow-v1"

    def __init__(self, symbols: tuple[str, ...], *, include_opening: bool = False) -> None:
        self._strategies = {
            symbol: AuctionShadowStrategy(
                scope_id=symbol,
                start_trigger_id="AUCTION_0920",
                middle_trigger_id="AUCTION_0924",
                end_trigger_id="AUCTION_0925",
                previous_segment_id=f"q2frame_{symbol}_0920_to_0924",
                current_segment_id=f"q2frame_{symbol}_0924_to_0925",
                price_fields=AUCTION_PRICE_FIELDS,
            )
            for symbol in symbols
        }
        self._opening_strategies = (
            {symbol: OpeningShadowStrategy(scope_id=symbol) for symbol in symbols}
            if include_opening
            else {}
        )

    def evaluate(self, snapshot: Any, bundle: FrozenDataBundle) -> StrategyResult:
        is_opening = snapshot.trigger_id == OPENING_TAG
        strategies = self._opening_strategies if is_opening else self._strategies
        if is_opening and not strategies:
            raise ValueError("OPENING_0932 was not enabled for this replay")
        child_results = {
            symbol: strategy.evaluate(snapshot, bundle)
            for symbol, strategy in strategies.items()
        }
        status_counts: Counter[str] = Counter()
        facts_by_symbol: dict[str, Any] = {}
        anchor_facts_by_symbol: dict[str, Any] = {}
        anchor_fact_status_counts: Counter[str] = Counter()
        opening_field_status_by_symbol: dict[str, Any] = {}
        opening_field_status_counts: dict[str, Counter[str]] = {}
        child_hashes: dict[str, str] = {}
        evidence_refs: set[str] = set()
        for symbol, result in child_results.items():
            child_hashes[symbol] = result.content_hash
            evidence_refs.update(result.evidence_refs)
            status = result.trace.get("fact_status", "UNKNOWN")
            status_counts[getattr(status, "value", str(status))] += 1
            anchor_fact = result.trace.get("auction_anchor_fact")
            if isinstance(anchor_fact, Mapping):
                anchor_facts_by_symbol[symbol] = anchor_fact
                anchor_fact_status_counts[str(anchor_fact.get("status", "UNKNOWN"))] += 1
            fact = (
                result.trace.get("opening_fact")
                if is_opening
                else result.trace.get("auction_fact_shadow")
            )
            if fact is not None:
                facts_by_symbol[symbol] = fact
            if is_opening:
                field_status = result.trace.get("opening_fact_field_status")
                if field_status is not None:
                    opening_field_status_by_symbol[symbol] = field_status
                    for field_name, field_status_value in field_status.items():
                        opening_field_status_counts.setdefault(
                            field_name, Counter()
                        )[str(field_status_value)] += 1

        trigger_id = snapshot.trigger_id
        trace: dict[str, Any] = {
            "state": "OBSERVE",
            "decision_status": "FACT_ONLY",
            "strategy_id": (
                "q2frame-all-symbol-opening-shadow-v1"
                if is_opening
                else self.strategy_id
            ),
            "trigger_id": trigger_id,
            "logical_time_ms": snapshot.logical_time_ms,
            "snapshot_hash": snapshot.content_hash,
            "fact_status_counts": dict(sorted(status_counts.items())),
            "fact_status_scope": (
                "change_pct_and_source_time"
                if is_opening
                else "adjacent_auction_comparison"
            ),
            "expected_q2frame_symbol_count": len(strategies),
            "observed_symbol_count": len(snapshot.symbol_states),
            "coverage": snapshot.coverage,
            "completeness": snapshot.completeness,
            "source_observation_metadata": snapshot.source_observation_metadata,
            "per_symbol_result_hashes": child_hashes,
        }
        if trigger_id == "AUCTION_0925":
            trace["facts_by_symbol"] = facts_by_symbol
            trace["facts_by_symbol_hash"] = semantic_hash(facts_by_symbol)
        if anchor_facts_by_symbol:
            trace["anchor_fact_status_scope"] = "standalone_current_anchor"
            trace["auction_anchor_facts_by_symbol"] = anchor_facts_by_symbol
            trace["auction_anchor_facts_by_symbol_hash"] = semantic_hash(
                anchor_facts_by_symbol
            )
            trace["auction_anchor_fact_status_counts"] = dict(
                sorted(anchor_fact_status_counts.items())
            )
        if is_opening:
            trace["fact_status_scope"] = "change_pct_and_source_time"
            trace["facts_by_symbol"] = facts_by_symbol
            trace["facts_by_symbol_hash"] = semantic_hash(facts_by_symbol)
            trace["opening_fact_field_status_by_symbol"] = (
                opening_field_status_by_symbol
            )
            trace["opening_fact_field_status_counts"] = {
                field_name: dict(sorted(counts.items()))
                for field_name, counts in sorted(opening_field_status_counts.items())
            }
            trace["opening_fact_field_status_hash"] = semantic_hash(
                opening_field_status_by_symbol
            )
            trace["stale_symbol_count"] = len(
                snapshot.source_observation_metadata.get("stale_symbols", ())
            )
            trace["missing_symbol_count"] = len(
                snapshot.source_observation_metadata.get("missing_symbols", ())
            )
        return StrategyResult(
            strategy_id=self.strategy_id,
            evaluation_id=bundle.evaluation_id,
            state="OBSERVE",
            trace=trace,
            evidence_refs=tuple(sorted(evidence_refs)),
            content_hash=semantic_hash(trace),
        )


def _new_engine(
    trade_date: str,
    symbols: tuple[str, ...],
    first_ms: int,
    last_ms: int,
    *,
    include_opening: bool = False,
) -> DeterministicEngine:
    strategy = _AllSymbolAuctionShadow(symbols, include_opening=include_opening)
    return DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("q2frame_replay", first_ms, last_ms + 1),)),
        strategy,
        session_id=trade_date,
        phase="REPLAY",
    )


def _run_once(
    path: Path,
    *,
    trade_date: str,
    inventory: Mapping[str, Any],
    input_sha256: str,
    include_opening: bool = False,
    plate_amount_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    symbols = tuple(inventory["symbols"])
    policies = {tag: AuctionTimingPolicyV1.default(tag) for tag in AUCTION_TAGS}
    barriers = {
        tag: policies[tag].at(trade_date)["first_observable_ms"]
        for tag in AUCTION_TAGS
    }
    if include_opening:
        barriers[OPENING_TAG] = _opening_evaluation_ms(trade_date)
    ordered_barriers = tuple(sorted(barriers.items(), key=lambda item: (item[1], item[0])))
    first_ms = _floor_second(int(inventory["first_logical_ts_ms"]))
    final_ms = ordered_barriers[-1][1]
    initial_clock = datetime.fromtimestamp(first_ms / 1000, timezone.utc)
    clock = VirtualClock(initial_clock)
    freshness_policy = (
        FreshnessPolicy(
            stale_after_ms=OPENING_STALE_AFTER_MS,
            max_future_skew_ms=999,
        )
        if include_opening
        else FreshnessPolicy()
    )
    source = Q2FrameReplaySource(
        trade_date,
        symbols,
        clock,
        freshness_policy=freshness_policy,
        source_id="t1_v2_q2frame",
    )
    engine = _new_engine(
        trade_date,
        symbols,
        first_ms,
        final_ms,
        include_opening=include_opening,
    )
    auction_timeline = AuctionTimeline(trade_date)
    anchor_evidence: dict[str, dict[str, Any]] = {}
    opening_evidence: dict[str, dict[str, Any]] = {}
    frame_count = update_count = 0
    latest_q2_update_by_symbol: dict[str, Mapping[str, Any]] = {}
    last_raw_frame_ms = None
    last_raw_update_ms = None
    first_excluded_frame: dict[str, Any] | None = None
    barrier_index = 0

    def capture_barrier(tag: str, logical_ms: int, result: Any) -> None:
        snapshot = result.snapshots[-1]
        strategy_result = result.strategy_results[-1]
        base = {
            "first_observable_ms": logical_ms,
            "first_observable_local": _local_datetime(logical_ms).isoformat(),
            "engine_snapshot_hash": snapshot.content_hash,
            "engine_revision": snapshot.market_state_revision,
            "engine_completeness": snapshot.completeness,
            "coverage": snapshot.coverage,
            "observed_symbol_count": len(snapshot.symbol_states),
            "expected_q2frame_symbol_count": len(symbols),
            "oldest_source_time_ms": snapshot.source_observation_metadata.get(
                "oldest_source_time_ms"
            ),
            "newest_source_time_ms": snapshot.source_observation_metadata.get(
                "newest_source_time_ms"
            ),
            "input_frames_included": frame_count,
            "input_updates_included": update_count,
            "last_raw_frame_time_ms": last_raw_frame_ms,
            "last_raw_update_time_ms": last_raw_update_ms,
            "strategy_result_hash": strategy_result.content_hash,
            "fact_status_counts": strategy_result.trace.get("fact_status_counts", {}),
            "fact_status_scope": strategy_result.trace.get("fact_status_scope"),
            "anchor_fact_status_scope": strategy_result.trace.get(
                "anchor_fact_status_scope"
            ),
            "facts_by_symbol_hash": strategy_result.trace.get("facts_by_symbol_hash"),
            "facts_by_symbol": strategy_result.trace.get("facts_by_symbol", {}),
            "auction_anchor_facts_by_symbol_hash": strategy_result.trace.get(
                "auction_anchor_facts_by_symbol_hash"
            ),
            "auction_anchor_fact_status_counts": strategy_result.trace.get(
                "auction_anchor_fact_status_counts", {}
            ),
            "auction_anchor_facts_by_symbol": strategy_result.trace.get(
                "auction_anchor_facts_by_symbol", {}
            ),
            "processed_signals": result.processed_signals,
            "reducer_revision": engine._reducer.state.revision,
            "virtual_clock_ms": int(clock.now_utc().timestamp() * 1000),
        }
        if tag == OPENING_TAG:
            expected_symbols = tuple(sorted(symbols))
            observed_symbols = tuple(
                sorted(set(snapshot.symbol_states).intersection(expected_symbols))
            )
            missing_symbols = tuple(
                symbol
                for symbol in expected_symbols
                if symbol not in set(observed_symbols)
            )
            frame_completeness = (
                "EMPTY"
                if not observed_symbols
                else "PARTIAL"
                if missing_symbols
                else "COMPLETE"
            )
            source_metadata = snapshot.source_observation_metadata
            cohort_state = CrossSectionStateV1(
                trade_date=trade_date,
                frame_no=max(0, frame_count - 1),
                logical_ts_ms=logical_ms,
                expected_symbols=expected_symbols,
                updated_symbols=observed_symbols,
                missing_symbols=missing_symbols,
                symbol_states={
                    symbol: snapshot.symbol_states[symbol]
                    for symbol in observed_symbols
                },
                frame_completeness=frame_completeness,
                coverage=(
                    len(observed_symbols) / float(len(expected_symbols))
                    if expected_symbols
                    else 0.0
                ),
                source_time_min_ms=source_metadata.get("oldest_source_time_ms"),
                source_time_max_ms=source_metadata.get("newest_source_time_ms"),
            )
            cross_section = build_cross_section_facts(
                cohort_state,
                scope="OBSERVED_COHORT",
                source_layers=("t1_v2_q2frame_event_time_replay",),
            )
            cross_section_evidence = {
                "contract": "CrossSectionFactsV1",
                "frame_no": cross_section.frame_no,
                "logical_ts_ms": cross_section.logical_ts_ms,
                "scope": cross_section.scope,
                "scope_authority": "Q2FRAME_INPUT_COHORT_ONLY_NOT_FULL_MARKET",
                "expected_count": cross_section.expected_count,
                "observed_count": cross_section.observed_count,
                "missing_count": cross_section.missing_count,
                "coverage": cross_section.coverage,
                "field_denominators": dict(cross_section.field_denominators),
                "market_breadth": dict(cross_section.market_breadth),
                "source_layers": list(cross_section.source_layers),
                "stale_symbol_count": len(source_metadata.get("stale_symbols", ())),
                "breadth_includes_stale_observed_quotes": True,
                "content_hash": cross_section.content_hash,
                "fact_only": cross_section.fact_only,
            }
            base.update(
                {
                    "contract": "OpeningFactV1",
                    "cross_section_facts": cross_section_evidence,
                    "fact_status_scope": strategy_result.trace.get(
                        "fact_status_scope"
                    ),
                    "evaluation_time_ms": logical_ms,
                    "expected_symbol_count": len(symbols),
                    "stale_symbol_count": strategy_result.trace.get(
                        "stale_symbol_count", 0
                    ),
                    "missing_symbol_count": strategy_result.trace.get(
                        "missing_symbol_count", 0
                    ),
                    "opening_fact_field_status_by_symbol": strategy_result.trace.get(
                        "opening_fact_field_status_by_symbol", {}
                    ),
                    "opening_fact_field_status_counts": strategy_result.trace.get(
                        "opening_fact_field_status_counts", {}
                    ),
                    "opening_fact_field_status_hash": strategy_result.trace.get(
                        "opening_fact_field_status_hash"
                    ),
                    "amount_2m_summary": build_opening_amount_summary(
                        strategy_result.trace.get("facts_by_symbol", {}),
                        expected_symbols=expected_symbols,
                        scope="OBSERVED_COHORT",
                    ),
                    "limit_state_summary": build_opening_limit_state_summary(
                        strategy_result.trace.get("facts_by_symbol", {}),
                        expected_symbols=expected_symbols,
                        scope="OBSERVED_COHORT",
                    ),
                    "freshness_policy": {
                        "stale_after_ms": OPENING_STALE_AFTER_MS,
                        "max_future_skew_ms": 999,
                    },
                    "universe_basis": inventory["universe_basis"],
                    "availability_status": "UNKNOWN_NOT_INFERRED",
                }
            )
            if plate_amount_context is not None:
                base["plate_amount_context"] = {
                    "contract": plate_amount_context["contract"],
                    "trade_date": plate_amount_context["trade_date"],
                    "content_hash": plate_amount_context["content_hash"],
                    "source_provenance": plate_amount_context["source_provenance"],
                    "selected_plates": plate_amount_context["selected_plates"],
                }
                base["plate_amount_summary"] = build_opening_plate_amount_summary(
                    strategy_result.trace.get("facts_by_symbol", {}),
                    mapped_symbols_by_plate=plate_amount_context[
                        "mapped_symbols_by_plate"
                    ],
                    auction_symbols_by_plate=plate_amount_context[
                        "auction_symbols_by_plate"
                    ],
                    auction_top1_amount_ratio_by_plate=plate_amount_context[
                        "auction_top1_amount_ratio_by_plate"
                    ],
                    selected_plates=plate_amount_context["selected_plates"],
                )
            opening_evidence[tag] = base
        else:
            base["auction_revision"] = auction_revision_summary(
                tag,
                snapshot,
                logical_ms,
            )
            if tag == "0925":
                q2_quotes = {
                    symbol: normalize_q2(symbol, update)
                    for symbol, update in latest_q2_update_by_symbol.items()
                }
                q2_summary = derive_q2_auction_summary(
                    q2_quotes,
                    trade_date=trade_date,
                    source_id=f"q2frame-sha256:{input_sha256}",
                    source_table="Q2FrameV1:event_time_replay",
                    # The frozen artifact's observed symbols are not an
                    # authoritative full-market universe at this barrier.
                    expected_symbols=None,
                    observation_time_ms=None,
                    input_content_hash=input_sha256,
                    evidence_refs=(input_sha256,),
                )
                base["q2_auction_summary"] = q2_summary.as_mapping()
            anchor_evidence[tag] = base

    def auction_revision_summary(
        tag: str,
        snapshot: Any,
        evaluation_time_ms: int,
    ) -> dict[str, Any]:
        rows = {
            symbol: {
                **dict(values),
                "source_time_ms": values.get("source_record_time_ms"),
            }
            for symbol, values in snapshot.symbol_states.items()
        }
        revision = auction_timeline.observe(
            tag,
            rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=symbols,
            observed_at_ms=None,
            source_layers=("t1_v2_q2frame_event_time_replay",),
        )
        analysis_bundle = auction_timeline.build_analysis_bundle(tag)
        recovery_plan = analysis_bundle["recovery_plan"]
        recovery_plan_summary = (
            None
            if recovery_plan is None
            else {
                "contract": "RecoveryPlanV1",
                "plan_id": recovery_plan.plan_id,
                "requested_symbols": list(recovery_plan.requested_symbols),
                "missing_fields": list(recovery_plan.missing_fields),
                "current_revision": recovery_plan.current_revision,
                "requested_at_ms": recovery_plan.requested_at_ms,
                "preferred_sources": list(recovery_plan.preferred_sources),
                "idempotency_key": recovery_plan.idempotency_key,
                "soft_deadline_ms": recovery_plan.soft_deadline_ms,
                "content_hash": recovery_plan.content_hash,
                "recovery_state": recovery_plan.recovery_state,
            }
        )
        return {
            "contract": "AuctionAnchorRevisionV2",
            "revision": revision.revision,
            "state": revision.state,
            "business_anchor_ms": revision.business_anchor_ms,
            "first_observable_ms": revision.first_observable_ms,
            "preferred_finalize_ms": revision.preferred_finalize_ms,
            "soft_deadline_ms": revision.soft_deadline_ms,
            "expected_symbol_count": len(revision.expected_symbols),
            "prior_deltas": analysis_bundle["prior_deltas"],
            "source_observed_symbol_count": len(revision.source_observed_symbols),
            "source_missing_symbol_count": len(revision.source_missing_symbols),
            "source_coverage": revision.source_coverage,
            "anchor_available_symbol_count": len(revision.available_anchor_symbols),
            "missing_anchor_symbol_count": len(revision.missing_anchor_symbols),
            "anchor_coverage": revision.anchor_coverage,
            # Compatibility field retains the V1 name, now explicitly scoped
            # to required anchor-field coverage by the V2 contract.
            "coverage": revision.anchor_coverage,
            "recovery_required": analysis_bundle["recovery_required"],
            "recovery_plan": recovery_plan_summary,
            "recovery_execution": (
                "NOT_RUN_BY_CORE"
                if recovery_plan is not None
                else "NOT_REQUIRED"
            ),
            "source_layers": revision.source_layers,
            "observed_at_ms": revision.observed_at_ms,
            "evaluation_time_ms": revision.evaluation_time_ms,
            "freeze_time_ms": revision.freeze_time_ms,
            "source_time_min_ms": revision.source_time_min_ms,
            "source_time_max_ms": revision.source_time_max_ms,
            "late_execution": revision.late_execution,
            "supersedes_revision": revision.supersedes_revision,
            "recovery_state": revision.recovery_state,
            "observations_hash": revision.observations_hash,
            "content_hash": revision.content_hash,
            "evidence_hash": revision.evidence_hash,
        }

    def fire_barrier(tag: str, logical_ms: int) -> None:
        nonlocal barrier_index
        clock.advance_to(datetime.fromtimestamp(logical_ms / 1000, timezone.utc))
        trigger_id = OPENING_TAG if tag == OPENING_TAG else f"AUCTION_{tag}"
        engine.submit(
            EngineSignal(
                signal_id=f"q2frame-timer:{tag}",
                logical_time_ms=logical_ms,
                signal_seq=1_000_000_000 + barrier_index,
                signal_kind=SignalKind.TIMER,
                payload={"trigger_id": trigger_id},
            )
        )
        result = engine.run_until_empty()
        capture_barrier(tag, logical_ms, result)
        barrier_index += 1

    for replay_second, group in _groups_by_replay_second(path):
        if replay_second > final_ms:
            first = group[0]
            first_excluded_frame = {
                "seq_no": first.seq_no,
                "raw_logical_ts_ms": first.logical_ts_ms,
                "raw_logical_time_local": _local_datetime(first.logical_ts_ms).isoformat(),
                "replay_second_ms": replay_second,
                "update_count": len(first.q2_updates),
                "excluded_reason": (
                    "AFTER_OPENING_0932_EVALUATION_SECOND"
                    if include_opening
                    else "AFTER_0925_FIRST_OBSERVABLE_SECOND"
                ),
            }
            break

        while (
            barrier_index < len(ordered_barriers)
            and ordered_barriers[barrier_index][1] < replay_second
        ):
            tag, logical_ms = ordered_barriers[barrier_index]
            fire_barrier(tag, logical_ms)

        for frame in group:
            raw_signal = source.signal_for(frame, signal_prefix="t1-v2-q2frame")
            replay_signal = EngineSignal(
                signal_id=raw_signal.signal_id,
                logical_time_ms=replay_second,
                signal_seq=raw_signal.signal_seq,
                signal_kind=raw_signal.signal_kind,
                payload=raw_signal.payload,
            )
            source.advance_before_consume(replay_signal)
            engine.submit(replay_signal)
            frame_count += 1
            update_count += len(frame.q2_updates)
            for update in frame.q2_updates:
                symbol = str(update.get("symbol", "")).strip()
                if symbol:
                    latest_q2_update_by_symbol[symbol] = update
            last_raw_frame_ms = frame.logical_ts_ms
            update_times = [
                int(update["ts"])
                for update in frame.q2_updates
                if isinstance(update.get("ts"), int)
                and not isinstance(update.get("ts"), bool)
            ]
            if update_times:
                frame_last_update_ms = max(update_times)
                last_raw_update_ms = (
                    frame_last_update_ms
                    if last_raw_update_ms is None
                    else max(last_raw_update_ms, frame_last_update_ms)
                )

        matching_barrier = (
            ordered_barriers[barrier_index]
            if barrier_index < len(ordered_barriers)
            else None
        )
        if matching_barrier is not None and matching_barrier[1] == replay_second:
            tag, logical_ms = matching_barrier
            # Engine signal priority applies every Q2Frame update in this whole
            # second before the same-time auction timer.
            clock.advance_to(datetime.fromtimestamp(logical_ms / 1000, timezone.utc))
            trigger_id = OPENING_TAG if tag == OPENING_TAG else f"AUCTION_{tag}"
            engine.submit(
                EngineSignal(
                    signal_id=f"q2frame-timer:{tag}",
                    logical_time_ms=logical_ms,
                    signal_seq=1_000_000_000 + barrier_index,
                    signal_kind=SignalKind.TIMER,
                    payload={"trigger_id": trigger_id},
                )
            )
        result = engine.run_until_empty()
        if matching_barrier is not None and matching_barrier[1] == replay_second:
            tag, logical_ms = matching_barrier
            capture_barrier(tag, logical_ms, result)
            barrier_index += 1

    while barrier_index < len(ordered_barriers):
        tag, logical_ms = ordered_barriers[barrier_index]
        fire_barrier(tag, logical_ms)

    if tuple(anchor_evidence) != AUCTION_TAGS:
        raise RuntimeError("not all three auction barriers produced Engine snapshots")
    if include_opening and tuple(opening_evidence) != (OPENING_TAG,):
        raise RuntimeError("OPENING_0932 did not produce an Engine snapshot")
    final_state = engine._reducer.state
    return {
        "anchor_evidence": anchor_evidence,
        "opening_evidence": opening_evidence,
        "first_excluded_frame": first_excluded_frame,
        "input_frames_processed": frame_count,
        "input_updates_processed": update_count,
        "processed_signals": engine.run_until_empty().processed_signals,
        "reducer_revision": final_state.revision,
        "final_state_hash": canonical_hash(
            {
                "revision": final_state.revision,
                "logical_time_ms": final_state.logical_time_ms,
                "symbols": final_state.symbol_states,
                "source": final_state.source_observation_metadata,
                "coverage": final_state.coverage,
                "completeness": final_state.completeness,
            }
        ),
        "engine_instances": 1,
        "virtual_clock_ms": int(clock.now_utc().timestamp() * 1000),
    }


def run_q2frame_auction_engine_shadow(
    *,
    q2frame_path: Path,
    trade_date: str,
    expected_sha256: str | None = None,
    include_opening: bool = False,
    plate_amount_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a pinned Q2Frame auction replay, optionally through opening, twice."""

    if plate_amount_context is not None:
        if not include_opening:
            raise ValueError("plate amount context requires include_opening=True")
        plate_amount_context = validate_opening_plate_amount_context(
            plate_amount_context,
            trade_date=trade_date,
        )

    initial_sha = _file_sha256(q2frame_path)
    if expected_sha256 is not None and initial_sha != expected_sha256:
        raise ValueError("Q2Frame SHA-256 does not match the pinned artifact")
    inventory = _inventory(q2frame_path, trade_date)
    after_inventory_sha = _file_sha256(q2frame_path)
    first = _run_once(
        q2frame_path,
        trade_date=trade_date,
        inventory=inventory,
        input_sha256=initial_sha,
        include_opening=include_opening,
        plate_amount_context=plate_amount_context,
    )
    between_runs_sha = _file_sha256(q2frame_path)
    repeat = _run_once(
        q2frame_path,
        trade_date=trade_date,
        inventory=inventory,
        input_sha256=initial_sha,
        include_opening=include_opening,
        plate_amount_context=plate_amount_context,
    )
    final_sha = _file_sha256(q2frame_path)
    input_stable = len(
        {initial_sha, after_inventory_sha, between_runs_sha, final_sha}
    ) == 1
    compared_fields = (
        "anchor_evidence",
        "opening_evidence",
        "input_frames_processed",
        "input_updates_processed",
        "processed_signals",
        "reducer_revision",
        "final_state_hash",
        "virtual_clock_ms",
    )
    determinism = {
        field: first[field] == repeat[field]
        for field in compared_fields
    }
    determinism["input_sha256_stable"] = input_stable
    deterministic = all(determinism.values())
    return {
        "contract_version": (
            "Task008Q2FrameSessionEngineShadowV7"
            if plate_amount_context is not None
            else
            "Task008Q2FrameSessionEngineShadowV6"
            if include_opening
            else "Task008Q2FrameAuctionEngineShadowV5"
        ),
        "trade_date": trade_date,
        "run_mode": "REAL_T1V2_Q2FRAME_EVENT_TIME_REPLAY",
        "q2frame": {
            "path": str(q2frame_path),
            "sha256": final_sha,
            "expected_sha256": expected_sha256,
            "source": "t1-v2 exact-release local Q2Frame output",
        },
        "inventory": inventory,
        "ordered": first,
        "repeat": repeat,
        "determinism": determinism,
        "deterministic": deterministic,
        "auction_timing_policy": {
            tag: {
                "policy_version": policies.policy_version,
                "business_anchor_ms": policies.at(trade_date)["business_anchor_ms"],
                "first_observable_ms": policies.at(trade_date)["first_observable_ms"],
                "preferred_finalize_ms": policies.at(trade_date)["preferred_finalize_ms"],
                "soft_deadline_ms": policies.at(trade_date)["soft_deadline_ms"],
            }
            for tag in AUCTION_TAGS
            for policies in (AuctionTimingPolicyV1.default(tag),)
        },
        "opening_timing_policy": (
            {
                "trigger_id": OPENING_TAG,
                "business_anchor_local": "09:32:00",
                "evaluation_local": OPENING_EVALUATION_LOCAL,
                "evaluation_time_ms": _opening_evaluation_ms(trade_date),
                "source_availability": "UNKNOWN_NOT_INFERRED",
            }
            if include_opening
            else None
        ),
        "plate_amount_context": (
            {
                "contract": plate_amount_context["contract"],
                "trade_date": plate_amount_context["trade_date"],
                "content_hash": plate_amount_context["content_hash"],
                "source_provenance": plate_amount_context["source_provenance"],
                "selected_plates": plate_amount_context["selected_plates"],
            }
            if plate_amount_context is not None
            else None
        ),
        "auction_price_field_policy": {
            tag: field for tag, field in zip(AUCTION_TAGS, AUCTION_PRICE_FIELDS)
        },
        "missing_auction_price_behavior": "MISSING_NO_FALLBACK_TO_LATEST_PX",
        "replay_time_policy": (
            "logical frame and timer times are floored to whole seconds; "
            "raw Q2Frame/update source milliseconds are retained in payload/evidence"
        ),
        "historical_available_at": "UNKNOWN_NOT_INFERRED",
        "rabbit_arrival_order": "UNKNOWN_NOT_INFERRED",
        "rabbit_delivery_membership": "UNKNOWN_NOT_INFERRED",
        "normal_opening_acceptance": "NOT_EVALUATED",
        "decision_status": "FACT_ONLY",
        "production_side_effects": "NONE; local Q2Frame read and in-memory Core Engine only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q2frame", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument(
        "--include-opening",
        action="store_true",
        help="continue the same Engine/Q2Frame replay through 09:32:10",
    )
    parser.add_argument(
        "--plate-amount-context",
        type=Path,
        help="date-pinned OpeningPlateAmountContextV1 JSON input",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plate_amount_context = (
        json.loads(args.plate_amount_context.read_text(encoding="utf-8"))
        if args.plate_amount_context is not None
        else None
    )
    result = run_q2frame_auction_engine_shadow(
        q2frame_path=args.q2frame,
        trade_date=args.trade_date,
        expected_sha256=args.expected_sha256,
        include_opening=args.include_opening,
        plate_amount_context=plate_amount_context,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(canonical_json(result))
        output.write("\n")
    print(
        canonical_json(
            {
                "output": str(args.output),
                "q2frame_sha256": result["q2frame"]["sha256"],
                "symbols": result["inventory"]["symbol_count"],
                "frames_processed_through_evaluation": result["ordered"][
                    "input_frames_processed"
                ],
                "updates_processed_through_evaluation": result["ordered"][
                    "input_updates_processed"
                ],
                "deterministic": result["deterministic"],
                "anchors": {
                    tag: result["ordered"]["anchor_evidence"][tag]["fact_status_counts"]
                    for tag in AUCTION_TAGS
                },
                "opening_fact_status_counts": (
                    result["ordered"]["opening_evidence"][OPENING_TAG][
                        "fact_status_counts"
                    ]
                    if args.include_opening
                    else None
                ),
            }
        )
    )
    return 0 if result["deterministic"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
