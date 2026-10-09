"""Soft-deadline auction facts and versioned late-correction timeline."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .clock import local_datetime_ms
from .contracts import deep_freeze, evidence_hash, semantic_hash
from .q2 import normalize_symbol


AUCTION_TIMING_POLICY_CONTRACT_VERSION = "AuctionTimingPolicyV1"
AUCTION_ANCHOR_FACT_CONTRACT_VERSION = "AuctionAnchorFactV1"
AUCTION_ANCHOR_REVISION_CONTRACT_VERSION = "AuctionAnchorRevisionV3"
AUCTION_TIMELINE_CONTRACT_VERSION = "AuctionTimelineV3"

OBSERVING = "OBSERVING"
READY = "READY"
PARTIAL = "PARTIAL"
MISSING = "MISSING"
FACT_ONLY = "FACT_ONLY"
AVAILABLE = "AVAILABLE"
UNKNOWN = "UNKNOWN"
INVALID = "INVALID"

_ANCHOR_PRICE_FIELDS = {
    "0920": "auction_anchor_0920_price_milli",
    "0924": "auction_anchor_0924_price_milli",
    "0925": "auction_anchor_0925_price_milli",
}
_ANCHOR_RAW_FIELDS = {"0920": "a20", "0924": "a24", "0925": "a25"}


def _whole_second_ms(value: int) -> int:
    """Drop subsecond precision for business-time comparisons."""

    return (value // 1_000) * 1_000


@dataclass(frozen=True)
class AuctionTimingPolicyV1:
    """Versioned business-anchor timing policy with adaptive grace."""

    tag: str
    business_time: str
    first_observable_time: str
    preferred_finalize_time: str
    soft_deadline_time: str
    policy_version: str = AUCTION_TIMING_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.tag not in {"0920", "0924", "0925"}:
            raise ValueError("unsupported auction tag")
        for value in (
            self.business_time,
            self.first_observable_time,
            self.preferred_finalize_time,
            self.soft_deadline_time,
        ):
            if not isinstance(value, str):
                raise TypeError("auction timing values must be strings in HH:MM:SS form")
            try:
                parsed = datetime.strptime(value, "%H:%M:%S")
            except ValueError as exc:
                raise ValueError("auction timing values must be valid HH:MM:SS") from exc
            if parsed.strftime("%H:%M:%S") != value:
                raise ValueError("auction timing values must be valid HH:MM:SS")
        if not (
            self.business_time <= self.first_observable_time
            <= self.preferred_finalize_time <= self.soft_deadline_time
        ):
            raise ValueError("auction timing must be monotonic")

    @classmethod
    def default(cls, tag: str) -> "AuctionTimingPolicyV1":
        values = {
            "0920": ("09:20:00", "09:20:03", "09:20:03", "09:20:30"),
            "0924": ("09:24:00", "09:24:10", "09:24:10", "09:24:30"),
            "0925": ("09:25:00", "09:25:06", "09:25:10", "09:25:30"),
        }
        try:
            business, first, preferred, soft = values[tag]
        except KeyError as exc:
            raise ValueError("unsupported auction tag") from exc
        return cls(tag, business, first, preferred, soft)

    @classmethod
    def for_tag(cls, tag: str) -> "AuctionTimingPolicyV1":
        return cls.default(tag)

    def at(self, trade_date: str) -> Mapping[str, int]:
        return {
            "business_anchor_ms": local_datetime_ms(trade_date, self.business_time),
            "first_observable_ms": local_datetime_ms(trade_date, self.first_observable_time),
            "preferred_finalize_ms": local_datetime_ms(trade_date, self.preferred_finalize_time),
            "soft_deadline_ms": local_datetime_ms(trade_date, self.soft_deadline_time),
        }

    def timestamps(self, trade_date: str) -> Mapping[str, int]:
        return self.at(trade_date)


@dataclass(frozen=True)
class AuctionAnchorFactV1:
    """One symbol's standalone, captured auction anchor fact.

    This fact is intentionally independent of adjacent-anchor comparisons.
    A missing 09:20/09:24 anchor therefore cannot suppress an available 09:25
    anchor. Historical availability remains explicit and is never inferred
    from event/source time.
    """

    trade_date: str
    tag: str
    symbol: str
    business_anchor_ms: int
    status: str
    price_milli: Optional[int]
    reason_code: Optional[str]
    source_time_ms: Optional[int]
    source_layer: str
    observed_at_ms: Optional[int]
    evaluation_time_ms: int
    freeze_time_ms: Optional[int]
    late_execution: bool
    historical_available_at_ms: Optional[int] = None
    historical_available_at_status: str = "UNKNOWN"
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.tag not in _ANCHOR_PRICE_FIELDS:
            raise ValueError("unsupported auction anchor tag")
        policy_times = AuctionTimingPolicyV1.default(self.tag).at(self.trade_date)
        if self.business_anchor_ms != policy_times["business_anchor_ms"]:
            raise ValueError("business_anchor_ms does not match the tag policy")
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if self.status not in {AVAILABLE, MISSING, UNKNOWN, INVALID}:
            raise ValueError("unsupported standalone anchor fact status")
        if self.status == AVAILABLE:
            if type(self.price_milli) is not int or self.price_milli <= 0:
                raise ValueError("AVAILABLE anchor fact requires a positive integer price")
            if self.reason_code is not None:
                raise ValueError("AVAILABLE anchor fact cannot have a reason_code")
        elif self.price_milli is not None:
            raise ValueError("non-available anchor facts must have a null price")
        if self.status == INVALID and not self.reason_code:
            raise ValueError("INVALID anchor fact requires a reason_code")
        if not isinstance(self.source_layer, str) or not self.source_layer:
            raise ValueError("source_layer is required")
        if self.evaluation_time_ms <= 0:
            raise ValueError("evaluation_time_ms must be positive")
        for name, value in (
            ("source_time_ms", self.source_time_ms),
            ("observed_at_ms", self.observed_at_ms),
            ("freeze_time_ms", self.freeze_time_ms),
            ("historical_available_at_ms", self.historical_available_at_ms),
        ):
            if value is not None and type(value) is not int:
                raise TypeError("%s must be an integer or None" % name)
        if self.historical_available_at_status not in {"KNOWN", "UNKNOWN"}:
            raise ValueError("historical_available_at_status must be KNOWN or UNKNOWN")
        if self.historical_available_at_status == "KNOWN" and self.historical_available_at_ms is None:
            raise ValueError("KNOWN historical availability requires its timestamp")
        if self.historical_available_at_status == "UNKNOWN" and self.historical_available_at_ms is not None:
            raise ValueError("UNKNOWN historical availability cannot carry a timestamp")
        object.__setattr__(self, "content_hash", semantic_hash({
            "contract": AUCTION_ANCHOR_FACT_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "tag": self.tag,
            "symbol": self.symbol,
            "business_anchor_ms": self.business_anchor_ms,
            "status": self.status,
            "price_milli": self.price_milli,
            "reason_code": self.reason_code,
        }))
        object.__setattr__(self, "evidence_hash", evidence_hash({
            "content_hash": self.content_hash,
            "source_time_ms": self.source_time_ms,
            "source_layer": self.source_layer,
            "observed_at_ms": self.observed_at_ms,
            "evaluation_time_ms": self.evaluation_time_ms,
            "freeze_time_ms": self.freeze_time_ms,
            "late_execution": self.late_execution,
            "historical_available_at_ms": self.historical_available_at_ms,
            "historical_available_at_status": self.historical_available_at_status,
        }))

    def as_trace(self) -> Mapping[str, Any]:
        return {
            "contract": AUCTION_ANCHOR_FACT_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "tag": self.tag,
            "symbol": self.symbol,
            "business_anchor_ms": self.business_anchor_ms,
            "status": self.status,
            "price_milli": self.price_milli,
            "reason_code": self.reason_code,
            "source_time_ms": self.source_time_ms,
            "source_layer": self.source_layer,
            "observed_at_ms": self.observed_at_ms,
            "evaluation_time_ms": self.evaluation_time_ms,
            "freeze_time_ms": self.freeze_time_ms,
            "late_execution": self.late_execution,
            "historical_available_at_ms": self.historical_available_at_ms,
            "historical_available_at_status": self.historical_available_at_status,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
        }


def build_auction_anchor_fact_v1(
    *,
    trade_date: str,
    tag: str,
    symbol: str,
    row: Optional[Mapping[str, Any]],
    evaluation_time_ms: int,
    source_layer: str,
    freeze_time_ms: Optional[int] = None,
    observed_at_ms: Optional[int] = None,
) -> AuctionAnchorFactV1:
    """Build a standalone per-symbol anchor from an already-observed Q2 row."""

    try:
        field_name = _ANCHOR_PRICE_FIELDS[tag]
    except KeyError as exc:
        raise ValueError("unsupported auction anchor tag") from exc
    policy_times = AuctionTimingPolicyV1.default(tag).at(trade_date)
    if row is None:
        status, price, reason = UNKNOWN, None, "SYMBOL_STATE_NOT_OBSERVED"
        source_time = None
    else:
        if not isinstance(row, Mapping):
            raise TypeError("anchor source row must be a mapping or None")
        row_symbol = row.get("symbol")
        if row_symbol is not None and normalize_symbol(row_symbol) != normalize_symbol(symbol):
            raise ValueError("row symbol does not match requested symbol")
        raw_price = row.get(field_name)
        quality_map = row.get("auction_anchor_field_quality")
        raw_fields = row.get("raw_fields")
        raw_field_name = _ANCHOR_RAW_FIELDS[tag]
        raw_present = isinstance(raw_fields, Mapping) and raw_field_name in raw_fields
        raw_wire_value = raw_fields.get(raw_field_name) if raw_present else None
        field_errors = row.get("field_errors", ()) or ()
        declared_quality = (
            quality_map.get(raw_field_name)
            if isinstance(quality_map, Mapping)
            else None
        )
        if declared_quality == "PRESENT_VALUE":
            if type(raw_price) is int and raw_price > 0:
                status, price, reason = AVAILABLE, raw_price, None
            else:
                status, price, reason = INVALID, None, "ANCHOR_QUALITY_VALUE_MISMATCH"
        elif declared_quality == "MISSING":
            if raw_price is None or raw_price == 0:
                status, price, reason = MISSING, None, "ANCHOR_FIELD_UNAVAILABLE"
            else:
                status, price, reason = INVALID, None, "ANCHOR_QUALITY_VALUE_MISMATCH"
        elif declared_quality == "UNKNOWN":
            if raw_price is None:
                status, price, reason = UNKNOWN, None, "ANCHOR_QUALITY_UNKNOWN"
            else:
                status, price, reason = INVALID, None, "ANCHOR_QUALITY_VALUE_MISMATCH"
        elif declared_quality == "INVALID":
            status, price, reason = INVALID, None, "ANCHOR_SOURCE_VALUE_INVALID"
        elif declared_quality is not None:
            status, price, reason = INVALID, None, "ANCHOR_QUALITY_UNSUPPORTED"
        elif raw_price is None:
            if raw_field_name + "_non_positive" in field_errors:
                status, price, reason = INVALID, None, "ANCHOR_PRICE_NON_POSITIVE"
            elif not raw_present:
                status, price, reason = UNKNOWN, None, "ANCHOR_RAW_FIELD_NOT_PRESENT"
            elif type(raw_wire_value) is int and raw_wire_value == 0:
                status, price, reason = MISSING, None, "ANCHOR_ZERO_UNAVAILABLE"
            elif isinstance(raw_wire_value, str) and raw_wire_value.strip() == "0":
                status, price, reason = MISSING, None, "ANCHOR_ZERO_UNAVAILABLE"
            elif raw_wire_value is None:
                status, price, reason = UNKNOWN, None, "ANCHOR_RAW_VALUE_NULL"
            else:
                status, price, reason = INVALID, None, "CANONICAL_ANCHOR_VALUE_MISMATCH"
        elif type(raw_price) is not int:
            status, price, reason = INVALID, None, "ANCHOR_PRICE_NOT_INTEGER"
        elif raw_price < 0:
            status, price, reason = INVALID, None, "ANCHOR_PRICE_NEGATIVE"
        elif raw_price == 0:
            status, price, reason = MISSING, None, "ANCHOR_ZERO_UNAVAILABLE"
        else:
            if raw_present:
                try:
                    parsed_raw = int(raw_wire_value)
                except (TypeError, ValueError):
                    parsed_raw = None
                if parsed_raw != raw_price:
                    status, price, reason = INVALID, None, "CANONICAL_ANCHOR_VALUE_MISMATCH"
                else:
                    status, price, reason = AVAILABLE, raw_price, None
            else:
                status, price, reason = AVAILABLE, raw_price, None
        raw_source_time = row.get("source_record_time_ms")
        source_time = raw_source_time if type(raw_source_time) is int else None
    return AuctionAnchorFactV1(
        trade_date=trade_date,
        tag=tag,
        symbol=symbol,
        business_anchor_ms=policy_times["business_anchor_ms"],
        status=status,
        price_milli=price,
        reason_code=reason,
        source_time_ms=source_time,
        source_layer=source_layer,
        observed_at_ms=observed_at_ms,
        evaluation_time_ms=evaluation_time_ms,
        freeze_time_ms=freeze_time_ms,
        late_execution=(
            _whole_second_ms(evaluation_time_ms)
            > _whole_second_ms(policy_times["preferred_finalize_ms"])
        ),
    )


@dataclass(frozen=True)
class AuctionAnchorRevisionV3:
    """Versioned auction-anchor content plus separate observation evidence.

    Revision identity is scoped to this tag's per-symbol anchor value and
    quality. Unrelated Q2 quote changes may advance evidence time, but do not
    create a new auction-anchor content revision. Source/evaluation metadata
    remains available in ``evidence_hash``.
    """
    trade_date: str
    tag: str
    revision: int
    business_anchor_ms: int
    first_observable_ms: int
    preferred_finalize_ms: int
    soft_deadline_ms: int
    state: str
    expected_symbols: Tuple[str, ...]
    available_anchor_symbols: Tuple[str, ...]
    missing_anchor_symbols: Tuple[str, ...]
    anchor_coverage: Optional[float]
    source_observed_symbols: Tuple[str, ...]
    source_missing_symbols: Tuple[str, ...]
    source_coverage: Optional[float]
    source_layers: Tuple[str, ...]
    observed_at_ms: Optional[int]
    evaluation_time_ms: int
    freeze_time_ms: Optional[int]
    source_time_min_ms: Optional[int]
    source_time_max_ms: Optional[int]
    late_execution: bool
    supersedes_revision: Optional[int] = None
    recovery_state: str = "NOT_REQUESTED"
    # Hashes only this anchor's per-symbol value/quality, not unrelated Q2
    # quote fields. Source/evaluation times remain in evidence_hash.
    observations_hash: str = ""
    invalid_source_symbols: Tuple[str, ...] = ()
    source_anomaly_count: int = 0
    source_anomaly_codes: Tuple[str, ...] = ()
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.tag not in {"0920", "0924", "0925"}:
            raise ValueError("unsupported auction tag")
        if self.revision <= 0 or self.evaluation_time_ms <= 0:
            raise ValueError("revision and evaluation_time_ms must be positive")
        if self.state not in {OBSERVING, READY, PARTIAL, MISSING}:
            raise ValueError("unsupported auction revision state")
        expected = tuple(sorted({normalize_symbol(item) for item in self.expected_symbols}))
        available = tuple(
            sorted({normalize_symbol(item) for item in self.available_anchor_symbols})
        )
        missing_anchor = tuple(
            sorted({normalize_symbol(item) for item in self.missing_anchor_symbols})
        )
        source_observed = tuple(
            sorted({normalize_symbol(item) for item in self.source_observed_symbols})
        )
        source_missing = tuple(
            sorted({normalize_symbol(item) for item in self.source_missing_symbols})
        )
        invalid_source = tuple(
            sorted({normalize_symbol(item) for item in self.invalid_source_symbols})
        )
        anomaly_codes = tuple(sorted(str(item) for item in self.source_anomaly_codes))
        if type(self.source_anomaly_count) is not int or self.source_anomaly_count < 0:
            raise ValueError("source_anomaly_count must be a non-negative integer")
        if self.source_anomaly_count != len(anomaly_codes):
            raise ValueError("source_anomaly_count must match source_anomaly_codes")
        if invalid_source and not anomaly_codes:
            raise ValueError("invalid_source_symbols require source anomaly diagnostics")
        for name, values in (
            ("available_anchor_symbols", available),
            ("missing_anchor_symbols", missing_anchor),
            ("source_observed_symbols", source_observed),
            ("source_missing_symbols", source_missing),
        ):
            if expected and set(values) - set(expected):
                raise ValueError("%s must be a subset of expected_symbols" % name)
        if set(available) & set(missing_anchor) or (
            expected and set(available) | set(missing_anchor) != set(expected)
        ):
            raise ValueError(
                "available/missing anchor symbols must partition expected_symbols"
            )
        if set(source_observed) & set(source_missing) or (
            expected and set(source_observed) | set(source_missing) != set(expected)
        ):
            raise ValueError(
                "source observed/missing symbols must partition expected_symbols"
            )
        for name, value in (
            ("anchor_coverage", self.anchor_coverage),
            ("source_coverage", self.source_coverage),
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError("%s must be between zero and one or unknown" % name)
        object.__setattr__(self, "expected_symbols", expected)
        object.__setattr__(self, "available_anchor_symbols", available)
        object.__setattr__(self, "missing_anchor_symbols", missing_anchor)
        object.__setattr__(self, "source_observed_symbols", source_observed)
        object.__setattr__(self, "source_missing_symbols", source_missing)
        object.__setattr__(self, "invalid_source_symbols", invalid_source)
        object.__setattr__(self, "source_anomaly_codes", anomaly_codes)
        object.__setattr__(self, "source_layers", tuple(self.source_layers))
        content = {
            "contract": AUCTION_ANCHOR_REVISION_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "tag": self.tag,
            "business_anchor_ms": self.business_anchor_ms,
            "expected_symbols": expected,
            "available_anchor_symbols": available,
            "missing_anchor_symbols": missing_anchor,
            "anchor_coverage": self.anchor_coverage,
            "source_observed_symbols": source_observed,
            "source_missing_symbols": source_missing,
            "source_coverage": self.source_coverage,
            "observations_hash": self.observations_hash,
        }
        if anomaly_codes:
            content.update({
                "invalid_source_symbols": invalid_source,
                "source_anomaly_count": self.source_anomaly_count,
                "source_anomaly_codes": anomaly_codes,
            })
        object.__setattr__(self, "content_hash", semantic_hash(content))
        object.__setattr__(self, "evidence_hash", evidence_hash({
            "revision": self.revision,
            "source_layers": self.source_layers,
            "observed_at_ms": self.observed_at_ms,
            "evaluation_time_ms": self.evaluation_time_ms,
            "freeze_time_ms": self.freeze_time_ms,
            "source_time_min_ms": self.source_time_min_ms,
            "source_time_max_ms": self.source_time_max_ms,
            "late_execution": self.late_execution,
            "supersedes_revision": self.supersedes_revision,
            "recovery_state": self.recovery_state,
        }))

    @property
    def observed_symbols(self) -> Tuple[str, ...]:
        """Compatibility alias: symbols with this tag's usable anchor field."""

        return self.available_anchor_symbols

    @property
    def missing_symbols(self) -> Tuple[str, ...]:
        """Compatibility alias: expected symbols without this tag's anchor."""

        return self.missing_anchor_symbols

    @property
    def coverage(self) -> Optional[float]:
        """Compatibility alias for anchor-field coverage, not row coverage."""

        return self.anchor_coverage

    @property
    def business_anchor(self) -> int:
        return self.business_anchor_ms

    @property
    def source_time_ms(self) -> Optional[int]:
        return self.source_time_max_ms


