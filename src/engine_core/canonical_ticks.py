"""Pure Rabbit/TD canonical tick and batch contracts.

This module is intentionally an in-memory boundary.  It accepts parsed
protobuf-like objects or already-read TD rows and never imports a Rabbit,
Redis or TDengine client.  Both sources project into the same immutable
``MarketTickV1``/``TickBatchV1`` shape; the legacy ``TDEventV1`` path remains
available as a shadow/oracle projection.  RabbitMQ ``DataRecord/DataBatch``
decoded into the existing C++ ``RawTick/TickBatch`` is the authoritative wire
shape; TD is compatibility input and cannot extend its semantics.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .replay import SHANGHAI, TDEventV1


CANONICAL_TICK_CONTRACT_VERSION = "MarketTickV1"
CANONICAL_BATCH_CONTRACT_VERSION = "TickBatchV1"
CANONICAL_HASH_ENCODING_VERSION = "CanonicalHashEncodingV1"
CANONICAL_SOURCE_AUTHORITY = "RABBITMQ_DATASERVICE_RAWTICK_V1"


class FieldQuality(str, Enum):
    PRESENT_VALUE = "PRESENT_VALUE"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"
    WIRE_DEFAULT_AMBIGUOUS = "WIRE_DEFAULT_AMBIGUOUS"
    INVALID = "INVALID"


class FieldProvenance(str, Enum):
    SOURCE_SNAPSHOT = "SOURCE_SNAPSHOT"
    SOURCE_EVENT = "SOURCE_EVENT"
    DERIVED_DELTA = "DERIVED_DELTA"
    SOURCE_FLAG = "SOURCE_FLAG"
    UNKNOWN = "UNKNOWN"


class BatchQuality(str, Enum):
    EMPTY = "EMPTY"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class SequenceStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ArrivalOrderStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReplayOrderStatus(str, Enum):
    SYNTHETIC_DETERMINISTIC = "SYNTHETIC_DETERMINISTIC"
    SOURCE_ORDER = "SOURCE_ORDER"
    UNKNOWN = "UNKNOWN"


class HistoricalAvailabilityStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    UNPROVEN = "UNPROVEN"


class ParityResult(str, Enum):
    STRICT_EQUAL = "STRICT_EQUAL"
    VALUE_EQUAL_QUALITY_DIFFERENT = "VALUE_EQUAL_QUALITY_DIFFERENT"
    ORDER_AMBIGUOUS = "ORDER_AMBIGUOUS"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    VALUE_MISMATCH = "VALUE_MISMATCH"


class CompatibilityResult(str, Enum):
    STRICT_COMPATIBLE = "STRICT_COMPATIBLE"
    ORDER_AMBIGUOUS = "ORDER_AMBIGUOUS"
    VALUE_MISMATCH = "VALUE_MISMATCH"


_TICK_VALUE_FIELDS: Tuple[str, ...] = (
    "px_milli", "pc_milli", "o_milli", "h_milli", "l_milli",
    "amt_yuan", "vol_units", "inst_vol", "inst_amt_yuan",
    "large_net_yuan", "limit_up_milli", "limit_down_milli",
    "limit_band_bp", "no_price_limit", "is_st",
    "ap_milli", "bp_milli", "av", "bv",
)
_PRICE_FIELDS = {"px_milli", "pc_milli", "o_milli", "h_milli", "l_milli",
                 "limit_up_milli", "limit_down_milli"}
_INT_FIELDS = set(_TICK_VALUE_FIELDS) - {"ap_milli", "bp_milli", "av", "bv",
                                          "no_price_limit", "is_st"}
_ARRAY_FIELDS = {"ap_milli", "bp_milli", "av", "bv"}


def _pack_len(size: int) -> bytes:
    return struct.pack(">Q", size)


def _encode(value: Any) -> bytes:
    """Encode contract values without relying on dict/JSON ordering."""

    if value is None:
        return b"N"
    if isinstance(value, bool):
        return b"B1" if value else b"B0"
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return b"I" + struct.pack(">q", value)
        except struct.error as exc:
            raise OverflowError("canonical integer is outside int64") from exc
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return b"S" + _pack_len(len(raw)) + raw
    if isinstance(value, bytes):
        return b"Y" + _pack_len(len(value)) + value
    if isinstance(value, Enum):
        return _encode(value.value)
    if isinstance(value, tuple):
        encoded = b"".join(_encode(item) for item in value)
        return b"T" + _pack_len(len(value)) + encoded
    if isinstance(value, list):
        encoded = b"".join(_encode(item) for item in value)
        return b"L" + _pack_len(len(value)) + encoded
    raise TypeError("unsupported CanonicalHashEncodingV1 value: %s" % type(value).__name__)


def _hash(kind: str, payload: Any) -> str:
    encoded = _encode((CANONICAL_HASH_ENCODING_VERSION, kind, payload))
    return hashlib.sha256(encoded).hexdigest()


def cxx_llround(value: float) -> int:
    """Match C++ ``std::llround``: ties away from zero."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("llround requires a numeric value")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("non-finite value cannot enter canonical numeric data")
    if numeric >= 0:
        rounded = math.floor(numeric + 0.5)
    else:
        rounded = math.ceil(numeric - 0.5)
    if rounded < -(2**63) or rounded > 2**63 - 1:
        raise OverflowError("llround result is outside int64")
    return int(rounded)


