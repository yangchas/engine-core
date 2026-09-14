"""Small deterministic signal queue and reducer orchestration."""

from __future__ import annotations

import heapq
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Mapping, Optional, Protocol, Tuple

from .contracts import (
    EngineSignal,
    EngineSnapshot,
    FrozenDataBundle,
    DataStatus,
    SignalKind,
    StrategyResult,
    canonical_hash,
    deep_freeze,
    semantic_hash,
)
from .state import MarketStateReducer
from .evaluation import EvaluationNode, EvaluationPlan
from .session import SessionPlan
from .windows import WindowManager


DEFAULT_EVALUATION_REGISTRATION_LIMIT = 65_536


class Strategy(Protocol):
    def evaluate(
        self,
        snapshot: EngineSnapshot,
        bundle: FrozenDataBundle,
    ) -> StrategyResult:
        ...


@dataclass(frozen=True)
class EngineRunResult:
    processed_signals: int
    snapshots: Tuple[EngineSnapshot, ...]
    strategy_results: Tuple[StrategyResult, ...]


@dataclass(frozen=True)
class _PendingEvaluation:
    evaluation_id: str
    snapshot: EngineSnapshot
    function_order: Tuple[str, ...]


@dataclass(frozen=True)
class _TerminalEvaluation:
    evaluation_id: str
    submission_hash: str
    completed_logical_time_ms: int