# Keep Python imports source-compatible while serialized evidence advances to
# the anchor-scoped content-hash contract.
AuctionAnchorRevisionV2 = AuctionAnchorRevisionV3
AuctionAnchorRevisionV1 = AuctionAnchorRevisionV3


def _normalize_observed_rows(
    rows: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Mapping[str, Any]], Tuple[str, ...], Tuple[str, ...]]:
    """Keep usable anchor rows while isolating malformed source members.

    A normalized-symbol collision is ambiguous, so all rows for that symbol
    are excluded rather than choosing whichever happened to arrive last.
    Other symbols remain usable; diagnostics are carried by the revision.
    """

    normalized: dict[str, Mapping[str, Any]] = {}
    invalid_symbols: set[str] = set()
    anomaly_codes: list[str] = []

    def add(symbol: str, values: Mapping[str, Any]) -> None:
        if symbol in invalid_symbols:
            anomaly_codes.append("DUPLICATE_NORMALIZED_SYMBOL")
            return
        if symbol in normalized:
            normalized.pop(symbol, None)
            invalid_symbols.add(symbol)
            anomaly_codes.append("DUPLICATE_NORMALIZED_SYMBOL")
            return
        normalized[symbol] = dict(values)

    if isinstance(rows, Mapping):
        for raw_symbol, values in rows.items():
            try:
                symbol = normalize_symbol(raw_symbol)
            except (TypeError, ValueError, UnicodeError):
                anomaly_codes.append("INVALID_SYMBOL")
                continue
            if not isinstance(values, Mapping):
                if symbol in normalized:
                    normalized.pop(symbol, None)
                    anomaly_codes.append("DUPLICATE_NORMALIZED_SYMBOL")
                invalid_symbols.add(symbol)
                anomaly_codes.append("ROW_NOT_MAPPING")
                continue
            add(symbol, values)
    else:
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Iterable):
            raise TypeError("auction rows must be a mapping or iterable of rows")
        for row in rows:
            if not isinstance(row, Mapping):
                anomaly_codes.append("ROW_NOT_MAPPING")
                continue
            raw_symbol = row.get("symbol")
            if not raw_symbol:
                anomaly_codes.append("MISSING_SYMBOL")
                continue
            try:
                symbol = normalize_symbol(raw_symbol)
            except (TypeError, ValueError, UnicodeError):
                anomaly_codes.append("INVALID_SYMBOL")
                continue
            add(symbol, row)

    return (
        normalized,
        tuple(sorted(invalid_symbols)),
        tuple(sorted(anomaly_codes)),
    )