def _strict_symbol(value: Any) -> str:
    """Reproduce ``RawTickConverter::copy_symbol`` for canonical input."""

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    text = str(value).strip()
    if len(text) != 6 or not text.isascii() or not text.isdigit():
        raise ValueError("symbol must be exactly six ASCII digits")
    return text


def normalize_td_symbol(value: Any) -> str:
    """Reproduce the existing C++ TD row pre-normalization exactly."""

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    text = str(value)
    colon = text.find(":")
    if colon >= 0:
        text = text[colon + 1:]
    dot = text.find(".")
    if dot >= 0:
        text = text[:dot]
    if len(text) > 6:
        text = text[-6:]
    return _strict_symbol(text)


def _effective_market(symbol: str, exchange: Any, market: Any) -> str:
    """Match the existing C++ source converter's market fallback."""

    market_text = str(market or "")
    if market_text:
        return market_text.lower()
    exchange_text = str(exchange or "").lower()
    if exchange_text == "sz":
        return "sz"
    if symbol.startswith("68"):
        return "kc"
    if exchange_text == "sh":
        return "sh"
    return ""


def _default_provenance(field_name: str) -> FieldProvenance:
    if field_name in {"inst_vol", "inst_amt_yuan", "large_net_yuan"}:
        return FieldProvenance.DERIVED_DELTA
    if field_name in {"no_price_limit", "is_st"}:
        return FieldProvenance.SOURCE_FLAG
    return FieldProvenance.SOURCE_SNAPSHOT


@dataclass(frozen=True)
class FieldMetaV1:
    quality: FieldQuality
    provenance: FieldProvenance = FieldProvenance.UNKNOWN
    invalid_reason: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "quality", FieldQuality(self.quality))
        object.__setattr__(self, "provenance", FieldProvenance(self.provenance))
        if self.quality is FieldQuality.INVALID and not self.invalid_reason:
            raise ValueError("INVALID field metadata requires invalid_reason")
        if self.quality is not FieldQuality.INVALID and self.invalid_reason is not None:
            raise ValueError("invalid_reason is only valid for INVALID fields")


def _default_value(field_name: str) -> Any:
    if field_name in _ARRAY_FIELDS:
        return (None,) * 5
    return None


def _coerce_field_meta(value: Any, field_name: str, field_value: Any) -> FieldMetaV1:
    if value is None:
        null_value = field_value is None or (
            isinstance(field_value, (tuple, list))
            and all(item is None for item in field_value)
        )
        quality = FieldQuality.UNKNOWN if null_value else FieldQuality.PRESENT_VALUE
        return FieldMetaV1(quality, _default_provenance(field_name))
    if isinstance(value, FieldMetaV1):
        return value
    if isinstance(value, Mapping):
        return FieldMetaV1(
            FieldQuality(value["quality"]),
            FieldProvenance(value.get("provenance", FieldProvenance.UNKNOWN)),
            value.get("invalid_reason"),
        )
    raise TypeError("field_meta values must be FieldMetaV1")


def _validate_quality(field_name: str, value: Any, meta: FieldMetaV1) -> None:
    null_value = value is None or (
        isinstance(value, (tuple, list)) and all(item is None for item in value)
    )
    if meta.quality is FieldQuality.PRESENT_VALUE and null_value:
        raise ValueError("PRESENT_VALUE requires a value for %s" % field_name)
    if meta.quality in {FieldQuality.MISSING, FieldQuality.INVALID} and not null_value:
        raise ValueError("%s requires typed NULL for %s" % (meta.quality.value, field_name))
    if meta.quality is FieldQuality.UNKNOWN and value is not None and not isinstance(value, (tuple, list)):
        raise ValueError("UNKNOWN scalar requires typed NULL for %s" % field_name)
    if meta.quality is FieldQuality.WIRE_DEFAULT_AMBIGUOUS and null_value:
        raise ValueError("WIRE_DEFAULT_AMBIGUOUS requires the wire default value")


