"""Small half-open window primitives used by the vertical slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Dict, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from .contracts import WindowView, canonical_hash, deep_freeze


SHANGHAI = ZoneInfo("Asia/Shanghai")
WindowSnapshot = WindowView


def local_time_ms(trade_date: str, hhmmss: str, tz: ZoneInfo = SHANGHAI) -> int:
    """Convert YYYY-MM-DD and HH:MM:SS to UTC epoch milliseconds."""

    day = date.fromisoformat(trade_date)
    parts = hhmmss.split(":")
    if len(parts) != 3:
        raise ValueError("hhmmss must be HH:MM:SS")
    value = datetime.combine(
        day,
        time(int(parts[0]), int(parts[1]), int(parts[2])),
        tzinfo=tz,
    )
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


@dataclass(frozen=True)
class WindowSpec:
    window_id: str
    start_ms: int
    end_exclusive_ms: int

    def __post_init__(self) -> None:
        if not self.window_id:
            raise ValueError("window_id is required")
        if self.start_ms >= self.end_exclusive_ms:
            raise ValueError("window must be non-empty and half-open")

    def contains(self, logical_time_ms: int) -> bool:
        return self.start_ms <= logical_time_ms < self.end_exclusive_ms


@dataclass
class _WindowAccumulator:
    spec: WindowSpec
    observation_count: int = 0
    first_source_time_ms: Optional[int] = None
    last_source_time_ms: Optional[int] = None
    min_coverage: float = 0.0
    min_completeness: str = "MISSING"
    last_content_hash: str = ""
    revision: int = 0
    closed: bool = False
    origin: str = "NORMAL"


class WindowManager:
    """Accumulate raw observation metadata without strategy interpretation."""

    def __init__(self, specs: Tuple[WindowSpec, ...]) -> None:
        if not specs:
            raise ValueError("at least one WindowSpec is required")
        ids = [spec.window_id for spec in specs]
        if len(ids) != len(set(ids)):
            raise ValueError("window_id values must be unique")
        self._windows: Dict[str, _WindowAccumulator] = {
            spec.window_id: _WindowAccumulator(spec=spec) for spec in specs
        }

    def observe(
        self,
        logical_time_ms: int,
        source_time_ms: Optional[int],
        coverage: float,
        completeness: str,
        content_hash: str,
    ) -> Tuple[str, ...]:
        """Record one raw observation in every containing window."""

        if not 0.0 <= coverage <= 1.0:
            raise ValueError("coverage must be between 0 and 1")
        touched = []
        for accumulator in self._windows.values():
            if accumulator.closed or not accumulator.spec.contains(logical_time_ms):
                continue
            accumulator.observation_count += 1
            accumulator.min_coverage = (
                coverage
                if accumulator.observation_count == 1
                else min(accumulator.min_coverage, coverage)
            )
            accumulator.min_completeness = (
                completeness
                if accumulator.observation_count == 1
                else _worst_status(accumulator.min_completeness, completeness)
            )
            accumulator.last_content_hash = content_hash
            accumulator.revision += 1
            if source_time_ms is not None:
                if accumulator.first_source_time_ms is None:
                    accumulator.first_source_time_ms = source_time_ms
                accumulator.last_source_time_ms = source_time_ms
            touched.append(accumulator.spec.window_id)
        return tuple(sorted(touched))

    def close(
        self,
        window_id: str,
        at_ms: int,
        *,
        origin: str = "NORMAL",
    ) -> WindowView:
        """Close a window and return its immutable raw fact view."""

        if origin not in {"NORMAL", "RECOVERY_CATCHUP"}:
            raise ValueError("origin must be NORMAL or RECOVERY_CATCHUP")
        accumulator = self._windows[window_id]
        if at_ms < accumulator.spec.end_exclusive_ms:
            raise ValueError("window cannot close before end_exclusive_ms")
        if not accumulator.closed:
            accumulator.closed = True
            accumulator.origin = origin
            accumulator.revision += 1
        return self._view(accumulator)

    def views(self) -> Mapping[str, WindowView]:
        """Return immutable-by-convention views for all configured windows."""

        return deep_freeze({
            key: self._view(value)
            for key, value in sorted(self._windows.items())
        })

    def _view(self, accumulator: _WindowAccumulator) -> WindowView:
        payload = {
            "window_id": accumulator.spec.window_id,
            "revision": accumulator.revision,
            "start_ms": accumulator.spec.start_ms,
            "end_exclusive_ms": accumulator.spec.end_exclusive_ms,
            "observation_count": accumulator.observation_count,
            "first_source_time_ms": accumulator.first_source_time_ms,
            "last_source_time_ms": accumulator.last_source_time_ms,
            "coverage": accumulator.min_coverage,
            "completeness": accumulator.min_completeness,
            "last_content_hash": accumulator.last_content_hash,
            "closed": accumulator.closed,
            "finality": "FINAL" if accumulator.closed else "OPEN",
            "origin": accumulator.origin,
        }
        return WindowView(
            window_id=accumulator.spec.window_id,
            revision=accumulator.revision,
            start_ms=accumulator.spec.start_ms,
            end_exclusive_ms=accumulator.spec.end_exclusive_ms,
            observation_count=accumulator.observation_count,
            first_source_time_ms=accumulator.first_source_time_ms,
            last_source_time_ms=accumulator.last_source_time_ms,
            coverage=accumulator.min_coverage,
            completeness=accumulator.min_completeness,
            content_hash=canonical_hash(payload),
            finality="FINAL" if accumulator.closed else "OPEN",
            origin=accumulator.origin,
        )


def _worst_status(left: str, right: str) -> str:
    rank = {
        "READY": 0,
        "PRELIMINARY": 1,
        "PARTIAL": 2,
        "STALE": 3,
        "MISSING": 4,
        "INVALID": 5,
    }
    return right if rank.get(right, 99) > rank.get(left, 99) else left
