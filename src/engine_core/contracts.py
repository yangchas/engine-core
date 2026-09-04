"""Small immutable contracts shared by the first vertical slice.

The contracts intentionally contain no Redis, TDengine, RabbitMQ or strategy
implementation details.  They are the boundary that lets those concerns be
replaced in tests and replay.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, TypeVar


class PayloadKind(str, Enum):
    RAW_TICK_BATCH = "RAW_TICK_BATCH"
    L2_PROJECTION_SNAPSHOT = "L2_PROJECTION_SNAPSHOT"
    FEATURE_BATCH = "FEATURE_BATCH"


class SignalKind(str, Enum):
    MARKET_UPDATE = "MARKET_UPDATE"
    PULSE = "PULSE"
    TIMER = "TIMER"
    DATA_READY = "DATA_READY"
    RECOVERY_CATCHUP = "RECOVERY_CATCHUP"

    @property
    def priority(self) -> int:
        return {
            SignalKind.MARKET_UPDATE: 10,
            SignalKind.DATA_READY: 20,
            SignalKind.PULSE: 30,
            SignalKind.TIMER: 40,
            SignalKind.RECOVERY_CATCHUP: 50,
        }[self]


class DataStatus(str, Enum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    MISSING = "MISSING"
    INVALID = "INVALID"
    ERROR = "ERROR"


T = TypeVar("T")
SERIALIZER_VERSION = 1


def deep_freeze(value: T) -> T:
    """Freeze containers without changing business values.

    Normalization, validation, unit conversion and sorting deliberately do not
    happen here.  Lists become tuples, mappings become read-only mappings and
    dataclasses are rebuilt with recursively frozen fields.  Unordered sets
    are rejected instead of receiving an invented business order.
    """

    if value is None or isinstance(value, (str, bool, int, float, bytes)):
        return value
    if isinstance(value, (set, frozenset)):
        raise TypeError("unordered sets are not allowed in frozen data")
    if isinstance(value, Mapping):
        frozen = {
            key: deep_freeze(item)
            for key, item in value.items()
        }
        return MappingProxyType(frozen)  # type: ignore[return-value]
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)  # type: ignore[return-value]
    if is_dataclass(value):
        frozen_fields = {
            item.name: deep_freeze(getattr(value, item.name))
            for item in fields(value)
        }
        return type(value)(**frozen_fields)  # type: ignore[return-value,call-arg]
    return value


def _canonical_value(value: Any) -> Any:
    """Return a strict JSON-compatible value for semantic hashing.

    The function rejects values whose implicit string conversion could hide a
    field or ordering problem.  Callers must normalize bytes and enums before
    crossing a data boundary.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and Infinity are not allowed in canonical data")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime is not allowed")
        return int(value.astimezone(timezone.utc).timestamp() * 1000)
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if is_dataclass(value):
        return {
            item.name: _canonical_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key in value:
            if not isinstance(key, str):
                raise TypeError("canonical mapping keys must be strings")
            result[key] = _canonical_value(value[key])
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise TypeError("unordered sets are not allowed in canonical data")
    if isinstance(value, bytes):
        raise TypeError("bytes must be decoded before canonical serialization")
    raise TypeError("unsupported canonical value: %s" % type(value).__name__)


def canonical_json(value: Any) -> str:
    """Serialize a normalized value deterministically as UTF-8 JSON."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    """Return the SHA-256 hash of strict canonical JSON."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _versioned_hash(value: Any, *, kind: str, schema_version: int) -> str:
    if schema_version <= 0:
        raise ValueError("schema_version must be positive")
    return canonical_hash(
        {
            "hash_kind": kind,
            "schema_version": schema_version,
            "serializer_version": SERIALIZER_VERSION,
            "value": value,
        }
    )


def semantic_hash(value: Any, *, schema_version: int = 1) -> str:
    """Hash only canonical business input/output, not evidence lineage."""

    return _versioned_hash(value, kind="semantic", schema_version=schema_version)


def evidence_hash(value: Any, *, schema_version: int = 1) -> str:
    """Hash provenance/evidence separately from semantic business content."""

    return _versioned_hash(value, kind="evidence", schema_version=schema_version)


def trunc_div(numerator: int, denominator: int) -> int:
    """Integer division truncated toward zero (C/C++ compatible)."""

    if not isinstance(numerator, int) or not isinstance(denominator, int):
        raise TypeError("trunc_div requires integer operands")
    if denominator == 0:
        raise ZeroDivisionError("division by zero")
    quotient = abs(numerator) // abs(denominator)
    return -quotient if (numerator < 0) != (denominator < 0) else quotient


@dataclass(frozen=True)
class Provenance:
    source_id: str
    source_kind: str
    source_schema: str
    source_trade_date: Optional[str]
    effective_at_ms: Optional[int]
    observed_at_ms: int
    evidence_ref: Optional[str] = None
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketDataEnvelope:
    envelope_id: str
    payload_kind: PayloadKind
    source_id: str
    schema_version: int
    effective_time_ms: Optional[int]
    observed_time_ms: int
    generation: Optional[str]
    generation_kind: str
    payload: Any
    provenance: Provenance

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", deep_freeze(self.payload))
        object.__setattr__(self, "provenance", deep_freeze(self.provenance))


@dataclass(frozen=True)
class EngineSignal:
    signal_id: str
    logical_time_ms: int
    signal_seq: int
    signal_kind: SignalKind
    payload: Any

    @property
    def sort_key(self) -> Tuple[int, int, int, str]:
        return (
            self.logical_time_ms,
            self.signal_kind.priority,
            self.signal_seq,
            self.signal_id,
        )


@dataclass(frozen=True)
class WindowView:
    window_id: str
    revision: int
    start_ms: int
    end_exclusive_ms: int
    observation_count: int
    first_source_time_ms: Optional[int]
    last_source_time_ms: Optional[int]
    coverage: float
    completeness: str
    content_hash: str
    finality: str = "OPEN"
    origin: str = "NORMAL"


@dataclass(frozen=True)
class EngineSnapshot:
    snapshot_id: str
    trigger_id: str
    logical_time_ms: int
    session_id: str
    phase: str
    market_state_revision: int
    source_observation_metadata: Mapping[str, Any]
    symbol_states: Mapping[str, Mapping[str, Any]]
    raw_market_cross_section: Mapping[str, Any]
    raw_theme_cross_section: Mapping[str, Any]
    windows: Mapping[str, WindowView]
    coverage: float
    completeness: str
    content_hash: str
    evidence_refs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_observation_metadata",
            deep_freeze(self.source_observation_metadata),
        )
        object.__setattr__(self, "symbol_states", deep_freeze(self.symbol_states))
        object.__setattr__(
            self,
            "raw_market_cross_section",
            deep_freeze(self.raw_market_cross_section),
        )
        object.__setattr__(
            self,
            "raw_theme_cross_section",
            deep_freeze(self.raw_theme_cross_section),
        )
        object.__setattr__(self, "windows", deep_freeze(self.windows))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