@dataclass(frozen=True)
class MarketTickV1:
    trade_date: str
    event_time_ms: int
    symbol: str
    market_code: str = ""
    px_milli: Optional[int] = None
    pc_milli: Optional[int] = None
    o_milli: Optional[int] = None
    h_milli: Optional[int] = None
    l_milli: Optional[int] = None
    amt_yuan: Optional[int] = None
    vol_units: Optional[int] = None
    inst_vol: Optional[int] = None
    inst_amt_yuan: Optional[int] = None
    large_net_yuan: Optional[int] = None
    limit_up_milli: Optional[int] = None
    limit_down_milli: Optional[int] = None
    limit_band_bp: Optional[int] = None
    no_price_limit: Optional[bool] = None
    is_st: Optional[bool] = None
    ap_milli: Tuple[Optional[int], ...] = field(default_factory=lambda: (None,) * 5)
    bp_milli: Tuple[Optional[int], ...] = field(default_factory=lambda: (None,) * 5)
    av: Tuple[Optional[int], ...] = field(default_factory=lambda: (None,) * 5)
    bv: Tuple[Optional[int], ...] = field(default_factory=lambda: (None,) * 5)
    field_meta: Tuple[Tuple[str, FieldMetaV1], ...] = ()
    schema_version: int = 1
    canonical_value_hash: str = field(init=False)
    canonical_semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.trade_date, str):
            raise TypeError("trade_date must be a string")
        try:
            parsed_date = datetime.fromisoformat(self.trade_date)
        except ValueError as exc:
            raise ValueError("trade_date must be YYYY-MM-DD") from exc
        if parsed_date.date().isoformat() != self.trade_date:
            raise ValueError("trade_date must be strict YYYY-MM-DD")
        if isinstance(self.event_time_ms, bool) or self.event_time_ms <= 0:
            raise ValueError("event_time_ms must be a positive epoch millisecond")
        symbol = _strict_symbol(self.symbol)
        market = str(self.market_code or "").lower()
        for field_name in _INT_FIELDS:
            value = getattr(self, field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise TypeError("%s must be an integer or None" % field_name)
        for field_name in _ARRAY_FIELDS:
            values = tuple(getattr(self, field_name))
            if len(values) != 5:
                raise ValueError("%s must contain five levels" % field_name)
            if any(value is not None and (isinstance(value, bool) or not isinstance(value, int)) for value in values):
                raise TypeError("%s values must be integers or None" % field_name)
            object.__setattr__(self, field_name, values)
        for field_name in ("no_price_limit", "is_st"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise TypeError("%s must be bool or None" % field_name)
        supplied = dict(self.field_meta)
        normalized_meta = []
        for field_name in _TICK_VALUE_FIELDS:
            value = getattr(self, field_name)
            meta = _coerce_field_meta(supplied.get(field_name), field_name, value)
            _validate_quality(field_name, value, meta)
            normalized_meta.append((field_name, meta))
        unknown_meta = set(supplied) - set(_TICK_VALUE_FIELDS)
        if unknown_meta:
            raise ValueError("unknown field metadata: %s" % sorted(unknown_meta))
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "market_code", market)
        object.__setattr__(self, "field_meta", tuple(normalized_meta))
        value_hash = _hash("canonical_value", self._value_payload())
        semantic_hash = _hash("canonical_semantic", (self._value_payload(), self._meta_payload()))
        object.__setattr__(self, "canonical_value_hash", value_hash)
        object.__setattr__(self, "canonical_semantic_hash", semantic_hash)

    def _value_payload(self) -> tuple[Any, ...]:
        return (
            CANONICAL_TICK_CONTRACT_VERSION,
            self.schema_version,
            self.trade_date,
            self.event_time_ms,
            self.symbol,
            self.market_code,
            tuple((name, getattr(self, name)) for name in _TICK_VALUE_FIELDS),
        )

    def _meta_payload(self) -> tuple[Any, ...]:
        return tuple(
            (name, meta.quality.value, meta.provenance.value, meta.invalid_reason)
            for name, meta in self.field_meta
        )

    @property
    def canonical_content_hash(self) -> str:
        return self.canonical_semantic_hash

    @property
    def field_meta_map(self) -> Mapping[str, FieldMetaV1]:
        return dict(self.field_meta)

    def to_tdevent(self) -> TDEventV1:
        """Project verified legacy fields without replacing the old oracle."""

        required = ("px_milli", "pc_milli", "amt_yuan")
        if any(getattr(self, name) is None for name in required):
            raise ValueError("legacy TDEventV1 projection requires price, pre-close and amount")
        return TDEventV1.from_mapping({
            "ts": self.event_time_ms,
            "symbol": self.symbol,
            "px_milli": self.px_milli,
            "pc_milli": self.pc_milli,
            "amt_yuan": self.amt_yuan,
            "vol_units": self.vol_units,
            "market_code": self.market_code,
            "ap_milli": self.ap_milli,
            "bp_milli": self.bp_milli,
            "av": self.av,
            "bv": self.bv,
            "inst_vol": self.inst_vol,
            "inst_amt_yuan": self.inst_amt_yuan,
            "large_net_yuan": self.large_net_yuan,
        })


def _field_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _field_with_presence(row: Mapping[str, Any], *names: str) -> tuple[bool, Any]:
    for name in names:
        if name in row:
            return True, row[name]
    return False, None


def _td_number(
    raw: Any,
    *,
    present: bool,
    field_name: str,
    price: bool = False,
    amount: bool = False,
    already_canonical: bool = False,
) -> tuple[Any, FieldMetaV1]:
    provenance = _default_provenance(field_name)
    if not present or raw is None:
        return None, FieldMetaV1(FieldQuality.MISSING, provenance)
    if isinstance(raw, bool):
        return None, FieldMetaV1(FieldQuality.INVALID, provenance, "BOOLEAN_NUMERIC")
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        return None, FieldMetaV1(FieldQuality.INVALID, provenance, "NOT_NUMERIC")
    if not math.isfinite(numeric):
        return None, FieldMetaV1(FieldQuality.INVALID, provenance, "NON_FINITE")
    try:
        if already_canonical:
            if not float(numeric).is_integer():
                raise ValueError
            value = int(numeric)
        elif price:
            # C++ RawTickConverter::price_to_milli returns zero for finite
            # non-positive prices and llround(price * 1000) otherwise.
            value = 0 if numeric <= 0 else cxx_llround(numeric * 1000.0)
            if value < -(2**31) or value > 2**31 - 1:
                raise OverflowError
        elif amount:
            value = 0 if numeric <= 0 else cxx_llround(numeric)
        elif isinstance(raw, int) and not isinstance(raw, bool):
            value = raw
        else:
            value = cxx_llround(numeric)
    except (OverflowError, ValueError):
        return None, FieldMetaV1(FieldQuality.INVALID, provenance, "OUT_OF_RANGE")
    return value, FieldMetaV1(FieldQuality.PRESENT_VALUE, provenance)


def _row_tick(row: Mapping[str, Any], trade_date: str, *, source: str) -> tuple[MarketTickV1, Optional[str]]:
    if not isinstance(row, Mapping):
        raise TypeError("source row must be a mapping")
    raw_symbol = _field_value(row, "symbol", "s")
    symbol = normalize_td_symbol(raw_symbol) if source == "td" else _strict_symbol(raw_symbol)
    ts = _field_value(row, "event_time_ms", "ts_ms", "ts", "tss")
    if isinstance(ts, datetime):
        if ts.tzinfo is None or ts.utcoffset() is None:
            ts = ts.replace(tzinfo=SHANGHAI)
        ts = int(ts.astimezone(timezone.utc).timestamp() * 1000)
    elif isinstance(ts, str):
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=SHANGHAI)
        ts = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    else:
        ts = int(ts)
    values: dict[str, Any] = {}
    metadata: dict[str, FieldMetaV1] = {}
    scalar_aliases = {
        "px_milli": ("px_milli", "lp"), "pc_milli": ("pc_milli", "lc"),
        "o_milli": ("o_milli", "o"), "h_milli": ("h_milli", "h"),
        "l_milli": ("l_milli", "l"), "amt_yuan": ("amt_yuan", "a"),
        "vol_units": ("vol_units", "v"), "inst_vol": ("inst_vol",),
        "inst_amt_yuan": ("inst_amt_yuan", "inst_amt"),
        "large_net_yuan": ("large_net_yuan", "large_net"),
        "limit_up_milli": ("limit_up_milli", "limit_up"),
        "limit_down_milli": ("limit_down_milli", "limit_down"),
        "limit_band_bp": ("limit_band_bp", "limit_ratio_bp"),
        "no_price_limit": ("no_price_limit",), "is_st": ("is_st", "st_flag"),
    }
    for field_name, names in scalar_aliases.items():
        present, raw = _field_with_presence(row, *names)
        if field_name in {"no_price_limit", "is_st"}:
            if not present or raw is None:
                values[field_name] = None
                metadata[field_name] = FieldMetaV1(FieldQuality.MISSING, _default_provenance(field_name))
            elif isinstance(raw, bool):
                values[field_name] = raw
                metadata[field_name] = FieldMetaV1(FieldQuality.PRESENT_VALUE, _default_provenance(field_name))
            else:
                values[field_name] = bool(int(raw))
                metadata[field_name] = FieldMetaV1(FieldQuality.PRESENT_VALUE, _default_provenance(field_name))
        else:
            value, meta = _td_number(
                raw,
                present=present,
                field_name=field_name,
                price=field_name in _PRICE_FIELDS,
                amount=field_name == "amt_yuan",
                already_canonical=field_name in row,
            )
            values[field_name] = value
            metadata[field_name] = meta
    for field_name in _ARRAY_FIELDS:
        values[field_name] = []
        element_meta: list[FieldMetaV1] = []
        prefix = field_name[:-6] if field_name.endswith("_milli") else field_name
        for index in range(1, 6):
            aliases = (
                "%s_milli%d" % (prefix, index),
                "%s%d_milli" % (prefix, index),
                "%s%d" % (prefix, index),
            )
            present, raw = _field_with_presence(row, *aliases)
            value, meta = _td_number(
                raw,
                present=present,
                field_name=field_name,
                price=field_name in {"ap_milli", "bp_milli"},
                already_canonical=any(name in row for name in aliases[:2]),
            )
            values[field_name].append(value)
            element_meta.append(meta)
        values[field_name] = tuple(values[field_name])
        if any(meta.quality is FieldQuality.INVALID for meta in element_meta):
            metadata[field_name] = FieldMetaV1(FieldQuality.INVALID, _default_provenance(field_name), "ARRAY_ELEMENT_INVALID")
        elif all(meta.quality is FieldQuality.MISSING for meta in element_meta):
            metadata[field_name] = FieldMetaV1(FieldQuality.MISSING, _default_provenance(field_name))
        elif any(meta.quality is FieldQuality.MISSING for meta in element_meta):
            metadata[field_name] = FieldMetaV1(FieldQuality.UNKNOWN, _default_provenance(field_name))
        else:
            metadata[field_name] = FieldMetaV1(FieldQuality.PRESENT_VALUE, _default_provenance(field_name))
    market = _effective_market(symbol, _field_value(row, "exchange"), _field_value(row, "market", "market_code"))
    stable_key = _field_value(row, "stable_td_row_key", "row_key", "source_row_key")
    return MarketTickV1(
        trade_date=trade_date,
        event_time_ms=ts,
        symbol=symbol,
        market_code=market,
        ap_milli=tuple(values["ap_milli"]),
        bp_milli=tuple(values["bp_milli"]),
        av=tuple(values["av"]),
        bv=tuple(values["bv"]),
        field_meta=tuple(metadata.items()),
        **{name: value for name, value in values.items() if name not in _ARRAY_FIELDS},
    ), stable_key