def _rows_by_symbol(rows: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    if isinstance(rows, Mapping):
        return {normalize_symbol(symbol): dict(values) for symbol, values in rows.items()}
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("symbol"):
            raise ValueError("auction rows must contain symbol")
        result[normalize_symbol(row["symbol"])] = dict(row)
    return result


def build_auction_anchor_revision(
    trade_date: str,
    tag: str,
    rows: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    evaluation_time_ms: int,
    expected_symbols: Sequence[Any] = (),
    observed_at_ms: Optional[int] = None,
    source_layers: Sequence[str] = (),
    revision: int = 1,
    supersedes_revision: Optional[int] = None,
    recovery_state: str = "NOT_REQUESTED",
    policy: Optional[AuctionTimingPolicyV1] = None,
) -> AuctionAnchorRevisionV3:
    policy = policy or AuctionTimingPolicyV1.default(tag)
    times = policy.at(trade_date)
    by_symbol, invalid_symbols, anomaly_codes = _normalize_observed_rows(rows)
    expected_values: set[str] = set()
    invalid_expected_count = 0
    for item in expected_symbols:
        try:
            expected_values.add(normalize_symbol(item))
        except (TypeError, ValueError, UnicodeError):
            # An invalid member in the declared universe must not discard
            # valid observed anchors. Drop only that member and withhold
            # coverage/completeness claims for the affected revision.
            invalid_expected_count += 1
    expected = tuple(sorted(expected_values))
    expected_set = set(expected)
    source_observed = tuple(
        sorted(symbol for symbol in by_symbol if not expected or symbol in expected_set)
    )
    source_missing = (
        tuple(symbol for symbol in expected if symbol not in set(source_observed))
        if expected
        else ()
    )
    anchor_field = "auction_anchor_%s_price_milli" % tag
    anchor_values = {
        symbol: by_symbol[symbol].get(anchor_field)
        for symbol in source_observed
    }
    raw_anchor_field = _ANCHOR_RAW_FIELDS[tag]
    anchor_observations = {
        symbol: {
            "price_milli": anchor_values[symbol],
            "quality": (
                by_symbol[symbol].get("auction_anchor_field_quality", {}).get(
                    raw_anchor_field
                )
                if isinstance(
                    by_symbol[symbol].get("auction_anchor_field_quality"), Mapping
                )
                else None
            ),
        }
        for symbol in source_observed
    }
    anchor_available = tuple(
        sorted(
            symbol
            for symbol, value in anchor_values.items()
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        )
    )
    missing_anchor = (
        tuple(symbol for symbol in expected if symbol not in set(anchor_available))
        if expected
        else ()
    )
    anchor_coverage = (
        len(anchor_available) / float(len(expected))
        if expected and not invalid_expected_count
        else None
    )
    source_coverage = (
        len(source_observed) / float(len(expected))
        if expected and not invalid_expected_count
        else None
    )
    evaluation_second_ms = _whole_second_ms(evaluation_time_ms)
    if evaluation_second_ms < _whole_second_ms(times["first_observable_ms"]):
        state = OBSERVING
    elif not expected:
        # An unknown denominator cannot establish field completeness.
        state = PARTIAL
    elif invalid_expected_count:
        # The valid subset may be usable, but it cannot establish that the
        # declared source universe was complete.
        state = PARTIAL
    elif not anchor_available:
        state = (
            MISSING
            if evaluation_second_ms >= _whole_second_ms(times["soft_deadline_ms"])
            else PARTIAL
        )
    elif not missing_anchor and recovery_state != "APPLIED":
        state = READY
    else:
        state = PARTIAL
    # The production t1-v2 contract emits the 0925 snapshot at the first
    # observable barrier (09:25:06).  ``preferred_finalize_ms`` remains an
    # adaptive-grace reference for later observations; it is not a hard
    # prerequisite for the first immutable anchor.  Late cohorts create a
    # new content revision without moving the original freeze barrier.
    freeze = (
        times["first_observable_ms"]
        if evaluation_second_ms >= _whole_second_ms(times["first_observable_ms"])
        else None
    )
    event_times = []
    all_anomaly_codes = list(anomaly_codes)
    all_anomaly_codes.extend(
        "INVALID_EXPECTED_SYMBOL" for _ in range(invalid_expected_count)
    )
    for value in by_symbol.values():
        source_time = value.get("source_time_ms")
        if source_time is None:
            source_time = value.get("source_record_time_ms")
        if source_time is None:
            source_time = value.get("ts")
        if source_time is None:
            continue
        if type(source_time) is not int or source_time <= 0:
            # A bad per-symbol timestamp weakens time bounds, not the other
            # valid fields in that symbol's anchor row or the rest of the batch.
            all_anomaly_codes.append("INVALID_SOURCE_TIME")
            continue
        event_times.append(source_time)
    observations_hash = semantic_hash(anchor_observations)
    return AuctionAnchorRevisionV3(
        trade_date=trade_date,
        tag=tag,
        revision=revision,
        business_anchor_ms=times["business_anchor_ms"],
        first_observable_ms=times["first_observable_ms"],
        preferred_finalize_ms=times["preferred_finalize_ms"],
        soft_deadline_ms=times["soft_deadline_ms"],
        state=state,
        expected_symbols=expected,
        available_anchor_symbols=anchor_available,
        missing_anchor_symbols=missing_anchor,
        anchor_coverage=anchor_coverage,
        source_observed_symbols=source_observed,
        source_missing_symbols=source_missing,
        source_coverage=source_coverage,
        source_layers=tuple(source_layers),
        observed_at_ms=observed_at_ms,
        evaluation_time_ms=evaluation_time_ms,
        freeze_time_ms=freeze,
        source_time_min_ms=min(event_times) if event_times else None,
        source_time_max_ms=max(event_times) if event_times else None,
        late_execution=(
            evaluation_second_ms > _whole_second_ms(times["soft_deadline_ms"])
        ),
        supersedes_revision=supersedes_revision,
        recovery_state=recovery_state,
        observations_hash=observations_hash,
        invalid_source_symbols=invalid_symbols,
        source_anomaly_count=len(all_anomaly_codes),
        source_anomaly_codes=tuple(all_anomaly_codes),
    )


class AuctionTimeline:
    """In-memory version ledger for auction facts and late corrections."""

    def __init__(self, trade_date: str, policies: Optional[Mapping[str, AuctionTimingPolicyV1]] = None) -> None:
        self.trade_date = trade_date
        self.policies = dict(policies or {tag: AuctionTimingPolicyV1.default(tag) for tag in ("0920", "0924", "0925")})
        self._history: dict[str, list[AuctionAnchorRevisionV3]] = {tag: [] for tag in self.policies}
        self._anchor_values: dict[str, dict[str, Any]] = {tag: {} for tag in self.policies}
        self._source_rows: dict[str, Mapping[str, Mapping[str, Any]]] = {
            tag: {} for tag in self.policies
        }
        self._applied_recovery_results: dict[
            str, tuple[str, str, AuctionAnchorRevisionV3]
        ] = {}
        # ``_history`` is a content-revision ledger.  ``_latest`` also tracks
        # the newest observation/evaluation evidence for the current revision;
        # identical rows observed after a soft cutoff must advance timing
        # state without fabricating a new content revision.
        self._latest: dict[str, AuctionAnchorRevisionV3] = {}

    def observe(
        self,
        tag: str,
        rows: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
        *,
        evaluation_time_ms: int,
        expected_symbols: Sequence[Any] = (),
        observed_at_ms: Optional[int] = None,
        source_layers: Sequence[str] = (),
        recovery_state: str = "NOT_REQUESTED",
        source_anomaly_codes: Sequence[str] = (),
        invalid_source_symbols: Sequence[str] = (),
    ) -> AuctionAnchorRevisionV3:
        if tag not in self.policies:
            raise ValueError("unsupported auction tag")
        history = self._history.setdefault(tag, [])
        normalized_rows, invalid_symbols, anomaly_codes = _normalize_observed_rows(rows)
        candidate = build_auction_anchor_revision(
            self.trade_date,
            tag,
            normalized_rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=expected_symbols,
            observed_at_ms=observed_at_ms,
            source_layers=source_layers,
            revision=(history[-1].revision + 1 if history else 1),
            supersedes_revision=(history[-1].revision if history else None),
            recovery_state=recovery_state,
            policy=self.policies[tag],
        )
        external_invalid_symbols: set[str] = set()
        external_anomaly_codes = {str(item) for item in source_anomaly_codes if str(item)}
        for raw_symbol in invalid_source_symbols:
            try:
                external_invalid_symbols.add(normalize_symbol(raw_symbol))
            except (TypeError, ValueError, UnicodeError):
                external_anomaly_codes.add("INVALID_DIAGNOSTIC_SYMBOL")
        combined_anomaly_codes = tuple(sorted((
            *candidate.source_anomaly_codes,
            *anomaly_codes,
            *sorted(external_anomaly_codes),
        )))
        combined_invalid_symbols = tuple(sorted(
            set(candidate.invalid_source_symbols)
            | set(invalid_symbols)
            | external_invalid_symbols
        ))
        if combined_anomaly_codes or combined_invalid_symbols:
            candidate = replace(
                candidate,
                invalid_source_symbols=combined_invalid_symbols,
                source_anomaly_count=len(combined_anomaly_codes),
                source_anomaly_codes=combined_anomaly_codes,
            )
        self._source_rows[tag] = deep_freeze(normalized_rows)
        anchor_field = _ANCHOR_PRICE_FIELDS[tag]
        self._anchor_values[tag] = {
            symbol: values.get(anchor_field)
            for symbol, values in normalized_rows.items()
        }
        if history and history[-1].content_hash == candidate.content_hash:
            previous = history[-1]
            candidate = replace(
                candidate,
                revision=previous.revision,
                supersedes_revision=previous.supersedes_revision,
            )
            self._latest[tag] = candidate
            return candidate
        history.append(candidate)
        self._latest[tag] = candidate
        return candidate

    def apply_recovery_result(
        self,
        plan: Any,
        result: Any,
        *,
        evaluation_time_ms: int,
    ) -> AuctionAnchorRevisionV3:
        """Apply one plan-bound recovery result.

        Omitted primary members and fields are restored from the exact observed
        base cohort before validation. This safely tolerates a sparse provider
        response without treating omissions as deletions. A row conflicting
        with an observed fact is quarantined by symbol while valid sibling
        fills continue. Out-of-plan members/fields are quarantined at their
        smallest useful scope; stale plans and invalid identities remain hard
        errors. The return value is the timeline's current revision after the
        operation. A retry for an older idempotency key returns the newer
        current revision without reapplying old facts; the original result
        revision remains available in revision history.
        """

        from .recovery import (
            RECOVERY_APPLIED,
            RECOVERY_ERROR,
            RecoveryPlanV1,
            RecoveryResultV1,
        )

        if not isinstance(plan, RecoveryPlanV1):
            raise TypeError("plan must be RecoveryPlanV1")
        if not isinstance(result, RecoveryResultV1):
            raise TypeError("result must be RecoveryResultV1")
        if plan.trade_date != self.trade_date:
            raise ValueError("recovery plan trade_date does not match timeline")
        if result.plan_id != plan.plan_id or result.idempotency_key != plan.idempotency_key:
            raise ValueError("recovery result plan identity does not match request")
        if result.base_revision != plan.current_revision:
            raise ValueError("recovery result base_revision does not match request")
        if (
            result.resulting_revision is not None
            and result.resulting_revision != plan.current_revision + 1
        ):
            raise ValueError("recovery result resulting_revision does not match next revision")
        if result.recovery_state != RECOVERY_APPLIED:
            raise ValueError("only APPLIED recovery results can create a revision")

        current = self.latest(plan.tag)
        if current is None:
            raise ValueError("recovery result has no observed base revision")

        previously_applied = self._applied_recovery_results.get(result.idempotency_key)
        if previously_applied is not None:
            previous_hash, previous_tag, previous_revision = previously_applied
            if previous_hash != result.content_hash or previous_tag != plan.tag:
                raise ValueError("idempotency key was reused with a different recovery result")
            if current.revision != previous_revision.revision:
                # A newer content revision already exists. A retry of an old
                # result must not move the timeline backwards or make the old
                # result revision look like the current timeline state.
                return current
            primary_rows = self._source_rows.get(plan.tag, {})
            (
                merged_rows,
                candidate_symbols,
                candidate_fields,
                scope_anomalies,
                quarantined_symbols,
            ) = self._prepare_recovery_result_scope(
                plan, current, result, primary_rows
            )
            merged_rows, completion_anomalies = self._complete_recovery_rows(
                plan, merged_rows, primary_rows
            )
            conflicting_symbols = set(self._validate_recovery_source_rows(
                plan,
                merged_rows,
                allowed_symbols=candidate_symbols,
                allowed_fields=candidate_fields,
            ))
            for symbol in conflicting_symbols:
                merged_rows[symbol] = dict(primary_rows[symbol])
            if evaluation_time_ms < current.evaluation_time_ms:
                # Keep the latest evidence monotonic even when a delayed retry
                # arrives after a newer evaluation of the same revision.
                return current

            observed_at_ms = result.observed_at_ms
            if current.observed_at_ms is not None and (
                observed_at_ms is None or observed_at_ms < current.observed_at_ms
            ):
                observed_at_ms = current.observed_at_ms
            refreshed = self.apply_recovery(
                plan,
                merged_rows,
                evaluation_time_ms=evaluation_time_ms,
                observed_at_ms=observed_at_ms,
                source=result.source,
                # This is a retry of an already-applied idempotency key. The
                # current primary cohort includes the prior fill, so comparing
                # it as though it were the original base would incorrectly
                # turn a successful retry into ERROR.
                recovery_state=previous_revision.recovery_state,
                source_anomaly_codes=(
                    *result.source_anomaly_codes,
                    *scope_anomalies,
                    *completion_anomalies,
                    *(
                        ("RECOVERY_CONFLICTING_SOURCE_ROW_QUARANTINED",)
                        if conflicting_symbols
                        else ()
                    ),
                ),
                invalid_source_symbols=tuple(
                    sorted(
                        set(result.invalid_symbols)
                        | quarantined_symbols
                        | conflicting_symbols
                    )
                ),
            )
            if (
                refreshed.revision != previous_revision.revision
                or refreshed.content_hash != previous_revision.content_hash
            ):
                raise ValueError("idempotent recovery changed anchor content")
            self._applied_recovery_results[result.idempotency_key] = (
                result.content_hash,
                plan.tag,
                refreshed,
            )
            return refreshed
        if plan.current_revision != current.revision:
            raise ValueError("stale recovery plan; request against the current revision")

        primary_rows = self._source_rows.get(plan.tag, {})
        (
            merged_rows,
            candidate_symbols,
            candidate_fields,
            scope_anomalies,
            quarantined_symbols,
        ) = self._prepare_recovery_result_scope(
            plan, current, result, primary_rows
        )
        # A conflicting recovery duplicate is quarantined by the result
        # contract. Keep the previously observed primary row for that symbol
        # so one bad recovered member neither erases old facts nor blocks other
        # valid recovery fills.
        for symbol in result.invalid_symbols:
            if symbol in primary_rows:
                merged_rows[symbol] = dict(primary_rows[symbol])
        merged_rows, completion_anomalies = self._complete_recovery_rows(
            plan, merged_rows, primary_rows
        )
        dropped_source_symbols = set(current.source_observed_symbols) - set(merged_rows)
        if dropped_source_symbols:
            raise ValueError("merged recovery cohort dropped existing source symbols")
        conflicting_symbols = set(self._validate_recovery_source_rows(
            plan,
            merged_rows,
            allowed_symbols=candidate_symbols,
            allowed_fields=candidate_fields,
        ))
        for symbol in conflicting_symbols:
            merged_rows[symbol] = dict(primary_rows[symbol])
        accepted_filled_symbols, accepted_filled_fields = self._accepted_recovery_fills(
            candidate_symbols - conflicting_symbols,
            candidate_fields,
            merged_rows,
            primary_rows,
        )
        if conflicting_symbols:
            completion_anomalies = tuple(sorted({
                *completion_anomalies,
                "RECOVERY_CONFLICTING_SOURCE_ROW_QUARANTINED",
            }))
        for symbol in current.available_anchor_symbols:
            old_value = self._anchor_values.get(plan.tag, {}).get(symbol)
            new_row = merged_rows.get(symbol)
            if new_row is None:
                raise ValueError("merged recovery cohort dropped an existing anchor symbol")
            if new_row.get(_ANCHOR_PRICE_FIELDS[plan.tag]) != old_value:
                raise ValueError("merged recovery cohort overwrites an existing anchor")
        self._validate_recovery_source_rows(
            plan,
            merged_rows,
            allowed_symbols=accepted_filled_symbols,
            allowed_fields=accepted_filled_fields,
        )

        anchor_field = _ANCHOR_PRICE_FIELDS[plan.tag]
        old_anchor_values = self._anchor_values.get(plan.tag, {})
        recovery_scope_symbols = set(current.expected_symbols) or set(merged_rows)
        newly_available_symbols = {
            symbol
            for symbol in recovery_scope_symbols
            if self._is_recovered_field_value(
                anchor_field,
                merged_rows.get(symbol, {}).get(anchor_field),
            )
            and not self._is_recovered_field_value(
                anchor_field, old_anchor_values.get(symbol)
            )
        }
        if newly_available_symbols != accepted_filled_symbols:
            raise ValueError(
                "filled_symbols must match anchors newly available in the merged cohort"
            )

        for symbol in accepted_filled_symbols:
            row = merged_rows[symbol]
            if not any(
                self._is_recovered_field_value(field_name, row.get(field_name))
                for field_name in accepted_filled_fields
            ):
                raise ValueError("filled symbol has no available recovered field value")
        for field_name in accepted_filled_fields:
            if not any(
                self._is_recovered_field_value(
                    field_name, merged_rows[symbol].get(field_name)
                )
                for symbol in accepted_filled_symbols
            ):
                raise ValueError("filled field has no available recovered value")

        applied = self.apply_recovery(
            plan,
            merged_rows,
            evaluation_time_ms=evaluation_time_ms,
            observed_at_ms=result.observed_at_ms,
            source=result.source,
            recovery_state=(
                RECOVERY_APPLIED if accepted_filled_symbols else RECOVERY_ERROR
            ),
            source_anomaly_codes=(
                *result.source_anomaly_codes,
                *scope_anomalies,
                *completion_anomalies,
            ),
            invalid_source_symbols=tuple(
                sorted(
                    set(result.invalid_symbols)
                    | quarantined_symbols
                    | conflicting_symbols
                )
            ),
        )
        self._applied_recovery_results[result.idempotency_key] = (
            result.content_hash,
            plan.tag,
            applied,
        )
        return applied

    def _prepare_recovery_result_scope(
        self,
        plan: Any,
        current: AuctionAnchorRevisionV3,
        result: Any,
        primary_rows: Mapping[str, Mapping[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], set[str], set[str], tuple[str, ...], set[str]]:
        """Quarantine out-of-plan result data without discarding valid siblings.

        Plan/date/revision identity is validated by the caller. A row whose
        embedded symbol disagrees with its normalized key is quarantined as an
        invalid member; it is never reassigned to either symbol. Other valid
        declared members continue through the same recovery operation.
        """

        declared_symbols = set(result.filled_symbols)
        requested_symbols = set(plan.requested_symbols)
        known_symbols = set(current.expected_symbols)
        allowed_symbols = requested_symbols or known_symbols or declared_symbols
        requested_fields = set(plan.missing_fields)
        candidate_symbols = declared_symbols & allowed_symbols
        candidate_fields = set(result.filled_fields) & requested_fields
        anomaly_codes: set[str] = set()
        quarantined_symbols: set[str] = set(result.invalid_symbols)

        out_of_scope_symbols = declared_symbols - allowed_symbols
        if out_of_scope_symbols:
            anomaly_codes.add("RECOVERY_OUT_OF_SCOPE_SYMBOL_QUARANTINED")
            quarantined_symbols.update(out_of_scope_symbols)
        for field_name in set(result.filled_fields) - requested_fields:
            anomaly_codes.add(
                f"RECOVERY_OUT_OF_SCOPE_FIELD_QUARANTINED:{field_name}"
            )

        merged_rows = {
            symbol: dict(values)
            for symbol, values in _rows_by_symbol(result.rows).items()
        }
        for symbol in result.invalid_symbols:
            if symbol in primary_rows:
                # RecoveryResultV1 already excluded this ambiguous member;
                # retain the base row so completion diagnostics stay stable
                # across idempotent retries.
                merged_rows[symbol] = dict(primary_rows[symbol])
        for symbol, row in tuple(merged_rows.items()):
            if "symbol" in row and not self._row_symbol_matches(row["symbol"], symbol):
                # The mapping key cannot safely be reassigned to the embedded
                # identity, but one malformed provider member must not discard
                # valid sibling fills. Retain the exact primary row when one
                # exists; otherwise omit this new member and report it.
                candidate_symbols.discard(symbol)
                quarantined_symbols.add(symbol)
                anomaly_codes.add(
                    "RECOVERY_EMBEDDED_SYMBOL_MISMATCH_QUARANTINED"
                )
                merged_rows.pop(symbol, None)
                if symbol in primary_rows:
                    merged_rows[symbol] = dict(primary_rows[symbol])
                continue

            primary = primary_rows.get(symbol, {})
            for field_name in tuple(row):
                if field_name == "symbol" or field_name in primary:
                    continue
                if field_name not in requested_fields:
                    row.pop(field_name)
                    anomaly_codes.add(
                        f"RECOVERY_OUT_OF_SCOPE_FIELD_QUARANTINED:{field_name}"
                    )

            for field_name in requested_fields:
                if symbol in candidate_symbols and field_name in candidate_fields:
                    continue
                recovered_value = row.get(field_name)
                primary_value = primary.get(field_name)
                if not (
                    self._is_recovered_field_value(field_name, recovered_value)
                    and not self._is_recovered_field_value(field_name, primary_value)
                ):
                    continue
                if field_name in primary:
                    row[field_name] = primary_value
                else:
                    row.pop(field_name, None)
                anomaly_codes.add(
                    f"RECOVERY_UNDECLARED_FILL_QUARANTINED:{symbol}.{field_name}"
                )
                if symbol not in primary_rows:
                    quarantined_symbols.add(symbol)

            if symbol not in primary_rows and symbol not in candidate_symbols:
                merged_rows.pop(symbol)
                if symbol not in quarantined_symbols:
                    anomaly_codes.add("RECOVERY_OUT_OF_SCOPE_SYMBOL_QUARANTINED")
                    quarantined_symbols.add(symbol)
                continue

            if symbol not in allowed_symbols:
                # Preserve an already-observed row through normal completion;
                # keep its returned existing fields for conflict detection,
                # but do not accept new fields from an unrequested member.
                if symbol not in primary_rows:
                    merged_rows.pop(symbol)
                    quarantined_symbols.add(symbol)
                else:
                    for field_name in tuple(merged_rows[symbol]):
                        if field_name not in primary_rows[symbol]:
                            merged_rows[symbol].pop(field_name)
                            anomaly_codes.add(
                                f"RECOVERY_OUT_OF_SCOPE_FIELD_QUARANTINED:{field_name}"
                            )
                    if symbol in declared_symbols:
                        quarantined_symbols.add(symbol)
                anomaly_codes.add("RECOVERY_OUT_OF_SCOPE_SYMBOL_QUARANTINED")
                continue

        return (
            merged_rows,
            candidate_symbols,
            candidate_fields,
            tuple(sorted(anomaly_codes)),
            quarantined_symbols,
        )

    def _accepted_recovery_fills(
        self,
        candidate_symbols: set[str],
        candidate_fields: set[str],
        merged_rows: Mapping[str, Mapping[str, Any]],
        primary_rows: Mapping[str, Mapping[str, Any]],
    ) -> tuple[set[str], set[str]]:
        """Return only declared fills that add a usable value to the base."""

        accepted_fields = {
            field_name
            for field_name in candidate_fields
            if any(
                symbol in merged_rows
                and self._is_recovered_field_value(
                    field_name, merged_rows[symbol].get(field_name)
                )
                and not self._is_recovered_field_value(
                    field_name, primary_rows.get(symbol, {}).get(field_name)
                )
                for symbol in candidate_symbols
            )
        }
        accepted_symbols = {
            symbol
            for symbol in candidate_symbols
            if any(
                self._is_recovered_field_value(
                    field_name, merged_rows.get(symbol, {}).get(field_name)
                )
                and not self._is_recovered_field_value(
                    field_name, primary_rows.get(symbol, {}).get(field_name)
                )
                for field_name in accepted_fields
            )
        }
        accepted_fields = {
            field_name
            for field_name in accepted_fields
            if any(
                self._is_recovered_field_value(
                    field_name, merged_rows.get(symbol, {}).get(field_name)
                )
                for symbol in accepted_symbols
            )
        }
        return accepted_symbols, accepted_fields

    @staticmethod
    def _is_recovered_field_value(field_name: str, value: Any) -> bool:
        if field_name in _ANCHOR_PRICE_FIELDS.values():
            return isinstance(value, int) and not isinstance(value, bool) and value > 0
        return value is not None

    @classmethod
    def _complete_recovery_rows(
        cls,
        plan: Any,
        rows: Mapping[str, Mapping[str, Any]],
        primary_rows: Mapping[str, Mapping[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
        """Complete sparse recovery rows without replacing observed primary facts.

        Missing old symbols and fields are copied from the saved primary
        cohort. An unavailable requested value also preserves the primary
        value (including explicit missing/zero semantics). New symbols with no
        usable requested value are omitted. Plan-bound result application
        removes/quarantines out-of-scope facts before calling this helper;
        direct lower-level recovery continues to validate its explicit scope.
        """

        merged = {symbol: dict(values) for symbol, values in rows.items()}
        requested_fields = tuple(plan.missing_fields)
        anomaly_codes: set[str] = set()

        for symbol, primary in primary_rows.items():
            row = merged.get(symbol)
            if row is None:
                merged[symbol] = dict(primary)
                anomaly_codes.add("RECOVERY_SOURCE_SYMBOL_RESTORED")
                continue
            missing_primary_fields = tuple(
                field_name for field_name in primary if field_name not in row
            )
            if missing_primary_fields:
                for field_name in missing_primary_fields:
                    row[field_name] = primary[field_name]
                anomaly_codes.add("RECOVERY_SOURCE_FIELD_RESTORED")

        for symbol, row in tuple(merged.items()):
            primary = primary_rows.get(symbol)
            if primary is None:
                # A new member with no usable requested value contributes no
                # recovery fact. Do not let its default-only row block valid
                # members in the same cohort.
                has_unrequested_facts = any(
                    field_name != "symbol" and field_name not in requested_fields
                    for field_name in row
                )
                has_usable_requested_value = any(
                    cls._is_recovered_field_value(field_name, row.get(field_name))
                    for field_name in requested_fields
                )
                if not has_unrequested_facts and not has_usable_requested_value:
                    merged.pop(symbol)
                    anomaly_codes.add("RECOVERY_UNAVAILABLE_NEW_SYMBOL_OMITTED")
                continue

            for field_name in requested_fields:
                if cls._is_recovered_field_value(field_name, row.get(field_name)):
                    continue
                if field_name in primary:
                    if field_name in row and row[field_name] != primary[field_name]:
                        anomaly_codes.add("RECOVERY_UNAVAILABLE_FIELD_RESTORED")
                    row[field_name] = primary[field_name]
                else:
                    row.pop(field_name, None)
        return merged, tuple(sorted(anomaly_codes))

    def _validate_recovery_source_rows(
        self,
        plan: Any,
        merged_rows: Mapping[str, Mapping[str, Any]],
        *,
        allowed_symbols: set[str],
        allowed_fields: set[str],
    ) -> Tuple[str, ...]:
        """Validate recovery scope and return conflicting existing members.

        A changed previously observed value quarantines only that symbol. This
        validator remains strict for any out-of-scope data not already
        quarantined by plan-bound result handling. Callers preserve the primary
        row for each returned symbol before applying sibling fills.
        """

        primary_rows = self._source_rows.get(plan.tag, {})
        conflicting_symbols: set[str] = set()
        dropped_symbols = set(primary_rows) - set(merged_rows)
        if dropped_symbols:
            raise ValueError("merged recovery cohort dropped existing source symbols")

        requested_symbols = set(plan.requested_symbols)
        if not requested_symbols:
            current = self.latest(plan.tag)
            known_universe = set(current.expected_symbols) if current is not None else set()
            # With no declared universe, a full-cohort recovery plan has no
            # symbol list to constrain. The result's explicit allowed_symbols
            # is then the scope; apply_recovery_result supplies filled_symbols
            # so undeclared additions are still rejected.
            requested_symbols = known_universe or set(allowed_symbols)
        requested_fields = set(plan.missing_fields)

        for symbol, primary_row in primary_rows.items():
            merged_row = merged_rows[symbol]
            for field_name, primary_value in primary_row.items():
                if field_name not in merged_row:
                    raise ValueError(
                        "merged recovery cohort dropped existing source field "
                        f"{symbol}.{field_name}"
                    )
                recovered_value = merged_row[field_name]
                if field_name == "symbol":
                    if self._row_symbol_matches(primary_value, symbol) and self._row_symbol_matches(
                        recovered_value, symbol
                    ):
                        continue
                    raise ValueError(
                        "recovery row symbol does not match its normalized key"
                    )
                if semantic_hash(primary_value) == semantic_hash(recovered_value):
                    continue

                is_declared_fill = (
                    symbol in requested_symbols
                    and symbol in allowed_symbols
                    and field_name in requested_fields
                    and field_name in allowed_fields
                    and not self._is_recovered_field_value(field_name, primary_value)
                    and self._is_recovered_field_value(field_name, recovered_value)
                )
                if is_declared_fill:
                    continue
                if (
                    field_name in _ANCHOR_PRICE_FIELDS.values()
                    and self._is_recovered_field_value(field_name, primary_value)
                ):
                    conflicting_symbols.add(symbol)
                    continue
                if (
                    field_name in _ANCHOR_PRICE_FIELDS.values()
                    and not self._is_recovered_field_value(field_name, primary_value)
                    and self._is_recovered_field_value(field_name, recovered_value)
                ):
                    raise ValueError(
                        "filled_symbols must match anchors newly available in the merged cohort"
                    )
                conflicting_symbols.add(symbol)

            for field_name, recovered_value in merged_row.items():
                if field_name in primary_row:
                    continue
                if field_name == "symbol":
                    if self._row_symbol_matches(recovered_value, symbol):
                        continue
                    raise ValueError(
                        "recovery row symbol does not match its normalized key"
                    )
                is_declared_addition = (
                    symbol in requested_symbols
                    and symbol in allowed_symbols
                    and field_name in requested_fields
                    and field_name in allowed_fields
                    and self._is_recovered_field_value(field_name, recovered_value)
                )
                if not is_declared_addition:
                    raise ValueError(
                        "merged recovery cohort adds unrequested source field "
                        f"{symbol}.{field_name}"
                    )

        for symbol in set(merged_rows) - set(primary_rows):
            if symbol not in requested_symbols or symbol not in allowed_symbols:
                raise ValueError(
                    "merged recovery cohort added an unrequested source symbol"
                )
            if not any(
                field_name in requested_fields
                and field_name in allowed_fields
                and self._is_recovered_field_value(field_name, value)
                for field_name, value in merged_rows[symbol].items()
            ):
                raise ValueError(
                    "new recovery symbol has no requested recovered field"
                )
        return tuple(sorted(conflicting_symbols))

    @staticmethod
    def _row_symbol_matches(value: Any, expected_symbol: str) -> bool:
        try:
            return normalize_symbol(value) == expected_symbol
        except (TypeError, ValueError, UnicodeError):
            return False

    def latest(self, tag: str) -> Optional[AuctionAnchorRevisionV3]:
        return self._latest.get(tag)

    def apply_recovery(
        self,
        plan: Any,
        rows: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
        *,
        evaluation_time_ms: int,
        observed_at_ms: Optional[int] = None,
        source: str = "wencai",
        recovery_state: str = "APPLIED",
        source_anomaly_codes: Sequence[str] = (),
        invalid_source_symbols: Sequence[str] = (),
    ) -> AuctionAnchorRevisionV3:
        """Apply recovery rows as a new fact revision.

        The source owner fetches missing fields. Core safely completes omitted
        members/fields from its saved primary cohort, quarantines a row that
        conflicts with observed facts while preserving valid sibling fills,
        and rejects invalid identity or out-of-plan additions. Recovery and
        anomaly provenance are recorded, and output remains fact-only.
        Repeated cohorts with the same semantic content are idempotent because
        ``observe`` compares content hashes.
        """

        from .recovery import RecoveryPlanV1

        if not isinstance(plan, RecoveryPlanV1):
            raise TypeError("plan must be RecoveryPlanV1")
        tag = getattr(plan, "tag", None)
        if not isinstance(tag, str):
            raise ValueError("recovery plan must expose tag")
        if plan.trade_date != self.trade_date:
            raise ValueError("recovery plan trade_date does not match timeline")
        current = self.latest(tag)
        if current is None:
            raise ValueError("recovery cohort has no observed base revision")
        merged_rows, row_invalid_symbols, row_anomaly_codes = _normalize_observed_rows(rows)
        primary_rows = self._source_rows.get(tag, {})
        invalid_symbols = set(row_invalid_symbols)
        anomaly_codes = set(row_anomaly_codes)
        anomaly_codes.update(str(item) for item in source_anomaly_codes if str(item))
        for raw_symbol in invalid_source_symbols:
            try:
                invalid_symbols.add(normalize_symbol(raw_symbol))
            except (TypeError, ValueError, UnicodeError):
                anomaly_codes.add("INVALID_DIAGNOSTIC_SYMBOL")
        for symbol in invalid_symbols:
            if symbol in primary_rows:
                merged_rows[symbol] = dict(primary_rows[symbol])
        for symbol, row in tuple(merged_rows.items()):
            if "symbol" not in row or self._row_symbol_matches(row["symbol"], symbol):
                continue
            # A mismatched embedded identity makes this member unusable; do
            # not guess which symbol owns it. Preserve prior facts for a known
            # member and continue applying any valid sibling rows.
            invalid_symbols.add(symbol)
            anomaly_codes.add("RECOVERY_EMBEDDED_SYMBOL_MISMATCH_QUARANTINED")
            merged_rows.pop(symbol, None)
            if symbol in primary_rows:
                merged_rows[symbol] = dict(primary_rows[symbol])
        merged_rows, completion_anomalies = self._complete_recovery_rows(
            plan, merged_rows, primary_rows
        )
        anomaly_codes.update(completion_anomalies)
        expected_revision = plan.current_revision
        allowed_symbols = (
            set(plan.requested_symbols)
            or set(current.expected_symbols)
            or set(merged_rows)
        )
        conflicting_symbols = set(self._validate_recovery_source_rows(
            plan,
            merged_rows,
            allowed_symbols=allowed_symbols,
            allowed_fields=set(plan.missing_fields),
        ))
        for symbol in conflicting_symbols:
            merged_rows[symbol] = dict(primary_rows[symbol])
        if conflicting_symbols:
            invalid_symbols.update(conflicting_symbols)
            anomaly_codes.add("RECOVERY_CONFLICTING_SOURCE_ROW_QUARANTINED")
        anchor_field = _ANCHOR_PRICE_FIELDS[tag]
        old_anchor_values = self._anchor_values.get(tag, {})
        has_recovered_anchor = any(
            self._is_recovered_field_value(anchor_field, row.get(anchor_field))
            and not self._is_recovered_field_value(
                anchor_field, old_anchor_values.get(symbol)
            )
            for symbol, row in merged_rows.items()
        )
        if (conflicting_symbols or invalid_symbols) and not has_recovered_anchor:
            recovery_state = "ERROR"
        is_identical_retry = (
            current.revision == expected_revision + 1
            and semantic_hash(merged_rows)
            == semantic_hash(self._source_rows.get(tag, {}))
        )
        if current.revision != expected_revision and not is_identical_retry:
            raise ValueError("stale recovery plan; request against the current revision")
        expected = current.expected_symbols if current is not None and current.expected_symbols else getattr(plan, "requested_symbols", ())
        source_layers = tuple(
            dict.fromkeys(
                (*(current.source_layers if current is not None else ()), source)
            )
        )
        return self.observe(
            tag,
            merged_rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=expected,
            observed_at_ms=observed_at_ms,
            source_layers=source_layers,
            recovery_state=recovery_state,
            source_anomaly_codes=tuple(sorted(anomaly_codes)),
            invalid_source_symbols=tuple(sorted(invalid_symbols)),
        )

    def revisions(self, tag: str) -> Tuple[AuctionAnchorRevisionV3, ...]:
        return tuple(self._history.get(tag, ()))

    def build_analysis_bundle(self, tag: str = "0925") -> Mapping[str, Any]:
        current = self.latest(tag)
        if current is None:
            recovery_plan = None
            if tag == "0925":
                from .recovery import build_recovery_plan

                recovery_plan = build_recovery_plan(
                    self.trade_date,
                    tag,
                    requested_symbols=(),
                    missing_fields=(_ANCHOR_PRICE_FIELDS[tag],),
                    current_revision=0,
                    requested_at_ms=local_datetime_ms(self.trade_date, "09:25:06"),
                    reason="0925 anchor is unavailable; request an asynchronous recovery cohort",
                )
            return {
                "status": MISSING,
                "fact_status": FACT_ONLY,
                "anchor": None,
                "prior_deltas": {},
                "recovery_required": tag == "0925",
                "recovery_plan": recovery_plan,
            }
        prior_deltas = {}
        if tag == "0925":
            for prior in ("0920", "0924"):
                prior_revision = self.latest(prior)
                if prior_revision is None or not prior_revision.observed_symbols:
                    prior_deltas[prior] = "UNKNOWN"
                elif prior_revision.state == READY:
                    prior_deltas[prior] = "AVAILABLE"
                else:
                    prior_deltas[prior] = "PARTIAL"
        recovery_plan = None
        # A successful recovery attempt can still leave some symbols or fields
        # unavailable. Keep the initial partial result usable, and expose a
        # follow-up plan only for the residual missing set. If all expected
        # anchors are present, the fact remains PARTIAL after recovery (it is
        # not promoted to READY), but no further recovery is required.
        residual_expected_symbols_missing = bool(current.missing_anchor_symbols)
        unresolved_universe_needs_initial_recovery = (
            not current.expected_symbols
            and current.recovery_state != "APPLIED"
        )
        recovery_required = (
            tag == "0925"
            and current.state in {PARTIAL, MISSING}
            and (
                residual_expected_symbols_missing
                or unresolved_universe_needs_initial_recovery
            )
        )
        if recovery_required:
            from .recovery import build_recovery_plan

            recovery_plan = build_recovery_plan(
                self.trade_date,
                tag,
                requested_symbols=current.missing_symbols,
                missing_fields=(_ANCHOR_PRICE_FIELDS[tag],),
                current_revision=current.revision,
                requested_at_ms=current.evaluation_time_ms,
                reason="partial auction cohort; fill missing symbols/fields asynchronously",
                soft_deadline_ms=current.soft_deadline_ms,
            )
        bundle = {
            "status": current.state,
            "fact_status": FACT_ONLY,
            "anchor": current,
            "prior_deltas": prior_deltas,
            "recovery_required": recovery_required,
            "anchor_available_symbols": current.available_anchor_symbols,
            "missing_anchor_symbols": current.missing_anchor_symbols,
            "anchor_coverage": current.anchor_coverage,
            "source_observed_symbols": current.source_observed_symbols,
            "source_missing_symbols": current.source_missing_symbols,
            "source_coverage": current.source_coverage,
            "source_layers": current.source_layers,
            "revision": current.revision,
            "content_hash": current.content_hash,
            "recovery_plan": recovery_plan,
        }
        if current.source_anomaly_count:
            bundle["source_anomalies"] = {
                "count": current.source_anomaly_count,
                "codes": current.source_anomaly_codes,
                "invalid_symbols": current.invalid_source_symbols,
            }
        return bundle