class DeterministicEngine:
    """Single-threaded reducer that is unaware of the input mode.

    ``result_history_limit`` bounds only retained observable output.  The
    cumulative processed count is still reported.  ``signal_id_cache_limit``
    bounds this in-memory idempotency horizon; durable duplicate protection is
    intentionally deferred with journal/checkpoint integration.

    ``_registered_evaluation_ids`` is a bounded, session-lifetime identity
    ledger.  It is deliberately not evicted: evicting it would allow a
    completed evaluation to be registered again after its terminal tombstone
    expires.  When the configured capacity is exhausted the engine fails
    closed instead of silently weakening once-only semantics.  A new engine
    instance (one trading session) is the lifecycle boundary for this ledger;
    durable cross-restart identity belongs to the deferred persistence layer.
    """

    def __init__(
        self,
        reducer: MarketStateReducer,
        windows: WindowManager,
        strategy: Strategy,
        *,
        session_id: str = "",
        phase: str = "UNKNOWN",
        session_plan: Optional[SessionPlan] = None,
        evaluation_plan: Optional[EvaluationPlan] = None,
        result_history_limit: int = 256,
        signal_id_cache_limit: int = 4096,
        evaluation_registration_limit: Optional[int] = DEFAULT_EVALUATION_REGISTRATION_LIMIT,
        terminal_evaluation_limit: int = 4096,
    ) -> None:
        if result_history_limit <= 0:
            raise ValueError("result_history_limit must be positive")
        if signal_id_cache_limit <= 0:
            raise ValueError("signal_id_cache_limit must be positive")
        if evaluation_registration_limit is None:
            evaluation_registration_limit = DEFAULT_EVALUATION_REGISTRATION_LIMIT
        if (
            isinstance(evaluation_registration_limit, bool)
            or not isinstance(evaluation_registration_limit, int)
            or evaluation_registration_limit <= 0
        ):
            raise ValueError("evaluation_registration_limit must be a positive integer")
        if terminal_evaluation_limit <= 0:
            raise ValueError("terminal_evaluation_limit must be positive")
        if session_plan is not None and not isinstance(session_plan, SessionPlan):
            raise TypeError("session_plan must be a SessionPlan")
        if evaluation_plan is not None and not isinstance(
            evaluation_plan, EvaluationPlan
        ):
            raise TypeError("evaluation_plan must be an EvaluationPlan")
        if session_plan is not None and phase != "UNKNOWN":
            raise ValueError("session_plan and explicit phase are mutually exclusive")
        if evaluation_plan is not None:
            strategy_id = getattr(strategy, "strategy_id", None)
            if not isinstance(strategy_id, str) or not strategy_id:
                raise ValueError("planned strategy must expose a non-empty strategy_id")
            for node in evaluation_plan.nodes:
                if node.fact_functions:
                    raise ValueError(
                        "Engine does not execute planned fact_functions yet"
                    )
                if node.strategies != (strategy_id,):
                    raise ValueError(
                        "evaluation node strategies must match the bound strategy"
                    )
        self._reducer = reducer
        self._windows = windows
        self._strategy = strategy
        self._session_id = session_id
        self._phase = phase
        self._session_plan = session_plan
        self._evaluation_plan = evaluation_plan
        self._trace_id = session_id or "engine-trace"
        self._queue: List[Tuple[Tuple[int, int, int, str], EngineSignal]] = []
        # This is intentionally an in-memory bounded idempotency horizon.  A
        # durable dedupe cursor belongs to the deferred journal/checkpoint
        # layer, not this first Engine integration slice.
        self._signal_id_cache_limit = signal_id_cache_limit
        self._submitted_signatures: OrderedDict[str, str] = OrderedDict()
        self._market_frontier: Optional[int] = None
        # Keep only recent observable output so a long drain does not retain
        # every snapshot/result forever.  processed_signals remains cumulative.
        self._snapshots: Deque[EngineSnapshot] = deque(maxlen=result_history_limit)
        self._strategy_results: Deque[StrategyResult] = deque(maxlen=result_history_limit)
        self._processed = 0
        self._active_logical_time: Optional[int] = None
        self._pending_evaluations: Dict[str, _PendingEvaluation] = {}
        self._registered_evaluation_ids: set[str] = set()
        # The ledger is bounded and fail-closed.  We do not evict registrations
        # because once-only semantics must survive terminal tombstone eviction.
        self._evaluation_registration_limit = evaluation_registration_limit
        self._terminal_evaluations: OrderedDict[str, _TerminalEvaluation] = OrderedDict()
        self._terminal_evaluation_limit = terminal_evaluation_limit

    def submit(self, signal: EngineSignal) -> None:
        """Submit a signal; reducer execution remains single-threaded."""

        if not signal.signal_id:
            raise ValueError("signal_id is required")
        if (
            self._active_logical_time is not None
            and signal.logical_time_ms < self._active_logical_time
        ):
            raise ValueError("signal logical time moved backwards during drain")
        frozen_payload = deep_freeze(signal.payload)
        queued_signal = EngineSignal(
            signal_id=signal.signal_id,
            logical_time_ms=signal.logical_time_ms,
            signal_seq=signal.signal_seq,
            signal_kind=signal.signal_kind,
            payload=frozen_payload,
        )
        signature = canonical_hash(
            {
                "logical_time_ms": queued_signal.logical_time_ms,
                "signal_seq": queued_signal.signal_seq,
                "signal_kind": queued_signal.signal_kind,
                "payload": queued_signal.payload,
            }
        )
        previous = self._submitted_signatures.get(signal.signal_id)
        if previous is not None:
            if previous != signature:
                raise ValueError("signal_id was submitted with conflicting content")
            self._submitted_signatures.move_to_end(signal.signal_id)
            return
        self._submitted_signatures[signal.signal_id] = signature
        while len(self._submitted_signatures) > self._signal_id_cache_limit:
            self._submitted_signatures.popitem(last=False)
        heapq.heappush(self._queue, (queued_signal.sort_key, queued_signal))

    def run_until_empty(self) -> EngineRunResult:
        """Drain signals in deterministic time groups and causal generations."""

        try:
            while self._queue:
                _, first = heapq.heappop(self._queue)
                logical_time = first.logical_time_ms
                generation = [first]
                while self._queue and self._queue[0][1].logical_time_ms == logical_time:
                    _, same_time = heapq.heappop(self._queue)
                    generation.append(same_time)
                while generation:
                    self._active_logical_time = logical_time
                    for signal in sorted(generation, key=lambda item: item.sort_key):
                        self._processed += 1
                        self._handle(signal)
                    self._active_logical_time = None
                    generation = []
                    while self._queue and self._queue[0][1].logical_time_ms == logical_time:
                        _, child = heapq.heappop(self._queue)
                        generation.append(child)
        finally:
            self._active_logical_time = None
        return EngineRunResult(
            processed_signals=self._processed,
            snapshots=tuple(self._snapshots),
            strategy_results=tuple(self._strategy_results),
        )

    def _handle(self, signal: EngineSignal) -> None:
        if signal.signal_kind == SignalKind.MARKET_UPDATE:
            if self._is_before_market_frontier(signal):
                return
            projection = signal.payload
            self._reducer.apply_snapshot(
                projection,
                logical_time_ms=signal.logical_time_ms,
                session_id=self._session_id,
                phase=self._phase_at(signal.logical_time_ms),
            )
            self._windows.observe(
                logical_time_ms=signal.logical_time_ms,
                source_time_ms=projection.newest_source_time_ms,
                oldest_source_time_ms=projection.oldest_source_time_ms,
                newest_source_time_ms=projection.newest_source_time_ms,
                coverage=projection.coverage,
                completeness=projection.status.value,
                content_hash=projection.content_hash,
            )
            self._advance_market_frontier(signal.logical_time_ms)
            return

        if signal.signal_kind == SignalKind.DATA_READY:
            payload = signal.payload
            if not isinstance(payload, Mapping):
                raise ValueError("DATA_READY payload must be a mapping")
            if "evaluation_id" not in payload or "bundle" not in payload:
                raise ValueError("DATA_READY payload requires evaluation_id and bundle")
            if "snapshot" in payload:
                raise ValueError("DATA_READY must not provide a replacement snapshot")
            evaluation_id = payload["evaluation_id"]
            bundle = payload["bundle"]
            if not isinstance(evaluation_id, str) or not evaluation_id:
                raise ValueError("DATA_READY evaluation_id must be non-empty")
            if not isinstance(bundle, FrozenDataBundle):
                raise ValueError("DATA_READY bundle must be FrozenDataBundle")
            self._complete_evaluation(evaluation_id, bundle, signal.logical_time_ms)
            return

        if signal.signal_kind in (
            SignalKind.PULSE,
            SignalKind.TIMER,
            SignalKind.RECOVERY_CATCHUP,
        ):
            if self._is_before_market_frontier(signal):
                return
            # Resolve and validate the trigger phase before closing windows or
            # mutating any other Engine-owned state.  A cross-date SessionPlan
            # failure must be fail-closed, not fail-after-partial-application.
            trigger_phase = self._phase_at(signal.logical_time_ms)
            payload = signal.payload if isinstance(signal.payload, Mapping) else {}
            origin = (
                "RECOVERY_CATCHUP"
                if signal.signal_kind is SignalKind.RECOVERY_CATCHUP
                else "NORMAL"
            )
            trigger_id = payload.get("trigger_id", signal.signal_kind.value)
            node, function_order = self._resolve_evaluation(trigger_id, payload)
            for window_id in payload.get("close_windows", ()):
                self._windows.close(
                    window_id,
                    signal.logical_time_ms,
                    origin=origin,
                )
            snapshot = self._reducer.build_snapshot(
                trigger_id,
                logical_time_ms=signal.logical_time_ms,
                windows=self._windows,
                phase=trigger_phase,
            )
            evaluation_id = self._make_evaluation_id(signal, snapshot, node=node)
            self._register_evaluation(evaluation_id, snapshot, function_order)
            if not function_order:
                self._complete_evaluation(
                    evaluation_id,
                    FrozenDataBundle.empty(
                        evaluation_id=evaluation_id,
                        knowledge_as_of_ms=snapshot.logical_time_ms,
                    ),
                    signal.logical_time_ms,
                )
            self._advance_market_frontier(signal.logical_time_ms)
            return

        raise ValueError("unsupported signal kind: %s" % signal.signal_kind)

    def _is_before_market_frontier(self, signal: EngineSignal) -> bool:
        """Reject old market/timer signals without rejecting old DATA_READY."""

        return (
            self._market_frontier is not None
            and signal.logical_time_ms < self._market_frontier
        )

    def _advance_market_frontier(self, logical_time_ms: int) -> None:
        if self._market_frontier is None or logical_time_ms > self._market_frontier:
            self._market_frontier = logical_time_ms

    def _evaluate(
        self,
        snapshot: EngineSnapshot,
        bundle: FrozenDataBundle,
    ) -> None:
        self._snapshots.append(snapshot)
        result = self._strategy.evaluate(snapshot, bundle)
        self._strategy_results.append(result)

    def _make_evaluation_id(
        self,
        signal: EngineSignal,
        snapshot: EngineSnapshot,
        *,
        node: Optional[EvaluationNode] = None,
    ) -> str:
        """Create a deterministic id unique within this engine trace."""

        identity = {
            "trace_id": self._trace_id,
            "session_id": self._session_id,
            "phase": snapshot.phase,
            "signal_id": signal.signal_id,
            "signal_kind": signal.signal_kind,
            "logical_time_ms": signal.logical_time_ms,
            "signal_seq": signal.signal_seq,
            "snapshot_hash": snapshot.content_hash,
        }
        if node is not None:
            identity["evaluation_plan_hash"] = self._evaluation_plan.content_hash
            identity["evaluation_node_hash"] = node.content_hash
        digest = semantic_hash(identity)
        return "evaluation:" + digest

    def _phase_at(self, logical_time_ms: int) -> str:
        """Resolve one signal's phase from the configured single authority."""

        if self._session_plan is not None:
            return self._session_plan.phase_at_ms(logical_time_ms)
        return self._phase

    def _resolve_evaluation(
        self,
        trigger_id: str,
        payload: Mapping[str, Any],
    ) -> Tuple[Optional[EvaluationNode], Tuple[str, ...]]:
        """Resolve the one supported plan boundary before Engine mutation."""

        if not isinstance(trigger_id, str) or not trigger_id:
            raise ValueError("trigger_id must be a non-empty string")
        if self._evaluation_plan is not None:
            if "data_requirements" in payload:
                raise ValueError(
                    "data_requirements must come from the configured EvaluationPlan"
                )
            node = self._evaluation_plan.node_for_trigger(trigger_id)
            return node, node.data_requirements

        requirements = payload.get("data_requirements", ())
        if requirements is None:
            requirements = ()
        if not isinstance(requirements, (tuple, list)) or any(
            not isinstance(item, str) or not item for item in requirements
        ):
            raise ValueError("data_requirements must be a sequence of non-empty strings")
        function_order = tuple(requirements)
        if len(function_order) != len(set(function_order)):
            raise ValueError("data_requirements must not contain duplicates")
        return None, function_order

    def _register_evaluation(
        self,
        evaluation_id: str,
        snapshot: EngineSnapshot,
        function_order: Tuple[str, ...],
    ) -> None:
        if evaluation_id in self._registered_evaluation_ids:
            raise ValueError("evaluation_id was already registered")
        if len(self._registered_evaluation_ids) >= self._evaluation_registration_limit:
            raise ValueError("evaluation registration capacity exhausted")
        self._registered_evaluation_ids.add(evaluation_id)
        self._pending_evaluations[evaluation_id] = _PendingEvaluation(
            evaluation_id=evaluation_id,
            snapshot=snapshot,
            function_order=tuple(function_order),
        )

    def _complete_evaluation(
        self,
        evaluation_id: str,
        bundle: FrozenDataBundle,
        completed_logical_time_ms: int,
    ) -> None:
        pending = self._pending_evaluations.get(evaluation_id)
        if pending is None:
            terminal = self._terminal_evaluations.get(evaluation_id)
            if terminal is not None:
                if terminal.submission_hash == bundle.submission_hash:
                    raise ValueError("duplicate evaluation completion")
                raise ValueError("conflicting evaluation completion")
            if evaluation_id in self._registered_evaluation_ids:
                raise ValueError("unknown terminal evaluation")
            raise ValueError("unknown evaluation_id")
        self._validate_bundle(pending, bundle)
        self._pending_evaluations.pop(evaluation_id)
        terminal = _TerminalEvaluation(
            evaluation_id=evaluation_id,
            submission_hash=bundle.submission_hash,
            completed_logical_time_ms=completed_logical_time_ms,
        )
        self._terminal_evaluations[evaluation_id] = terminal
        self._terminal_evaluations.move_to_end(evaluation_id)
        while len(self._terminal_evaluations) > self._terminal_evaluation_limit:
            self._terminal_evaluations.popitem(last=False)
        self._evaluate(pending.snapshot, bundle)

    @staticmethod
    def _validate_bundle(
        pending: _PendingEvaluation,
        bundle: FrozenDataBundle,
    ) -> None:
        if bundle.evaluation_id != pending.evaluation_id:
            raise ValueError("bundle evaluation_id does not match registered evaluation")
        if bundle.knowledge_as_of_ms != pending.snapshot.logical_time_ms:
            raise ValueError("bundle knowledge cutoff does not match frozen snapshot")
        if tuple(bundle.function_order) != tuple(pending.function_order):
            raise ValueError("bundle function order does not match evaluation")
        for function_id in bundle.function_order:
            result = bundle.results_by_function[function_id]
            if result.status in (DataStatus.READY, DataStatus.PARTIAL):
                if result.available_at_ms is None:
                    raise ValueError("runtime data has unknown available_at")
                if result.available_at_ms > bundle.knowledge_as_of_ms:
                    raise ValueError("runtime data is after knowledge cutoff")