@dataclass(frozen=True)
class TickBatchV1:
    schema_version: int
    hash_encoding_version: str
    mode: str
    trade_date: str
    logical_ts_ms: int
    wall_ts_ms: Optional[int]
    seq_no: int
    source: str
    source_batch_id: str
    source_sequence: Optional[str]
    source_sequence_status: SequenceStatus
    arrival_order_status: ArrivalOrderStatus
    replay_order_status: ReplayOrderStatus
    historical_available_at_ms: Optional[int]
    historical_available_at_status: HistoricalAvailabilityStatus
    same_event_order_ambiguity: bool
    batch_quality: BatchQuality
    ticks: Tuple[MarketTickV1, ...]
    batch_content_hash: str = field(init=False)
    source_evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_sequence_status", SequenceStatus(self.source_sequence_status))
        object.__setattr__(self, "arrival_order_status", ArrivalOrderStatus(self.arrival_order_status))
        object.__setattr__(self, "replay_order_status", ReplayOrderStatus(self.replay_order_status))
        object.__setattr__(self, "historical_available_at_status", HistoricalAvailabilityStatus(self.historical_available_at_status))
        object.__setattr__(self, "batch_quality", BatchQuality(self.batch_quality))
        if not isinstance(self.trade_date, str):
            raise TypeError("trade_date must be a string")
        try:
            parsed_date = datetime.fromisoformat(self.trade_date)
        except ValueError as exc:
            raise ValueError("trade_date must be YYYY-MM-DD") from exc
        if parsed_date.date().isoformat() != self.trade_date:
            raise ValueError("trade_date must be strict YYYY-MM-DD")
        if not self.source_batch_id:
            raise ValueError("source_batch_id is required")
        if isinstance(self.seq_no, bool) or self.seq_no < 0:
            raise ValueError("seq_no must be non-negative")
        if self.logical_ts_ms <= 0:
            raise ValueError("logical_ts_ms must be positive")
        ticks = tuple(self.ticks)
        if any(tick.trade_date != self.trade_date for tick in ticks):
            raise ValueError("every tick.trade_date must equal batch.trade_date")
        if self.batch_quality is BatchQuality.EMPTY and ticks:
            raise ValueError("EMPTY batch cannot contain ticks")
        if not ticks and self.batch_quality not in {BatchQuality.EMPTY, BatchQuality.UNKNOWN}:
            raise ValueError("empty batch must be EMPTY or UNKNOWN")
        if ticks and self.batch_quality is BatchQuality.COMPLETE and self.same_event_order_ambiguity:
            raise ValueError("ambiguous order cannot be COMPLETE")
        object.__setattr__(self, "ticks", ticks)
        content = (
            CANONICAL_BATCH_CONTRACT_VERSION,
            self.schema_version,
            self.hash_encoding_version,
            tuple(tick.canonical_semantic_hash for tick in ticks),
        )
        object.__setattr__(self, "batch_content_hash", _hash("batch_content", content))
        evidence = (
            self.batch_content_hash,
            self.mode,
            self.trade_date,
            self.logical_ts_ms,
            self.source,
            self.source_batch_id,
            self.source_sequence,
            self.source_sequence_status.value,
            self.arrival_order_status.value,
            self.replay_order_status.value,
            self.historical_available_at_ms,
            self.historical_available_at_status.value,
            self.same_event_order_ambiguity,
            self.batch_quality.value,
        )
        object.__setattr__(self, "source_evidence_hash", _hash("source_evidence", evidence))

    def run_evidence_hash(self, *, run_id: str, wall_ts_ms: int) -> str:
        return _hash("run_evidence", (self.source_evidence_hash, self.seq_no, wall_ts_ms, run_id))


