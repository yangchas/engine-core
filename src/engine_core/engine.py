"""Small deterministic signal queue and reducer orchestration."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol, Tuple

from .contracts import (
    EngineSignal,
    EngineSnapshot,
    FrozenDataBundle,
    SignalKind,
    StrategyResult,
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
        self._snapshots: List[EngineSnapshot] = []
        self._strategy_results: List[StrategyResult] = []
        self._processed = 0

    def submit(self, signal: EngineSignal) -> None:
        """Submit a signal; reducer execution remains single-threaded."""

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
            return

        if signal.signal_kind == SignalKind.DATA_READY:
            payload = signal.payload
            snapshot = payload["snapshot"]
            bundle = payload["bundle"]
            self._evaluate(signal, snapshot, bundle)
            return

        if signal.signal_kind in (
            SignalKind.PULSE,
            SignalKind.TIMER,
            SignalKind.RECOVERY_CATCHUP,
        ):
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
            return

        raise ValueError("unsupported signal kind: %s" % signal.signal_kind)

    def _evaluate(
        self,
        signal: EngineSignal,
        snapshot: EngineSnapshot,
        bundle: FrozenDataBundle,
    ) -> None:
        self._snapshots.append(snapshot)
        result = self._strategy.evaluate(snapshot, bundle)
        self._strategy_results.append(result)
