"""Minimal Q2FrameV1 replay source for the existing Engine queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any, Iterable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from .clock import VirtualClock
from .contracts import EngineSignal, SignalKind, deep_freeze, semantic_hash
from .q2 import (
    FreshnessPolicy,
    Q2ProjectionSnapshot,
    build_q2_projection,
    normalize_symbol,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


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
    logical time before normalization.  Signal construction is side-effect free
    for the shared clock; replay helpers advance the clock immediately before
    the Engine consumes a signal.
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

    def _apply_frame(
        self,
        raw_frame: Mapping[str, Any] | Q2FrameV1,
        *,
        advance_clock: bool,
    ) -> Q2ProjectionSnapshot:
        frame = raw_frame if isinstance(raw_frame, Q2FrameV1) else Q2FrameV1.from_mapping(raw_frame)
        if frame.seq_no != self._last_seq_no + 1:
            raise ValueError("Q2Frame seq_no is not continuous")
        if frame.logical_ts_ms < self._last_logical_ts_ms:
            raise ValueError("Q2Frame logical_ts_ms moved backwards")
        target = datetime.fromtimestamp(frame.logical_ts_ms / 1000.0, tz=timezone.utc)
        if advance_clock:
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
        observed_at = self._clock.now_utc() if advance_clock else target
        return build_q2_projection(
            self._trade_date,
            observed_at,
            self._expected_symbols,
            self._raw_hashes,
            freshness_policy=self._freshness_policy,
            source_id=self._source_id,
        )

    def apply(self, raw_frame: Mapping[str, Any] | Q2FrameV1) -> Q2ProjectionSnapshot:
        """Apply a frame and advance the clock for direct wheel tests."""

        return self._apply_frame(raw_frame, advance_clock=True)

    def signal_for(
        self,
        raw_frame: Mapping[str, Any] | Q2FrameV1,
        *,
        signal_prefix: str = "q2frame",
    ) -> EngineSignal:
        frame = raw_frame if isinstance(raw_frame, Q2FrameV1) else Q2FrameV1.from_mapping(raw_frame)
        projection = self._apply_frame(frame, advance_clock=False)
        return EngineSignal(
            signal_id=f"{signal_prefix}:{frame.seq_no}",
            logical_time_ms=frame.logical_ts_ms,
            signal_seq=frame.seq_no,
            signal_kind=SignalKind.MARKET_UPDATE,
            payload=projection,
        )

    def advance_before_consume(self, signal: EngineSignal) -> None:
        """Advance virtual time at the Engine-consumption boundary."""

        target = datetime.fromtimestamp(signal.logical_time_ms / 1000.0, tz=timezone.utc)
        self._clock.advance_to(target)


def replay_q2frames(
    frames: Iterable[Mapping[str, Any] | Q2FrameV1],
    source: Q2FrameReplaySource,
    engine: Any,
    *,
    signal_prefix: str = "q2frame",
) -> None:
    """Submit frame-derived updates to an existing Engine, in frame order."""

    signals = [source.signal_for(frame, signal_prefix=signal_prefix) for frame in frames]
    index = 0
    while index < len(signals):
        logical_time = signals[index].logical_time_ms
        source.advance_before_consume(signals[index])
        while index < len(signals) and signals[index].logical_time_ms == logical_time:
            engine.submit(signals[index])
            index += 1
        engine.run_until_empty()


def _td_timestamp_ms(value: Any, *, source_timezone: tzinfo) -> int:
    """Normalize a TD timestamp without inferring a local zone silently."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, int) and not isinstance(value, bool):
        if 946_684_800_000 <= value <= 4_102_444_800_000:
            return value
        if 946_684_800 <= value <= 4_102_444_800:
            return value * 1000
        raise ValueError("TD tick timestamp is outside supported epoch range")
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("TD tick timestamp must be ISO datetime") from exc
    else:
        raise ValueError("TD tick timestamp must be datetime, epoch integer or ISO text")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=source_timezone)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _td_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("TD tick %s must be an integer" % field)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("TD tick %s must be an integer" % field)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("TD tick %s must be an integer" % field) from exc


