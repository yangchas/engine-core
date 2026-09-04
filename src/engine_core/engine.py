"""Small deterministic signal queue and reducer orchestration."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

from .contracts import (
    EngineSignal,
    EngineSnapshot,
    FrozenDataBundle,
    SignalKind,
    StrategyResult,
    canonical_hash,
)
from .state import MarketStateReducer
from .windows import WindowManager


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


class DeterministicEngine:
    """Single-threaded reducer that is unaware of the input mode."""

    def __init__(
        self,
        reducer: MarketStateReducer,
        windows: WindowManager,
        strategy: Strategy,
        *,
        session_id: str = "",
        phase: str = "UNKNOWN",
    ) -> None:
        self._reducer = reducer
        self._windows = windows
        self._strategy = strategy
        self._session_id = session_id
        self._phase = phase
        self._queue: List[Tuple[Tuple[int, int, int, str], EngineSignal]] = []
        self._submitted_signatures: dict[str, str] = {}
        self._market_frontier: Optional[int] = None
        self._snapshots: List[EngineSnapshot] = []
        self._strategy_results: List[StrategyResult] = []
        self._processed = 0

    def submit(self, signal: EngineSignal) -> None:
        """Submit a signal; reducer execution remains single-threaded."""

        if not signal.signal_id:
            raise ValueError("signal_id is required")
        signature = canonical_hash(
            {
                "logical_time_ms": signal.logical_time_ms,
                "signal_seq": signal.signal_seq,
                "signal_kind": signal.signal_kind,
                "payload": signal.payload,
            }
        )
        previous = self._submitted_signatures.get(signal.signal_id)
        if previous is not None:
            if previous != signature:
                raise ValueError("signal_id was submitted with conflicting content")
            return
        self._submitted_signatures[signal.signal_id] = signature
        heapq.heappush(self._queue, (signal.sort_key, signal))

    def run_until_empty(self) -> EngineRunResult:
        """Drain signals in deterministic order."""

        while self._queue:
            _, signal = heapq.heappop(self._queue)
            self._processed += 1
            self._handle(signal)
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
                phase=self._phase,
            )
            self._windows.observe(
                logical_time_ms=signal.logical_time_ms,
                source_time_ms=projection.newest_source_time_ms,
                coverage=projection.coverage,
                completeness=projection.status.value,
                content_hash=projection.content_hash,
            )
            self._advance_market_frontier(signal.logical_time_ms)
            return

        if signal.signal_kind == SignalKind.DATA_READY:
            payload = signal.payload
            if not isinstance(payload, dict):
                raise ValueError("DATA_READY payload must be a mapping")
            if "snapshot" not in payload or "bundle" not in payload:
                raise ValueError("DATA_READY payload requires snapshot and bundle")
            snapshot = payload["snapshot"]
            bundle = payload["bundle"]
            self._evaluate(signal, snapshot, bundle)
            return

        if signal.signal_kind in (
            SignalKind.PULSE,
            SignalKind.TIMER,
            SignalKind.RECOVERY_CATCHUP,
        ):
            if self._is_before_market_frontier(signal):
                return
            payload = signal.payload if isinstance(signal.payload, dict) else {}
            for window_id in payload.get("close_windows", ()):
                self._windows.close(window_id, signal.logical_time_ms)
            trigger_id = payload.get("trigger_id", signal.signal_kind.value)
            snapshot = self._reducer.build_snapshot(
                trigger_id,
                logical_time_ms=signal.logical_time_ms,
                windows=self._windows,
            )
            self._evaluate(signal, snapshot, FrozenDataBundle.empty(
                evaluation_id=signal.signal_id,
                knowledge_as_of_ms=signal.logical_time_ms,
            ))
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
        signal: EngineSignal,
        snapshot: EngineSnapshot,
        bundle: FrozenDataBundle,
    ) -> None:
        self._snapshots.append(snapshot)
        result = self._strategy.evaluate(snapshot, bundle)
        self._strategy_results.append(result)
