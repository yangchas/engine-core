"""Soft-deadline auction facts and versioned late-correction timeline."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .clock import local_datetime_ms
from .contracts import evidence_hash, semantic_hash
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
            if len(value) != 8 or value[2] != ":" or value[5] != ":":
                raise ValueError("auction timing values must be HH:MM:SS")
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
        for name, values in (
            ("available_anchor_symbols", available),
            ("missing_anchor_symbols", missing_anchor),
            ("source_observed_symbols", source_observed),
            ("source_missing_symbols", source_missing),
        ):
            if set(values) - set(expected):
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
        object.__setattr__(self, "source_layers", tuple(self.source_layers))
        object.__setattr__(self, "content_hash", semantic_hash({
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
        }))
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
    by_symbol = _rows_by_symbol(rows)
    expected = tuple(sorted({normalize_symbol(item) for item in expected_symbols}))
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
        len(anchor_available) / float(len(expected)) if expected else None
    )
    source_coverage = (
        len(source_observed) / float(len(expected)) if expected else None
    )
    evaluation_second_ms = _whole_second_ms(evaluation_time_ms)
    if evaluation_second_ms < _whole_second_ms(times["first_observable_ms"]):
        state = OBSERVING
    elif not expected:
        # An unknown denominator cannot establish field completeness.
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
    event_times = [
        value.get("source_time_ms", value.get("ts"))
        for value in by_symbol.values()
        if value.get("source_time_ms", value.get("ts")) is not None
    ]
    event_times = [int(item) for item in event_times]
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
    )


class AuctionTimeline:
    """In-memory version ledger for auction facts and late corrections."""

    def __init__(self, trade_date: str, policies: Optional[Mapping[str, AuctionTimingPolicyV1]] = None) -> None:
        self.trade_date = trade_date
        self.policies = dict(policies or {tag: AuctionTimingPolicyV1.default(tag) for tag in ("0920", "0924", "0925")})
        self._history: dict[str, list[AuctionAnchorRevisionV3]] = {tag: [] for tag in self.policies}
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
    ) -> AuctionAnchorRevisionV3:
        if tag not in self.policies:
            raise ValueError("unsupported auction tag")
        history = self._history.setdefault(tag, [])
        candidate = build_auction_anchor_revision(
            self.trade_date,
            tag,
            rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=expected_symbols,
            observed_at_ms=observed_at_ms,
            source_layers=source_layers,
            revision=(history[-1].revision + 1 if history else 1),
            supersedes_revision=(history[-1].revision if history else None),
            recovery_state=recovery_state,
            policy=self.policies[tag],
        )
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
    ) -> AuctionAnchorRevisionV3:
        """Apply an already merged recovery cohort as a new fact revision.

        The external recovery owner is responsible for fetching and merging
        only missing fields.  Core records the source layer and remains
        fact-only; repeated cohorts with the same semantic content are
        idempotent because ``observe`` compares content hashes.
        """

        tag = getattr(plan, "tag", None)
        if not isinstance(tag, str):
            raise ValueError("recovery plan must expose tag")
        current = self.latest(tag)
        expected = current.expected_symbols if current is not None and current.expected_symbols else getattr(plan, "requested_symbols", ())
        return self.observe(
            tag,
            rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=expected,
            observed_at_ms=observed_at_ms,
            source_layers=(source,),
            recovery_state="APPLIED",
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
                    missing_fields=("anchor",),
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
        recovery_required = (
            tag == "0925"
            and current.state in {PARTIAL, MISSING}
            and current.recovery_state != "APPLIED"
        )
        if recovery_required:
            from .recovery import build_recovery_plan

            recovery_plan = build_recovery_plan(
                self.trade_date,
                tag,
                requested_symbols=current.missing_symbols,
                missing_fields=("anchor",),
                current_revision=current.revision,
                requested_at_ms=current.evaluation_time_ms,
                reason="partial auction cohort; fill missing symbols/fields asynchronously",
                soft_deadline_ms=current.soft_deadline_ms,
            )
        return {
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