@dataclass(frozen=True)
class TDEventV1:
    """One normalized ``stock_tick_v2`` row for event-time replay."""

    event_time_ms: int
    symbol: str
    price_milli: int
    pre_close_milli: int
    amount_yuan: int
    volume_units: Optional[int]
    raw_fields: Mapping[str, Any]
    content_hash: str

    def __post_init__(self) -> None:
        if self.event_time_ms <= 0:
            raise ValueError("event_time_ms must be positive")
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "raw_fields", deep_freeze(self.raw_fields))

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        source_timezone: tzinfo = SHANGHAI,
    ) -> "TDEventV1":
        if not isinstance(raw, Mapping):
            raise ValueError("TD tick must be an object")
        symbol = normalize_symbol(raw.get("symbol", ""))
        event_time_ms = _td_timestamp_ms(raw.get("ts"), source_timezone=source_timezone)
        price_milli = _td_int(raw.get("px_milli"), field="px_milli")
        pre_close_milli = _td_int(raw.get("pc_milli"), field="pc_milli")
        amount_yuan = _td_int(raw.get("amt_yuan"), field="amt_yuan")
        volume_value = raw.get("vol_units")
        volume_units = (
            None
            if volume_value is None
            else _td_int(volume_value, field="vol_units")
        )
        normalized_raw = dict(raw)
        normalized_raw.update(
            {
                "symbol": symbol,
                "ts": event_time_ms,
                "px_milli": price_milli,
                "pc_milli": pre_close_milli,
                "amt_yuan": amount_yuan,
            }
        )
        if volume_units is not None:
            normalized_raw["vol_units"] = volume_units
        digest = semantic_hash(
            {
                "event_time_ms": event_time_ms,
                "symbol": symbol,
                "price_milli": price_milli,
                "pre_close_milli": pre_close_milli,
                "amount_yuan": amount_yuan,
                "volume_units": volume_units,
                "raw_fields": normalized_raw,
            }
        )
        return cls(
            event_time_ms=event_time_ms,
            symbol=symbol,
            price_milli=price_milli,
            pre_close_milli=pre_close_milli,
            amount_yuan=amount_yuan,
            volume_units=volume_units,
            raw_fields=normalized_raw,
            content_hash=digest,
        )

    def to_q2_raw(self) -> Mapping[str, Any]:
        """Map only verified shared fields; ``vol_units`` is not relabeled."""

        return {
            "px": self.price_milli,
            "pc": self.pre_close_milli,
            "amt": self.amount_yuan,
            "ts": self.event_time_ms,
        }


@dataclass(frozen=True)
class TDEventSlice:
    """A three-second transport batch that preserves every normalized tick."""

    slice_no: int
    start_ms: int
    end_exclusive_ms: int
    events: Tuple[TDEventV1, ...]
    content_hash: str

    def __post_init__(self) -> None:
        if self.slice_no < 0:
            raise ValueError("slice_no must be non-negative")
        if self.start_ms >= self.end_exclusive_ms:
            raise ValueError("slice must use a positive half-open interval")
        object.__setattr__(self, "events", tuple(self.events))
        if any(
            event.event_time_ms < self.start_ms
            or event.event_time_ms >= self.end_exclusive_ms
            for event in self.events
        ):
            raise ValueError("event is outside its slice interval")


