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
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


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
    effective_time_ms: int
    observed_time_ms: int
    generation: Optional[str]
    generation_kind: str
    payload: Any
    provenance: Provenance


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


@dataclass(frozen=True)
class FrozenDataBundle:
    evaluation_id: str
    knowledge_as_of_ms: int
    results_by_function: Mapping[str, DataResult]
    completeness: float
    content_hash: str
    function_order: Tuple[str, ...] = ()

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
            results_by_function={},
            completeness=1.0,
            content_hash=canonical_hash(content),
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
        return cls(
            evaluation_id=evaluation_id,
            knowledge_as_of_ms=knowledge_as_of_ms,
            results_by_function=ordered,
            completeness=completeness,
            content_hash=canonical_hash(content),
            function_order=function_order,
        )


@dataclass(frozen=True)
class StrategyResult:
    strategy_id: str
    evaluation_id: Optional[str]
    state: str
    trace: Mapping[str, Any]
    evidence_refs: Tuple[str, ...]
    content_hash: str