def _proto_present(record: Any, field_name: str) -> bool:
    """Use protobuf ListFields/HasField without importing protobuf."""

    try:
        fields = record.ListFields()
    except AttributeError:
        return field_name in record if isinstance(record, Mapping) else True
    for descriptor, _value in fields:
        if descriptor.name == field_name:
            return True
    return False


def _proto_declared(record: Any, field_name: str) -> bool:
    """Distinguish an absent schema field from a declared proto3 default."""

    if isinstance(record, Mapping):
        return field_name in record
    descriptor = getattr(record, "DESCRIPTOR", None)
    if descriptor is not None:
        return field_name in descriptor.fields_by_name
    return hasattr(record, field_name)


def _proto_get(record: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(field_name, default)
    return getattr(record, field_name, default)


class RabbitFixtureAdapter:
    """Adapt the authoritative parsed RabbitMQ ``DataBatch`` without I/O.

    The protobuf/Rabbit shape is the canonical source contract.  TD rows are
    compatibility input only and may not introduce fields or semantics that
    are not representable by this RawTick-shaped contract.
    """

    def build_batch(
        self,
        data_batch: Any,
        *,
        trade_date: str,
        logical_ts_ms: int,
        seq_no: int,
        mode: str = "LIVE",
        wall_ts_ms: Optional[int] = None,
    ) -> TickBatchV1:
        records = tuple(_proto_get(data_batch, "records", ()))
        ticks = []
        for record in records:
            ticks.append(self._record_to_tick(record, trade_date))
        batch_id = str(_proto_get(data_batch, "batch_id", ""))
        if not batch_id:
            raise ValueError("DataBatch.batch_id is required")
        return TickBatchV1(
            schema_version=1,
            hash_encoding_version=CANONICAL_HASH_ENCODING_VERSION,
            mode=mode,
            trade_date=trade_date,
            logical_ts_ms=logical_ts_ms,
            wall_ts_ms=wall_ts_ms,
            seq_no=seq_no,
            source="rabbit",
            source_batch_id=batch_id,
            source_sequence=None,
            source_sequence_status=SequenceStatus.UNKNOWN,
            arrival_order_status=ArrivalOrderStatus.UNKNOWN,
            replay_order_status=ReplayOrderStatus.UNKNOWN,
            historical_available_at_ms=None,
            historical_available_at_status=HistoricalAvailabilityStatus.UNKNOWN,
            same_event_order_ambiguity=False,
            batch_quality=BatchQuality.UNKNOWN if ticks else BatchQuality.EMPTY,
            ticks=tuple(ticks),
        )

    def _record_to_tick(self, record: Any, trade_date: str) -> MarketTickV1:
        symbol_value = _proto_get(record, "symbol", "")
        symbol = _strict_symbol(symbol_value)
        market = _effective_market(symbol, _proto_get(record, "exchange", ""), _proto_get(record, "market", ""))
        values: dict[str, Any] = {}
        quality: dict[str, FieldMetaV1] = {}

        def scalar(name: str, proto_name: str, *, kind: str, provenance: FieldProvenance) -> None:
            if not _proto_declared(record, proto_name):
                values[name] = None
                quality[name] = FieldMetaV1(FieldQuality.MISSING, provenance)
                return
            present = _proto_present(record, proto_name)
            raw = _proto_get(record, proto_name, 0 if kind != "bool" else False)
            if kind == "float":
                if not present and raw == 0:
                    values[name] = 0
                    quality[name] = FieldMetaV1(FieldQuality.WIRE_DEFAULT_AMBIGUOUS, provenance)
                else:
                    if not math.isfinite(float(raw)):
                        values[name] = None
                        quality[name] = FieldMetaV1(FieldQuality.INVALID, provenance, "NON_FINITE")
                    else:
                        try:
                            values[name] = cxx_llround(float(raw) * 1000.0) if name in _PRICE_FIELDS else cxx_llround(float(raw))
                        except (OverflowError, ValueError):
                            values[name] = None
                            quality[name] = FieldMetaV1(FieldQuality.INVALID, provenance, "OUT_OF_RANGE")
                        else:
                            quality[name] = FieldMetaV1(FieldQuality.PRESENT_VALUE, provenance)
            elif kind == "int":
                values[name] = int(raw)
                quality[name] = FieldMetaV1(FieldQuality.PRESENT_VALUE if present else FieldQuality.WIRE_DEFAULT_AMBIGUOUS, provenance)
            else:
                values[name] = bool(raw)
                quality[name] = FieldMetaV1(FieldQuality.PRESENT_VALUE if present else FieldQuality.WIRE_DEFAULT_AMBIGUOUS, provenance)

        for name, proto_name, kind, provenance in (
            ("px_milli", "lp", "float", FieldProvenance.SOURCE_SNAPSHOT),
            ("pc_milli", "lc", "float", FieldProvenance.SOURCE_SNAPSHOT),
            ("o_milli", "o", "float", FieldProvenance.SOURCE_SNAPSHOT),
            ("h_milli", "h", "float", FieldProvenance.SOURCE_SNAPSHOT),
            ("l_milli", "l", "float", FieldProvenance.SOURCE_SNAPSHOT),
            ("amt_yuan", "a", "float", FieldProvenance.SOURCE_SNAPSHOT),
            ("vol_units", "v", "int", FieldProvenance.SOURCE_SNAPSHOT),
        ):
            scalar(name, proto_name, kind=kind, provenance=provenance)
        for prefix, target, kind in (("ap", "ap_milli", "float"), ("bp", "bp_milli", "float"), ("av", "av", "int"), ("bv", "bv", "int")):
            values[target] = []
            for index in range(1, 6):
                field_name = "%s%d" % (prefix, index)
                if not _proto_declared(record, field_name):
                    values[target].append(None)
                    quality["%s[%d]" % (target, index - 1)] = FieldMetaV1(
                        FieldQuality.MISSING,
                        FieldProvenance.SOURCE_SNAPSHOT,
                    )
                    continue
                present = _proto_present(record, field_name)
                raw = _proto_get(record, field_name, 0)
                if kind == "float":
                    if not present and raw == 0:
                        value = 0
                        meta = FieldMetaV1(FieldQuality.WIRE_DEFAULT_AMBIGUOUS, FieldProvenance.SOURCE_SNAPSHOT)
                    elif not math.isfinite(float(raw)):
                        value = None
                        meta = FieldMetaV1(FieldQuality.INVALID, FieldProvenance.SOURCE_SNAPSHOT, "NON_FINITE")
                    else:
                        value = cxx_llround(float(raw) * 1000.0)
                        meta = FieldMetaV1(FieldQuality.PRESENT_VALUE, FieldProvenance.SOURCE_SNAPSHOT)
                else:
                    value = int(raw)
                    meta = FieldMetaV1(FieldQuality.PRESENT_VALUE if present else FieldQuality.WIRE_DEFAULT_AMBIGUOUS, FieldProvenance.SOURCE_SNAPSHOT)
                values[target].append(value)
                quality["%s[%d]" % (target, index - 1)] = meta
            values[target] = tuple(values[target])
        fields = (("inst_vol", "inst_vol", "int", FieldProvenance.DERIVED_DELTA),
                  ("inst_amt_yuan", "inst_amt_yuan", "int", FieldProvenance.DERIVED_DELTA),
                  ("large_net_yuan", "large_net_yuan", "int", FieldProvenance.DERIVED_DELTA),
                  ("limit_up_milli", "limit_up_milli", "float", FieldProvenance.SOURCE_SNAPSHOT),
                  ("limit_down_milli", "limit_down_milli", "float", FieldProvenance.SOURCE_SNAPSHOT),
                  ("limit_band_bp", "limit_band_bp", "int", FieldProvenance.SOURCE_SNAPSHOT),
                  ("no_price_limit", "no_price_limit", "bool", FieldProvenance.SOURCE_FLAG),
                  ("is_st", "is_st", "bool", FieldProvenance.SOURCE_FLAG))
        for name, proto_name, kind, provenance in fields:
            if kind == "float":
                scalar(name, proto_name, kind="float", provenance=provenance)
            else:
                scalar(name, proto_name, kind=kind, provenance=provenance)
        # Group-level metadata is canonicalized as one entry per public field;
        # array element presence remains part of the semantic value in fixture
        # diagnostics and does not create a second public hash type.
        group_quality = dict(quality)
        for name in _ARRAY_FIELDS:
            entries = [quality["%s[%d]" % (name, i)] for i in range(5)]
            if any(meta.quality is FieldQuality.INVALID for meta in entries):
                group_quality[name] = FieldMetaV1(FieldQuality.INVALID, FieldProvenance.SOURCE_SNAPSHOT, "ARRAY_ELEMENT_INVALID")
            elif any(meta.quality is FieldQuality.WIRE_DEFAULT_AMBIGUOUS for meta in entries):
                group_quality[name] = FieldMetaV1(FieldQuality.WIRE_DEFAULT_AMBIGUOUS, FieldProvenance.SOURCE_SNAPSHOT)
            elif all(meta.quality is FieldQuality.MISSING for meta in entries):
                group_quality[name] = FieldMetaV1(FieldQuality.MISSING, FieldProvenance.SOURCE_SNAPSHOT)
            else:
                group_quality[name] = FieldMetaV1(FieldQuality.PRESENT_VALUE, FieldProvenance.SOURCE_SNAPSHOT)
        event_time_ms = int(_proto_get(record, "tss", 0))
        event_local_date = datetime.fromtimestamp(
            event_time_ms / 1000.0,
            timezone.utc,
        ).astimezone(SHANGHAI).date().isoformat()
        if event_local_date != trade_date:
            raise ValueError("Rabbit tick event date does not match trade_date")
        return MarketTickV1(
            trade_date=trade_date,
            event_time_ms=event_time_ms,
            symbol=symbol,
            market_code=market,
            field_meta=tuple((name, group_quality[name]) for name in _TICK_VALUE_FIELDS),
            **values,
        )


class TDFrameAdapter:
    """Pure adapter from one already-read TD frame to ``TickBatchV1``."""

    def build_batch(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        trade_date: str,
        frame_start_ms: int,
        frame_end_ms: int,
        seq_no: int,
        mode: str = "REPLAY",
        wall_ts_ms: Optional[int] = None,
        query_succeeded: bool = True,
        completeness_proven: bool = False,
        known_partial: bool = False,
    ) -> TickBatchV1:
        if frame_end_ms <= frame_start_ms:
            raise ValueError("TD frame must be a positive half-open interval")
        normalized: list[tuple[MarketTickV1, Optional[str]]] = []
        for row in rows:
            tick, stable_key = _row_tick(row, trade_date, source="td")
            if tick.event_time_ms < frame_start_ms or tick.event_time_ms >= frame_end_ms:
                raise ValueError("TD tick is outside the half-open frame")
            local_date = datetime.fromtimestamp(tick.event_time_ms / 1000.0, timezone.utc).astimezone(SHANGHAI).date().isoformat()
            if local_date != trade_date:
                raise ValueError("TD tick event date does not match trade_date")
            normalized.append((tick, None if stable_key is None else str(stable_key)))
        normalized.sort(key=lambda item: (
            item[0].event_time_ms,
            item[0].symbol,
            0 if item[1] is not None else 1,
            item[1] or "",
            item[0].canonical_semantic_hash,
        ))
        ambiguity = any(
            len({tick.canonical_semantic_hash for tick, _ in group}) > 1 and any(key is None for _, key in group)
            for group in _group_by_time_symbol(normalized)
        )
        ticks = tuple(tick for tick, _ in normalized)
        if not ticks:
            quality = BatchQuality.EMPTY if query_succeeded else BatchQuality.UNKNOWN
        elif known_partial:
            quality = BatchQuality.PARTIAL
        elif completeness_proven and not ambiguity:
            quality = BatchQuality.COMPLETE
        else:
            quality = BatchQuality.UNKNOWN
        return TickBatchV1(
            schema_version=1,
            hash_encoding_version=CANONICAL_HASH_ENCODING_VERSION,
            mode=mode,
            trade_date=trade_date,
            logical_ts_ms=frame_end_ms,
            wall_ts_ms=wall_ts_ms,
            seq_no=seq_no,
            source="td",
            source_batch_id="td-replay:%s:%d:%d" % (trade_date, frame_start_ms, frame_end_ms),
            source_sequence=None,
            source_sequence_status=SequenceStatus.UNKNOWN,
            arrival_order_status=ArrivalOrderStatus.UNKNOWN,
            replay_order_status=ReplayOrderStatus.SYNTHETIC_DETERMINISTIC,
            historical_available_at_ms=None,
            historical_available_at_status=HistoricalAvailabilityStatus.UNKNOWN,
            same_event_order_ambiguity=ambiguity,
            batch_quality=quality,
            ticks=ticks,
        )


def _group_by_time_symbol(items: Sequence[tuple[MarketTickV1, Optional[str]]]) -> Iterable[Sequence[tuple[MarketTickV1, Optional[str]]]]:
    groups: dict[tuple[int, str], list[tuple[MarketTickV1, Optional[str]]]] = defaultdict(list)
    for item in items:
        groups[(item[0].event_time_ms, item[0].symbol)].append(item)
    return groups.values()


def compare_ticks(left: MarketTickV1, right: MarketTickV1) -> ParityResult:
    if left.canonical_value_hash != right.canonical_value_hash:
        return ParityResult.VALUE_MISMATCH
    if left.canonical_semantic_hash == right.canonical_semantic_hash:
        return ParityResult.STRICT_EQUAL
    return ParityResult.VALUE_EQUAL_QUALITY_DIFFERENT


def compare_batches(left: TickBatchV1, right: TickBatchV1, *, equivalent_boundary: bool = False) -> ParityResult:
    if not equivalent_boundary:
        return ParityResult.NOT_COMPARABLE
    left_values = Counter(tick.canonical_value_hash for tick in left.ticks)
    right_values = Counter(tick.canonical_value_hash for tick in right.ticks)
    if left_values != right_values:
        return ParityResult.VALUE_MISMATCH
    if left.same_event_order_ambiguity or right.same_event_order_ambiguity:
        return ParityResult.ORDER_AMBIGUOUS
    left_semantic = Counter(tick.canonical_semantic_hash for tick in left.ticks)
    right_semantic = Counter(tick.canonical_semantic_hash for tick in right.ticks)
    if left_semantic != right_semantic:
        return ParityResult.VALUE_EQUAL_QUALITY_DIFFERENT
    if tuple(tick.canonical_semantic_hash for tick in left.ticks) != tuple(
        tick.canonical_semantic_hash for tick in right.ticks
    ):
        return ParityResult.ORDER_AMBIGUOUS
    if left.batch_content_hash == right.batch_content_hash:
        return ParityResult.STRICT_EQUAL
    return ParityResult.VALUE_EQUAL_QUALITY_DIFFERENT


def classify_legacy_compatibility(old_events: Sequence[TDEventV1], new_ticks: Sequence[MarketTickV1], *, order_ambiguous: bool = False) -> CompatibilityResult:
    def legacy_signature(event: TDEventV1) -> tuple[Any, ...]:
        return (
            event.event_time_ms,
            event.symbol,
            event.price_milli,
            event.pre_close_milli,
            event.amount_yuan,
            event.volume_units,
        )

    old_values = Counter(legacy_signature(event) for event in old_events)
    new_values = Counter(
        legacy_signature(tick.to_tdevent())
        for tick in new_ticks
    )
    if old_values != new_values:
        return CompatibilityResult.VALUE_MISMATCH
    if order_ambiguous:
        return CompatibilityResult.ORDER_AMBIGUOUS
    if tuple(legacy_signature(event) for event in old_events) != tuple(
        legacy_signature(tick.to_tdevent()) for tick in new_ticks
    ):
        return CompatibilityResult.ORDER_AMBIGUOUS
    return CompatibilityResult.STRICT_COMPATIBLE


__all__ = [
    "CANONICAL_TICK_CONTRACT_VERSION", "CANONICAL_BATCH_CONTRACT_VERSION",
    "CANONICAL_HASH_ENCODING_VERSION", "CANONICAL_SOURCE_AUTHORITY",
    "FieldQuality", "FieldProvenance",
    "FieldMetaV1", "BatchQuality", "SequenceStatus", "ArrivalOrderStatus",
    "ReplayOrderStatus", "HistoricalAvailabilityStatus", "ParityResult",
    "CompatibilityResult", "MarketTickV1", "TickBatchV1", "TDFrameAdapter",
    "RabbitFixtureAdapter", "compare_ticks", "compare_batches",
    "classify_legacy_compatibility", "cxx_llround", "normalize_td_symbol",
]