@dataclass(frozen=True)
class DataRequest:
    request_id: str
    function_id: str
    trade_date: str
    effective_as_of_ms: int
    knowledge_as_of_ms: int
    symbols: Tuple[str, ...] = ()
    required_fields: Tuple[str, ...] = ()
    freshness_max_age_ms: Optional[int] = None
    catalog_version: Optional[str] = None
    purpose: str = ""


@dataclass(frozen=True)
class DataResult:
    request_id: str
    function_id: str
    status: DataStatus
    data: Any
    actual_source: Optional[str]
    requested_trade_date: str
    actual_trade_date: Optional[str]
    effective_at_ms: Optional[int]
    available_at_ms: Optional[int]
    observed_at_ms: int
    schema_version: int
    completeness: float
    missing_fields: Tuple[str, ...] = ()
    missing_symbols: Tuple[str, ...] = ()
    content_hash: str = ""
    provenance: Tuple[Provenance, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", deep_freeze(self.data))
        object.__setattr__(self, "missing_fields", tuple(self.missing_fields))
        object.__setattr__(self, "missing_symbols", tuple(self.missing_symbols))
        object.__setattr__(self, "provenance", deep_freeze(self.provenance))


@dataclass(frozen=True)
class FrozenDataBundle:
    evaluation_id: str
    knowledge_as_of_ms: int
    results_by_function: Mapping[str, DataResult]
    completeness: float
    content_hash: str
    function_order: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "results_by_function",
            deep_freeze(self.results_by_function),
        )
        object.__setattr__(self, "function_order", tuple(self.function_order))

    @classmethod
    def empty(cls, evaluation_id: str, knowledge_as_of_ms: int) -> "FrozenDataBundle":
        content = {
            "evaluation_id": evaluation_id,
            "knowledge_as_of_ms": knowledge_as_of_ms,
            "results_by_function": {},
        }
        return cls(
            evaluation_id=evaluation_id,
            knowledge_as_of_ms=knowledge_as_of_ms,
            results_by_function=MappingProxyType({}),
            completeness=1.0,
            content_hash=semantic_hash(content),
            function_order=(),
        )

    @classmethod
    def from_results(
        cls,
        evaluation_id: str,
        knowledge_as_of_ms: int,
        function_order: Tuple[str, ...],
        results_by_function: Mapping[str, DataResult],
    ) -> "FrozenDataBundle":
        """Build a bundle in declared function order, not completion order."""

        if len(function_order) != len(set(function_order)):
            raise ValueError("function_order must not contain duplicates")
        missing = [
            function_id
            for function_id in function_order
            if function_id not in results_by_function
        ]
        if missing:
            raise ValueError("missing DataResult values: %s" % ", ".join(missing))
        ordered = {
            function_id: results_by_function[function_id]
            for function_id in function_order
        }
        extra = sorted(set(results_by_function).difference(function_order))
        for function_id in extra:
            ordered[function_id] = results_by_function[function_id]
        completeness = min(
            (result.completeness for result in ordered.values()),
            default=1.0,
        )
        content = {
            "evaluation_id": evaluation_id,
            "knowledge_as_of_ms": knowledge_as_of_ms,
            "function_order": function_order,
            "results": [
                {
                    "function_id": function_id,
                    "result": ordered[function_id],
                }
                for function_id in function_order
            ],
            "extra_results": [
                {
                    "function_id": function_id,
                    "result": ordered[function_id],
                }
                for function_id in extra
            ],
        }
        frozen_ordered = deep_freeze(ordered)
        return cls(
            evaluation_id=evaluation_id,
            knowledge_as_of_ms=knowledge_as_of_ms,
            results_by_function=frozen_ordered,
            completeness=completeness,
            content_hash=semantic_hash(content),
            function_order=tuple(function_order),
        )


@dataclass(frozen=True)
class StrategyResult:
    strategy_id: str
    evaluation_id: Optional[str]
    state: str
    trace: Mapping[str, Any]
    evidence_refs: Tuple[str, ...]
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace", deep_freeze(self.trace))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
