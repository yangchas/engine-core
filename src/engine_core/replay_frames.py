"""Deterministic full-market cross-sectional replay contracts.

This module is deliberately an in-memory boundary.  It accepts already read
rows and never opens a Redis, TDengine or RabbitMQ connection.  A replay is a
single trading-day timeline made of fixed, half-open frames; an empty frame is
still a fact in that timeline.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .contracts import DataStatus, EngineSignal, SignalKind, deep_freeze, evidence_hash, semantic_hash
from .q2 import FreshnessPolicy, Q2ProjectionSnapshot, build_q2_projection, normalize_symbol
from .replay import SHANGHAI, TDEventV1
from .clock import VirtualClock


MARKET_FRAME_CONTRACT_VERSION = "MarketFrameV1"
FRAME_MANIFEST_CONTRACT_VERSION = "FrameManifestV1"
CROSS_SECTION_STATE_CONTRACT_VERSION = "CrossSectionStateV1"
CROSS_SECTION_PROJECTION_CONTRACT_VERSION = "CrossSectionProjectionV1"
CROSS_SECTION_FACTS_CONTRACT_VERSION = "CrossSectionFactsV1"

SOURCE_SEQUENCE_UNKNOWN = "UNKNOWN"
RABBIT_ARRIVAL_UNKNOWN = "UNKNOWN"
HISTORICAL_AVAILABLE_AT_UNKNOWN = "UNKNOWN"

FRAME_COMPLETE = "COMPLETE"
FRAME_PARTIAL = "PARTIAL"
FRAME_EMPTY = "EMPTY"


class ReplayVerificationLevel(str, Enum):
    """Cost/fidelity level for bounded replay evidence.

    FULL retains the historical per-frame cumulative state hash.  FRAME keeps
    frame/input/signal evidence and uses an incremental state identity.  FINAL
    verifies the cumulative state only at the final snapshot.  NONE is only
    appropriate for profiling and must never be presented as determinism
    evidence.
    """

    FULL = "FULL"
    FRAME = "FRAME"
    FINAL = "FINAL"
    NONE = "NONE"


def _strict_trade_date(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("trade_date must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("trade_date must be YYYY-MM-DD") from exc
    if parsed.date().isoformat() != value:
        # ``fromisoformat`` accepts a datetime; reject it explicitly.
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    return value


def _event_local_date(event: TDEventV1, source_timezone: tzinfo) -> str:
    return datetime.fromtimestamp(event.event_time_ms / 1000.0, tz=timezone.utc).astimezone(source_timezone).date().isoformat()


@dataclass(frozen=True)
class MarketFrameV1:
    """One complete 3-second market cross-section interval."""

    trade_date: str
    frame_no: int
    start_ms: int
    end_exclusive_ms: int
    expected_symbols: Tuple[str, ...]
    updated_symbols: Tuple[str, ...]
    missing_symbols: Tuple[str, ...]
    events: Tuple[TDEventV1, ...]
    completeness: str
    source_time_min_ms: Optional[int]
    source_time_max_ms: Optional[int]
    source_sequence_status: str = SOURCE_SEQUENCE_UNKNOWN
    rabbit_arrival_order: str = RABBIT_ARRIVAL_UNKNOWN
    historical_available_at: str = HISTORICAL_AVAILABLE_AT_UNKNOWN
    logical_ts_ms: int = field(init=False)
    coverage: float = field(init=False)
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _strict_trade_date(self.trade_date)
        if isinstance(self.frame_no, bool) or self.frame_no < 0:
            raise ValueError("frame_no must be non-negative")
        if self.start_ms <= 0 or self.end_exclusive_ms <= self.start_ms:
            raise ValueError("frame interval must be positive")
        expected = tuple(sorted({normalize_symbol(symbol) for symbol in self.expected_symbols}))
        updated = tuple(sorted({normalize_symbol(symbol) for symbol in self.updated_symbols}))
        missing = tuple(sorted({normalize_symbol(symbol) for symbol in self.missing_symbols}))
        if set(updated) - set(expected) or set(missing) - set(expected):
            raise ValueError("updated/missing symbols must be in expected_symbols")
        if set(updated) & set(missing) or set(updated) | set(missing) != set(expected):
            raise ValueError("updated and missing symbols must partition expected_symbols")
        if self.completeness not in {FRAME_COMPLETE, FRAME_PARTIAL, FRAME_EMPTY}:
            raise ValueError("unsupported frame completeness")
        events = tuple(self.events)
        if any(not isinstance(event, TDEventV1) for event in events):
            raise TypeError("events must contain TDEventV1")
        if any(event.event_time_ms < self.start_ms or event.event_time_ms >= self.end_exclusive_ms for event in events):
            raise ValueError("event is outside the half-open frame")
        event_symbols = {event.symbol for event in events}
        if event_symbols - set(updated):
            raise ValueError("updated_symbols must include every event symbol")
        object.__setattr__(self, "expected_symbols", expected)
        object.__setattr__(self, "updated_symbols", updated)
        object.__setattr__(self, "missing_symbols", missing)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "logical_ts_ms", self.end_exclusive_ms)
        coverage = len(updated) / float(len(expected)) if expected else 0.0
        object.__setattr__(self, "coverage", coverage)
        semantic = {
            "contract": MARKET_FRAME_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "frame_no": self.frame_no,
            "start_ms": self.start_ms,
            "end_exclusive_ms": self.end_exclusive_ms,
            "expected_symbols": expected,
            "updated_symbols": updated,
            "missing_symbols": missing,
            "events": tuple(event.content_hash for event in events),
            "completeness": self.completeness,
        }
        object.__setattr__(self, "content_hash", semantic_hash(semantic))
        object.__setattr__(
            self,
            "evidence_hash",
            evidence_hash(
                {
                    "source_time_min_ms": self.source_time_min_ms,
                    "source_time_max_ms": self.source_time_max_ms,
                    "source_sequence_status": self.source_sequence_status,
                    "rabbit_arrival_order": self.rabbit_arrival_order,
                    "historical_available_at": self.historical_available_at,
                }
            ),
        )

    @property
    def start_inclusive_ms(self) -> int:
        return self.start_ms

    @property
    def expected_count(self) -> int:
        return len(self.expected_symbols)

    @property
    def observed_count(self) -> int:
        return len(self.updated_symbols)

    @property
    def missing_count(self) -> int:
        return len(self.missing_symbols)


@dataclass(frozen=True)
class FrameManifestV1:
    """Manifest for a bounded frame query and its deterministic input."""

    trade_date: str
    source_timezone: str
    start_ms: int
    end_exclusive_ms: int
    frame_interval_ms: int
    expected_symbols: Tuple[str, ...]
    frame_count: int
    event_count: int
    source_table: str = "market_data1.stock_tick_v2"
    query_hash: str = ""
    input_hash: str = ""
    source_sequence_status: str = SOURCE_SEQUENCE_UNKNOWN
    rabbit_arrival_order: str = RABBIT_ARRIVAL_UNKNOWN
    historical_available_at: str = HISTORICAL_AVAILABLE_AT_UNKNOWN
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _strict_trade_date(self.trade_date)
        if self.start_ms <= 0 or self.end_exclusive_ms <= self.start_ms:
            raise ValueError("manifest interval must be positive")
        if self.frame_interval_ms <= 0 or (self.end_exclusive_ms - self.start_ms) % self.frame_interval_ms:
            raise ValueError("manifest interval must divide evenly into frames")
        if self.frame_count != (self.end_exclusive_ms - self.start_ms) // self.frame_interval_ms:
            raise ValueError("frame_count does not match interval")
        if self.frame_count <= 0 or self.event_count < 0:
            raise ValueError("manifest counts are invalid")
        object.__setattr__(self, "expected_symbols", tuple(sorted({normalize_symbol(item) for item in self.expected_symbols})))
        object.__setattr__(self, "content_hash", semantic_hash({
            "contract": FRAME_MANIFEST_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "source_timezone": self.source_timezone,
            "start_ms": self.start_ms,
            "end_exclusive_ms": self.end_exclusive_ms,
            "frame_interval_ms": self.frame_interval_ms,
            "expected_symbols": self.expected_symbols,
            "frame_count": self.frame_count,
            "event_count": self.event_count,
            "source_table": self.source_table,
            "query_hash": self.query_hash,
            "input_hash": self.input_hash,
        }))
        object.__setattr__(self, "evidence_hash", evidence_hash({
            "source_sequence_status": self.source_sequence_status,
            "rabbit_arrival_order": self.rabbit_arrival_order,
            "historical_available_at": self.historical_available_at,
        }))

    @property
    def actual_frame_count(self) -> int:
        return self.frame_count

    @property
    def expected_symbol_count(self) -> int:
        return len(self.expected_symbols)


@dataclass(frozen=True)
class CrossSectionStateV1:
    """Reducer input describing the current all-symbol observation facts."""

    trade_date: str
    frame_no: int
    logical_ts_ms: int
    expected_symbols: Tuple[str, ...]
    updated_symbols: Tuple[str, ...]
    missing_symbols: Tuple[str, ...]
    symbol_states: Mapping[str, Mapping[str, Any]]
    frame_completeness: str
    coverage: float
    source_time_min_ms: Optional[int]
    source_time_max_ms: Optional[int]
    source_sequence_status: str = SOURCE_SEQUENCE_UNKNOWN
    rabbit_arrival_order: str = RABBIT_ARRIVAL_UNKNOWN
    historical_available_at: str = HISTORICAL_AVAILABLE_AT_UNKNOWN
    content_hash_override: Optional[str] = field(default=None, repr=False, compare=False)
    symbol_states_already_frozen: bool = field(default=False, repr=False, compare=False)
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    __deep_frozen_contract__ = True

    def __post_init__(self) -> None:
        _strict_trade_date(self.trade_date)
        if self.logical_ts_ms <= 0 or self.frame_no < 0:
            raise ValueError("invalid cross-section identity")
        expected = tuple(sorted({normalize_symbol(item) for item in self.expected_symbols}))
        updated = tuple(sorted({normalize_symbol(item) for item in self.updated_symbols}))
        missing = tuple(sorted({normalize_symbol(item) for item in self.missing_symbols}))
        if set(updated) | set(missing) != set(expected) or set(updated) & set(missing):
            raise ValueError("updated/missing symbols must partition expected_symbols")
        if self.frame_completeness not in {FRAME_COMPLETE, FRAME_PARTIAL, FRAME_EMPTY}:
            raise ValueError("unsupported frame completeness")
        if not 0.0 <= self.coverage <= 1.0:
            raise ValueError("coverage must be between zero and one")
        states = {
            normalize_symbol(symbol): values if self.symbol_states_already_frozen else dict(values)
            for symbol, values in self.symbol_states.items()
        }
        object.__setattr__(self, "expected_symbols", expected)
        object.__setattr__(self, "updated_symbols", updated)
        object.__setattr__(self, "missing_symbols", missing)
        object.__setattr__(
            self,
            "symbol_states",
            MappingProxyType(states) if self.symbol_states_already_frozen else deep_freeze(states),
        )
        object.__setattr__(self, "content_hash", self.content_hash_override or semantic_hash({
            "contract": CROSS_SECTION_STATE_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "frame_no": self.frame_no,
            "logical_ts_ms": self.logical_ts_ms,
            "expected_symbols": expected,
            "updated_symbols": updated,
            "missing_symbols": missing,
            "symbol_states": states,
            "frame_completeness": self.frame_completeness,
            "coverage": self.coverage,
        }))
        object.__setattr__(self, "evidence_hash", evidence_hash({
            "source_time_min_ms": self.source_time_min_ms,
            "source_time_max_ms": self.source_time_max_ms,
            "source_sequence_status": self.source_sequence_status,
            "rabbit_arrival_order": self.rabbit_arrival_order,
            "historical_available_at": self.historical_available_at,
        }))

    @property
    def symbols(self) -> Mapping[str, Mapping[str, Any]]:
        return self.symbol_states

    @property
    def expected_count(self) -> int:
        return len(self.expected_symbols)

    @property
    def observed_count(self) -> int:
        return len(self.updated_symbols)

    @property
    def missing_count(self) -> int:
        return len(self.missing_symbols)


@dataclass(frozen=True)
class CrossSectionFactsV1:
    """Fact-only aggregate with explicit denominators and scope."""

    trade_date: str
    frame_no: int
    logical_ts_ms: int
    scope: str
    expected_count: int
    observed_count: int
    missing_count: int
    coverage: float
    field_denominators: Mapping[str, int]
    market_breadth: Mapping[str, int]
    source_layers: Tuple[str, ...]
    fact_only: bool = True
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.scope not in {"FULL_MARKET", "TOP_N"}:
            raise ValueError("scope must be FULL_MARKET or TOP_N")
        if self.fact_only is not True:
            raise ValueError("cross-sectional aggregate is fact-only")
        if self.expected_count < 0 or self.observed_count < 0 or self.missing_count < 0:
            raise ValueError("aggregate counts must be non-negative")
        if self.observed_count + self.missing_count != self.expected_count:
            raise ValueError("aggregate counts must balance")
        if not 0.0 <= self.coverage <= 1.0:
            raise ValueError("coverage must be between zero and one")
        object.__setattr__(self, "field_denominators", deep_freeze(dict(self.field_denominators)))
        object.__setattr__(self, "market_breadth", deep_freeze(dict(self.market_breadth)))
        object.__setattr__(self, "source_layers", tuple(self.source_layers))
        object.__setattr__(self, "content_hash", semantic_hash({
            "contract": CROSS_SECTION_FACTS_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "frame_no": self.frame_no,
            "logical_ts_ms": self.logical_ts_ms,
            "scope": self.scope,
            "expected_count": self.expected_count,
            "observed_count": self.observed_count,
            "missing_count": self.missing_count,
            "coverage": self.coverage,
            "field_denominators": self.field_denominators,
            "market_breadth": self.market_breadth,
        }))


def build_cross_section_facts(
    state: CrossSectionStateV1,
    *,
    field_names: Sequence[str] = ("price_milli", "pre_close_milli"),
    scope: str = "FULL_MARKET",
    source_layers: Sequence[str] = (),
) -> CrossSectionFactsV1:
    """Aggregate only observed facts; no strategy or trade conclusion."""

    observed = tuple(state.updated_symbols)
    field_denominators = {
        field_name: sum(
            1
            for symbol in observed
            if state.symbol_states.get(symbol, {}).get(field_name) is not None
        )
        for field_name in field_names
    }
    breadth = {"up_count": 0, "down_count": 0, "flat_count": 0, "unknown_count": 0}
    for symbol in observed:
        values = state.symbol_states.get(symbol, {})
        price = values.get("price_milli", values.get("px_milli"))
        pre_close = values.get("pre_close_milli", values.get("pc_milli"))
        if price is None or pre_close is None:
            breadth["unknown_count"] += 1
        elif price > pre_close:
            breadth["up_count"] += 1
        elif price < pre_close:
            breadth["down_count"] += 1
        else:
            breadth["flat_count"] += 1
    return CrossSectionFactsV1(
        trade_date=state.trade_date,
        frame_no=state.frame_no,
        logical_ts_ms=state.logical_ts_ms,
        scope=scope,
        expected_count=len(state.expected_symbols),
        observed_count=len(observed),
        missing_count=len(state.missing_symbols),
        coverage=state.coverage,
        field_denominators=field_denominators,
        market_breadth=breadth,
        source_layers=source_layers,
    )


class IncrementalCrossSectionState:
    """Maintain a cheap deterministic identity for cumulative symbol facts.

    The mutable index is deliberately kept outside :class:`CrossSectionStateV1`:
    the public contract remains frozen, while FRAME/FINAL verification avoids
    serializing the full 5k-symbol cumulative mapping on every frame. Fixed
    symbol slots maintain a stable SHA-256 leaf digest and an XOR aggregate;
    this is an optimization identity, not a replacement for the FULL contract
    hash. Parity is checked by callers at finalization.
    """

    _LEAF_CONTRACT = "CrossSectionSymbolSlotLeafV1"
    _AGGREGATE_CONTRACT = "CrossSectionAggregateStateV1"

    def __init__(self, expected_symbols: Iterable[Any], *, trade_date: str = "") -> None:
        self.expected_symbols = tuple(sorted({normalize_symbol(item) for item in expected_symbols}))
        self.trade_date = trade_date
        self.latest_raw: dict[str, Mapping[str, Any]] = {}
        self._symbol_indexes = {symbol: index for index, symbol in enumerate(self.expected_symbols)}
        self._leaf_hashes = [self._leaf_hash(symbol, None) for symbol in self.expected_symbols]
        self._aggregate_xor = 0
        for digest in self._leaf_hashes:
            self._aggregate_xor ^= int(digest, 16)

    def _leaf_hash(self, symbol: str, raw: Optional[Mapping[str, Any]]) -> str:
        return semantic_hash({
            "contract": self._LEAF_CONTRACT,
            "symbol": symbol,
            "slot": self._symbol_indexes[symbol],
            "state": raw,
        })

    def _update_leaf(self, symbol: str, raw: Mapping[str, Any]) -> None:
        index = self._symbol_indexes[symbol]
        new_digest = self._leaf_hash(symbol, raw)
        self._aggregate_xor ^= int(self._leaf_hashes[index], 16)
        self._aggregate_xor ^= int(new_digest, 16)
        self._leaf_hashes[index] = new_digest

    def apply(self, events: Iterable[TDEventV1]) -> None:
        pending_raw: dict[str, Mapping[str, Any]] = {}
        for event in events:
            raw = event.to_q2_raw()
            # TDEventV1 emits scalar values, so a shallow proxy is sufficient
            # and avoids recursively freezing the full universe per frame.
            frozen_raw = MappingProxyType(raw)
            self.latest_raw[event.symbol] = frozen_raw
            pending_raw[event.symbol] = raw
        for symbol, raw in pending_raw.items():
            self._update_leaf(symbol, raw)

    @property
    def merkle_root(self) -> str:
        digest = hashlib.sha256()
        digest.update(self._AGGREGATE_CONTRACT.encode("utf-8"))
        digest.update(b"\0")
        digest.update(self._aggregate_xor.to_bytes(32, "big"))
        return digest.hexdigest()

    @property
    def aggregate_state_hash(self) -> str:
        return self.merkle_root

    def full_state_hash(
        self,
        *,
        frame_no: int,
        logical_ts_ms: int,
        updated_symbols: Sequence[str],
        missing_symbols: Sequence[str],
        completeness: str,
        coverage: float,
        source_time_min_ms: Optional[int] = None,
        source_time_max_ms: Optional[int] = None,
    ) -> str:
        """Build the public FULL hash once for final parity verification."""

        return CrossSectionStateV1(
            trade_date=self.trade_date,
            frame_no=frame_no,
            logical_ts_ms=logical_ts_ms,
            expected_symbols=self.expected_symbols,
            updated_symbols=updated_symbols,
            missing_symbols=missing_symbols,
            symbol_states=self.latest_raw,
            frame_completeness=completeness,
            coverage=coverage,
            source_time_min_ms=source_time_min_ms,
            source_time_max_ms=source_time_max_ms,
            symbol_states_already_frozen=True,
        ).content_hash

    def identity_hash(
        self,
        *,
        frame_no: int,
        logical_ts_ms: int,
        updated_symbols: Sequence[str],
        missing_symbols: Sequence[str],
        completeness: str,
        coverage: float,
    ) -> str:
        return semantic_hash({
            "contract": CROSS_SECTION_STATE_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "frame_no": frame_no,
            "logical_ts_ms": logical_ts_ms,
            "expected_symbols": self.expected_symbols,
            "updated_symbols": tuple(updated_symbols),
            "missing_symbols": tuple(missing_symbols),
            "merkle_root": self.merkle_root,
            "frame_completeness": completeness,
            "coverage": coverage,
        })


@dataclass(frozen=True)
class CrossSectionProjectionV1:
    """Q2-compatible view carrying the explicit cross-sectional state."""

    base_projection: Q2ProjectionSnapshot
    cross_section: CrossSectionStateV1

    __deep_frozen_contract__ = True

    @property
    def trade_date(self) -> str:
        return self.base_projection.trade_date

    @property
    def envelope(self) -> Any:
        return self.base_projection.envelope

    @property
    def quotes(self) -> Mapping[str, Any]:
        return self.base_projection.quotes

    @property
    def expected_symbols(self) -> Tuple[str, ...]:
        return self.cross_section.expected_symbols

    @property
    def missing_symbols(self) -> Tuple[str, ...]:
        return self.cross_section.missing_symbols

    @property
    def stale_symbols(self) -> Tuple[str, ...]:
        return self.base_projection.stale_symbols

    @property
    def coverage(self) -> float:
        return self.cross_section.coverage

    @property
    def status(self) -> DataStatus:
        if self.cross_section.frame_completeness == FRAME_EMPTY:
            return DataStatus.MISSING
        if self.cross_section.frame_completeness == FRAME_PARTIAL:
            return DataStatus.PARTIAL
        return DataStatus.READY

    @property
    def consistency_status(self) -> str:
        return "CROSS_SECTION_%s" % self.cross_section.frame_completeness

    @property
    def oldest_source_time_ms(self) -> Optional[int]:
        return self.cross_section.source_time_min_ms

    @property
    def newest_source_time_ms(self) -> Optional[int]:
        return self.cross_section.source_time_max_ms

    @property
    def content_hash(self) -> str:
        return semantic_hash({
            "contract": CROSS_SECTION_PROJECTION_CONTRACT_VERSION,
            "base": self.base_projection.content_hash,
            "cross_section": self.cross_section.content_hash,
        })


class CrossSectionReplaySource:
    """Create exactly one deterministic market update per fixed frame."""

    def __init__(
        self,
        trade_date: str,
        expected_symbols: Iterable[Any],
        clock: VirtualClock,
        *,
        slice_anchor_ms: int,
        slice_ms: int = 3_000,
        end_exclusive_ms: Optional[int] = None,
        source_timezone: tzinfo = SHANGHAI,
        freshness_policy: FreshnessPolicy = FreshnessPolicy(),
        source_id: str = "td_cross_section_replay",
    ) -> None:
        _strict_trade_date(trade_date)
        if slice_anchor_ms <= 0 or slice_ms <= 0:
            raise ValueError("slice_anchor_ms and slice_ms must be positive")
        self.trade_date = trade_date
        self.expected_symbols = tuple(sorted({normalize_symbol(item) for item in expected_symbols}))
        if not self.expected_symbols:
            raise ValueError("expected_symbols must not be empty")
        self.clock = clock
        self.slice_anchor_ms = slice_anchor_ms
        self.slice_ms = slice_ms
        # The bounded migration contract is a 25-minute morning window.  A
        # caller may still provide another explicit whole-frame endpoint for
        # fixture tests, but an omitted endpoint remains the safe fixed window.
        self.end_exclusive_ms = (
            end_exclusive_ms
            if end_exclusive_ms is not None
            else slice_anchor_ms + 25 * 60 * 1000
        )
        self.source_timezone = source_timezone
        self.freshness_policy = freshness_policy
        self.source_id = source_id

    @property
    def frame_count(self) -> int:
        if self.end_exclusive_ms is None:
            raise ValueError("end_exclusive_ms is required to determine frame_count")
        span = self.end_exclusive_ms - self.slice_anchor_ms
        if span <= 0 or span % self.slice_ms:
            raise ValueError("replay interval must contain whole frames")
        return span // self.slice_ms

    def frames(
        self,
        rows: Iterable[Mapping[str, Any] | TDEventV1],
        *,
        start_ms: Optional[int] = None,
        end_exclusive_ms: Optional[int] = None,
    ) -> Tuple[MarketFrameV1, ...]:
        start = self.slice_anchor_ms if start_ms is None else start_ms
        end = self.end_exclusive_ms if end_exclusive_ms is None else end_exclusive_ms
        if end is None or start != self.slice_anchor_ms:
            raise ValueError("bounded cross-sectional replay requires configured interval")
        span = end - start
        if span <= 0 or span % self.slice_ms:
            raise ValueError("replay interval must contain whole frames")
        events = [
            row if isinstance(row, TDEventV1) else TDEventV1.from_mapping(row, source_timezone=self.source_timezone)
            for row in rows
        ]
        events.sort(key=lambda event: (event.event_time_ms, event.symbol, event.content_hash))
        grouped: dict[int, list[TDEventV1]] = {}
        expected_set = set(self.expected_symbols)
        for event in events:
            if _event_local_date(event, self.source_timezone) != self.trade_date:
                raise ValueError("TD tick event date does not match trade_date")
            if event.symbol not in expected_set:
                raise ValueError("TD tick symbol is outside expected_symbols")
            if event.event_time_ms < start or event.event_time_ms >= end:
                raise ValueError("TD tick is outside the bounded replay interval")
            frame_no = (event.event_time_ms - start) // self.slice_ms
            grouped.setdefault(frame_no, []).append(event)
        result = []
        for frame_no in range(span // self.slice_ms):
            frame_start = start + frame_no * self.slice_ms
            frame_end = frame_start + self.slice_ms
            frame_events = tuple(grouped.get(frame_no, ()))
            updated = tuple(sorted({event.symbol for event in frame_events}))
            updated_set = set(updated)
            missing = tuple(symbol for symbol in self.expected_symbols if symbol not in updated_set)
            if not frame_events:
                completeness = FRAME_EMPTY
            elif missing:
                completeness = FRAME_PARTIAL
            else:
                completeness = FRAME_COMPLETE
            times = [event.event_time_ms for event in frame_events]
            result.append(MarketFrameV1(
                trade_date=self.trade_date,
                frame_no=frame_no,
                start_ms=frame_start,
                end_exclusive_ms=frame_end,
                expected_symbols=self.expected_symbols,
                updated_symbols=updated,
                missing_symbols=missing,
                events=frame_events,
                completeness=completeness,
                source_time_min_ms=min(times) if times else None,
                source_time_max_ms=max(times) if times else None,
            ))
        return tuple(result)

    def frame_from_events(
        self,
        frame_no: int,
        events: Iterable[Mapping[str, Any] | TDEventV1],
        *,
        presorted: bool = False,
    ) -> MarketFrameV1:
        """Build one frame from a bounded 3-second query result.

        This is the streaming counterpart to :meth:`frames`: callers may
        query one half-open interval at a time and never materialize the full
        trading window in memory. ``presorted`` is safe only when the source
        guarantees ``event_time_ms, symbol, content_hash`` order; bounds and
        symbol membership are still validated here.
        """

        if frame_no < 0 or frame_no >= self.frame_count:
            raise ValueError("frame_no is outside the configured replay window")
        frame_start = self.slice_anchor_ms + frame_no * self.slice_ms
        frame_end = frame_start + self.slice_ms
        normalized = [
            item if isinstance(item, TDEventV1) else TDEventV1.from_mapping(item, source_timezone=self.source_timezone)
            for item in events
        ]
        if not presorted:
            normalized.sort(key=lambda event: (event.event_time_ms, event.symbol, event.content_hash))
        expected_set = set(self.expected_symbols)
        for event in normalized:
            if event.symbol not in expected_set:
                raise ValueError("TD tick symbol is outside expected_symbols")
            if event.event_time_ms < frame_start or event.event_time_ms >= frame_end:
                raise ValueError("TD tick is outside the frame interval")
            if _event_local_date(event, self.source_timezone) != self.trade_date:
                raise ValueError("TD tick event date does not match trade_date")
        updated = tuple(sorted({event.symbol for event in normalized}))
        updated_set = set(updated)
        missing = tuple(symbol for symbol in self.expected_symbols if symbol not in updated_set)
        completeness = FRAME_EMPTY if not normalized else FRAME_COMPLETE if not missing else FRAME_PARTIAL
        times = [event.event_time_ms for event in normalized]
        return MarketFrameV1(
            trade_date=self.trade_date,
            frame_no=frame_no,
            start_ms=frame_start,
            end_exclusive_ms=frame_end,
            expected_symbols=self.expected_symbols,
            updated_symbols=updated,
            missing_symbols=missing,
            events=tuple(normalized),
            completeness=completeness,
            source_time_min_ms=min(times) if times else None,
            source_time_max_ms=max(times) if times else None,
        )

    def manifest(self, frames: Sequence[MarketFrameV1], *, source_table: str = "market_data1.stock_tick_v2", query_hash: str = "") -> FrameManifestV1:
        if not frames:
            raise ValueError("frames must not be empty")
        return FrameManifestV1(
            trade_date=self.trade_date,
            source_timezone=getattr(self.source_timezone, "key", str(self.source_timezone)),
            start_ms=frames[0].start_ms,
            end_exclusive_ms=frames[-1].end_exclusive_ms,
            frame_interval_ms=self.slice_ms,
            expected_symbols=self.expected_symbols,
            frame_count=len(frames),
            event_count=sum(len(frame.events) for frame in frames),
            source_table=source_table,
            query_hash=query_hash,
            input_hash=semantic_hash(tuple(event.content_hash for frame in frames for event in frame.events)),
        )

    def iter_signals(self, frames: Sequence[MarketFrameV1], *, signal_prefix: str = "cross-section") -> Iterable[EngineSignal]:
        """Yield one signal per frame without retaining all projections."""

        latest_raw: dict[str, Mapping[str, Any]] = {}
        for frame in frames:
            for event in frame.events:
                latest_raw[event.symbol] = event.to_q2_raw()
            observed_at = datetime.fromtimestamp(frame.logical_ts_ms / 1000.0, tz=timezone.utc)
            base = build_q2_projection(
                self.trade_date,
                observed_at,
                self.expected_symbols,
                latest_raw,
                freshness_policy=self.freshness_policy,
                source_id=self.source_id,
            )
            state = CrossSectionStateV1(
                trade_date=self.trade_date,
                frame_no=frame.frame_no,
                logical_ts_ms=frame.logical_ts_ms,
                expected_symbols=self.expected_symbols,
                updated_symbols=frame.updated_symbols,
                missing_symbols=frame.missing_symbols,
                symbol_states=latest_raw,
                frame_completeness=frame.completeness,
                coverage=frame.coverage,
                source_time_min_ms=frame.source_time_min_ms,
                source_time_max_ms=frame.source_time_max_ms,
            )
            projection = CrossSectionProjectionV1(base_projection=base, cross_section=state)
            yield EngineSignal(
                signal_id=f"{signal_prefix}:frame:{frame.frame_no}:{frame.content_hash[:16]}",
                logical_time_ms=frame.logical_ts_ms,
                signal_seq=frame.frame_no + 1,
                signal_kind=SignalKind.MARKET_UPDATE,
                payload=projection,
            )

    def _signals_from_frames(self, frames: Sequence[MarketFrameV1], *, signal_prefix: str) -> Tuple[EngineSignal, ...]:
        return tuple(self.iter_signals(frames, signal_prefix=signal_prefix))

    def signal_for_frame(
        self,
        frame: MarketFrameV1,
        latest_raw: dict[str, Mapping[str, Any]],
        *,
        signal_prefix: str = "cross-section",
        state_content_hash_override: Optional[str] = None,
        projection_builder: Any = None,
        symbol_states_already_frozen: bool = False,
    ) -> EngineSignal:
        """Create one frame signal while updating a caller-owned cumulative state."""

        for event in frame.events:
            latest_raw[event.symbol] = event.to_q2_raw()
        observed_at = datetime.fromtimestamp(frame.logical_ts_ms / 1000.0, tz=timezone.utc)
        if projection_builder is None:
            base = build_q2_projection(
                self.trade_date,
                observed_at,
                self.expected_symbols,
                latest_raw,
                freshness_policy=self.freshness_policy,
                source_id=self.source_id,
            )
        else:
            base = projection_builder.build(
                observed_at,
                latest_raw,
                changed_symbols=frame.updated_symbols,
                full_hash=state_content_hash_override is None,
            )
        state = CrossSectionStateV1(
            trade_date=self.trade_date,
            frame_no=frame.frame_no,
            logical_ts_ms=frame.logical_ts_ms,
            expected_symbols=self.expected_symbols,
            updated_symbols=frame.updated_symbols,
            missing_symbols=frame.missing_symbols,
            symbol_states=latest_raw,
            frame_completeness=frame.completeness,
            coverage=frame.coverage,
            source_time_min_ms=frame.source_time_min_ms,
            source_time_max_ms=frame.source_time_max_ms,
            content_hash_override=state_content_hash_override,
            symbol_states_already_frozen=symbol_states_already_frozen,
        )
        projection = CrossSectionProjectionV1(base_projection=base, cross_section=state)
        return EngineSignal(
            signal_id=f"{signal_prefix}:frame:{frame.frame_no}:{frame.content_hash[:16]}",
            logical_time_ms=frame.logical_ts_ms,
            signal_seq=frame.frame_no + 1,
            signal_kind=SignalKind.MARKET_UPDATE,
            payload=projection,
        )

    def signals_for(self, rows: Iterable[Mapping[str, Any] | TDEventV1], *, signal_prefix: str = "cross-section") -> Tuple[EngineSignal, ...]:
        return self._signals_from_frames(self.frames(rows), signal_prefix=signal_prefix)

    def event_frames(self, rows: Iterable[Mapping[str, Any] | TDEventV1], **kwargs: Any) -> Tuple[MarketFrameV1, ...]:
        """Compatibility spelling used by replay runners."""

        return self.frames(rows, **kwargs)

    def replay(self, rows: Iterable[Mapping[str, Any] | TDEventV1], engine: Any, *, signal_prefix: str = "cross-section") -> None:
        signals = self.signals_for(rows, signal_prefix=signal_prefix)
        for signal in signals:
            self.clock.advance_to(datetime.fromtimestamp(signal.logical_time_ms / 1000.0, tz=timezone.utc))
            engine.submit(signal)
            engine.run_until_empty()


def replay_cross_section(rows: Iterable[Mapping[str, Any] | TDEventV1], source: CrossSectionReplaySource, engine: Any, *, signal_prefix: str = "cross-section") -> None:
    """Convenience function retaining one Engine and one update per frame."""

    source.replay(rows, engine, signal_prefix=signal_prefix)
