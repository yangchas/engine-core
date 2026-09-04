"""Minimal Q2FrameV1 replay source for the existing Engine queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Tuple

from .clock import VirtualClock
from .contracts import EngineSignal, SignalKind, deep_freeze
from .q2 import (
    FreshnessPolicy,
    Q2ProjectionSnapshot,
    build_q2_projection,
    normalize_symbol,
)


@dataclass(frozen=True)
class Q2FrameV1:
    """Validated in-memory representation of one existing Q2FrameV1 row."""

    seq_no: int
    logical_ts_ms: int
    q2_updates: Tuple[Mapping[str, Any], ...]
    phase: Any = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Q2FrameV1":
        if not isinstance(raw, Mapping):
            raise ValueError("Q2Frame must be an object")
        if raw.get("version") != "Q2FrameV1":
            raise ValueError("unsupported Q2Frame version")
        try:
            seq_no = int(raw["seq_no"])
            logical_ts_ms = int(raw["logical_ts_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Q2Frame seq_no/logical_ts_ms must be integers") from exc
        if seq_no <= 0:
            raise ValueError("Q2Frame seq_no must be positive")
        if logical_ts_ms <= 0:
            raise ValueError("Q2Frame logical_ts_ms must be positive")
        updates = raw.get("q2_updates", [])
        if not isinstance(updates, list):
            raise ValueError("Q2Frame q2_updates must be a list")
        normalized = []
        for update in updates:
            if not isinstance(update, Mapping):
                raise ValueError("Q2Frame update must be an object")
            symbol = normalize_symbol(update.get("symbol", ""))
            values = {
                str(key): value
                for key, value in update.items()
                if key != "symbol"
            }
            values["symbol"] = symbol
            normalized.append(deep_freeze(values))
        return cls(
            seq_no=seq_no,
            logical_ts_ms=logical_ts_ms,
            q2_updates=tuple(normalized),
            phase=raw.get("phase"),
        )


class Q2FrameReplaySource:
    """Turn Q2FrameV1 rows into immutable Q2 projections.

    The source keeps only the latest raw hash per symbol, matching the
    existing Q2 projection shape. It has no Redis/TD/network access and does
    not infer Rabbit arrival order. ``VirtualClock`` advances to each frame's
    logical time before normalization.
    """

    def __init__(
        self,
        trade_date: str,
        expected_symbols: Iterable[Any],
        clock: VirtualClock,
        *,
        freshness_policy: FreshnessPolicy = FreshnessPolicy(),
        source_id: str = "q2frame_replay",
    ) -> None:
        self._trade_date = trade_date
        self._expected_symbols = tuple(
            sorted({normalize_symbol(value) for value in expected_symbols})
        )
        if not self._expected_symbols:
            raise ValueError("expected_symbols must not be empty")
        self._clock = clock
        self._freshness_policy = freshness_policy
        self._source_id = source_id
        self._raw_hashes: dict[str, dict[str, Any]] = {}
        self._last_seq_no = 0
        self._last_logical_ts_ms = 0

    @property
    def last_seq_no(self) -> int:
        return self._last_seq_no

    @property
    def last_logical_ts_ms(self) -> int:
        return self._last_logical_ts_ms

    def apply(self, raw_frame: Mapping[str, Any] | Q2FrameV1) -> Q2ProjectionSnapshot:
        frame = raw_frame if isinstance(raw_frame, Q2FrameV1) else Q2FrameV1.from_mapping(raw_frame)
        if frame.seq_no != self._last_seq_no + 1:
            raise ValueError("Q2Frame seq_no is not continuous")
        if frame.logical_ts_ms < self._last_logical_ts_ms:
            raise ValueError("Q2Frame logical_ts_ms moved backwards")
        target = datetime.fromtimestamp(frame.logical_ts_ms / 1000.0, tz=timezone.utc)
        self._clock.advance_to(target)
        for update in frame.q2_updates:
            symbol = str(update["symbol"])
            values = {
                key: value
                for key, value in update.items()
                if key != "symbol"
            }
            self._raw_hashes.setdefault(symbol, {}).update(values)
        self._last_seq_no = frame.seq_no
        self._last_logical_ts_ms = frame.logical_ts_ms
        return build_q2_projection(
            self._trade_date,
            self._clock.now_utc(),
            self._expected_symbols,
            self._raw_hashes,
            freshness_policy=self._freshness_policy,
            source_id=self._source_id,
        )

    def signal_for(
        self,
        raw_frame: Mapping[str, Any] | Q2FrameV1,
        *,
        signal_prefix: str = "q2frame",
    ) -> EngineSignal:
        frame = raw_frame if isinstance(raw_frame, Q2FrameV1) else Q2FrameV1.from_mapping(raw_frame)
        projection = self.apply(frame)
        return EngineSignal(
            signal_id=f"{signal_prefix}:{frame.seq_no}",
            logical_time_ms=frame.logical_ts_ms,
            signal_seq=frame.seq_no,
            signal_kind=SignalKind.MARKET_UPDATE,
            payload=projection,
        )


def replay_q2frames(
    frames: Iterable[Mapping[str, Any] | Q2FrameV1],
    source: Q2FrameReplaySource,
    engine: Any,
    *,
    signal_prefix: str = "q2frame",
) -> None:
    """Submit frame-derived updates to an existing Engine, in frame order."""

    for frame in frames:
        engine.submit(source.signal_for(frame, signal_prefix=signal_prefix))