class TDEventTimeReplaySource:
    """Deterministic TD event-time source with explicit three-second slices.

    TD rows are ordered by event time, symbol and canonical event content.  No
    Rabbit arrival order, batch boundary or watermark is inferred.  Slices
    retain each event; the source emits one Engine MARKET_UPDATE signal per
    event so no OHLC/feature pre-aggregation occurs before the reducer.
    """

    def __init__(
        self,
        trade_date: str,
        expected_symbols: Iterable[Any],
        clock: VirtualClock,
        *,
        slice_anchor_ms: int,
        slice_ms: int = 3_000,
        source_timezone: tzinfo = SHANGHAI,
        freshness_policy: FreshnessPolicy = FreshnessPolicy(),
        source_id: str = "td_event_time_replay",
    ) -> None:
        if not trade_date:
            raise ValueError("trade_date is required")
        if slice_anchor_ms <= 0:
            raise ValueError("slice_anchor_ms must be positive")
        if slice_ms <= 0:
            raise ValueError("slice_ms must be positive")
        self._trade_date = trade_date
        self._expected_symbols = tuple(
            sorted({normalize_symbol(value) for value in expected_symbols})
        )
        self._expected_symbol_set = set(self._expected_symbols)
        self._clock = clock
        self._slice_anchor_ms = slice_anchor_ms
        self._slice_ms = slice_ms
        self._source_timezone = source_timezone
        self._freshness_policy = freshness_policy
        self._source_id = source_id

    @property
    def slice_ms(self) -> int:
        return self._slice_ms

    def event_slices(
        self,
        rows: Iterable[Mapping[str, Any] | TDEventV1],
    ) -> Tuple[TDEventSlice, ...]:
        events = [
            row
            if isinstance(row, TDEventV1)
            else TDEventV1.from_mapping(
                row,
                source_timezone=self._source_timezone,
            )
            for row in rows
        ]
        events.sort(
            key=lambda event: (
                event.event_time_ms,
                event.symbol,
                event.content_hash,
            )
        )
        grouped: dict[int, list[TDEventV1]] = {}
        for event in events:
            if (
                self._expected_symbol_set
                and event.symbol not in self._expected_symbol_set
            ):
                raise ValueError("TD tick symbol is outside expected_symbols")
            event_date = datetime.fromtimestamp(
                event.event_time_ms / 1000.0,
                tz=timezone.utc,
            ).astimezone(self._source_timezone).date().isoformat()
            if event_date != self._trade_date:
                raise ValueError("TD tick event date does not match trade_date")
            if event.event_time_ms < self._slice_anchor_ms:
                raise ValueError("TD tick precedes slice_anchor_ms")
            slice_no = (event.event_time_ms - self._slice_anchor_ms) // self._slice_ms
            grouped.setdefault(slice_no, []).append(event)
        slices = []
        for slice_no in sorted(grouped):
            start_ms = self._slice_anchor_ms + slice_no * self._slice_ms
            end_ms = start_ms + self._slice_ms
            slice_events = tuple(grouped[slice_no])
            slices.append(
                TDEventSlice(
                    slice_no=slice_no,
                    start_ms=start_ms,
                    end_exclusive_ms=end_ms,
                    events=slice_events,
                    content_hash=semantic_hash(
                        {
                            "slice_no": slice_no,
                            "start_ms": start_ms,
                            "end_exclusive_ms": end_ms,
                            "events": [event.content_hash for event in slice_events],
                        }
                    ),
                )
            )
        return tuple(slices)

    def signals_for(
        self,
        rows: Iterable[Mapping[str, Any] | TDEventV1],
        *,
        signal_prefix: str = "td-event",
    ) -> Tuple[EngineSignal, ...]:
        latest_raw: dict[str, Mapping[str, Any]] = {}
        signals = []
        signal_seq = 0
        for event_slice in self.event_slices(rows):
            for event_index, event in enumerate(event_slice.events):
                latest_raw[event.symbol] = event.to_q2_raw()
                projection = build_q2_projection(
                    self._trade_date,
                    datetime.fromtimestamp(
                        event.event_time_ms / 1000.0,
                        tz=timezone.utc,
                    ),
                    self._expected_symbols or tuple(sorted(latest_raw)),
                    latest_raw,
                    freshness_policy=self._freshness_policy,
                    source_id=self._source_id,
                )
                signal_seq += 1
                signals.append(
                    EngineSignal(
                        signal_id=(
                            f"{signal_prefix}:slice:{event_slice.slice_no}:"
                            f"event:{event_index}:{event.content_hash[:16]}"
                        ),
                        logical_time_ms=event.event_time_ms,
                        signal_seq=signal_seq,
                        signal_kind=SignalKind.MARKET_UPDATE,
                        payload=projection,
                    )
                )
        return tuple(signals)

    def advance_before_consume(self, signal: EngineSignal) -> None:
        """Advance virtual time at the Engine-consumption boundary."""

        target = datetime.fromtimestamp(signal.logical_time_ms / 1000.0, tz=timezone.utc)
        self._clock.advance_to(target)


def replay_td_event_time(
    rows: Iterable[Mapping[str, Any] | TDEventV1],
    source: TDEventTimeReplaySource,
    engine: Any,
    *,
    signal_prefix: str = "td-event",
) -> None:
    """Submit TD event-time signals to the existing Engine queue."""

    signals = source.signals_for(rows, signal_prefix=signal_prefix)
    index = 0
    while index < len(signals):
        logical_time = signals[index].logical_time_ms
        source.advance_before_consume(signals[index])
        while index < len(signals) and signals[index].logical_time_ms == logical_time:
            engine.submit(signals[index])
            index += 1
        engine.run_until_empty()
