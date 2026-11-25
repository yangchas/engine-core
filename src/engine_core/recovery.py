"""Read-only recovery contracts for the external IntradayDataHub owner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple

from .contracts import deep_freeze, evidence_hash, semantic_hash
from .q2 import normalize_symbol


RECOVERY_PLAN_CONTRACT_VERSION = "RecoveryPlanV1"
RECOVERY_RESULT_CONTRACT_VERSION = "RecoveryResultV1"
RECOVERY_MERGE_CONTRACT_VERSION = "RecoveryMergeV1"

RECOVERY_NOT_REQUESTED = "NOT_REQUESTED"
RECOVERY_REQUESTED = "REQUESTED"
RECOVERY_IN_FLIGHT = "IN_FLIGHT"
RECOVERY_APPLIED = "APPLIED"
RECOVERY_NO_DATA = "NO_DATA"
RECOVERY_ERROR = "ERROR"

# Source-specific exception, not a general zero policy: the pinned t1-v2
# Q2 writer zero-initializes these three auction anchors until each is
# captured. TD snapshots encode the corresponding unavailable price as NULL.
# Keep this translation limited to these fields; other numeric zeroes remain
# ordinary values.
_ZERO_MEANS_MISSING_FIELDS = frozenset({
    "auction_anchor_0920_price_milli",
    "auction_anchor_0924_price_milli",
    "auction_anchor_0925_price_milli",
})


def _recovery_field_value_available(field_name: str, value: Any) -> bool:
    if field_name in _ZERO_MEANS_MISSING_FIELDS:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    return value is not None


def _normalize_recovery_mapping(
    rows: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], tuple[str, ...], tuple[str, ...]]:
    """Normalize a recovery mapping without choosing a conflicting duplicate.

    Identical aliases are harmless duplicates and collapse to one row. If two
    rows normalize to the same symbol but carry different content, that symbol
    is quarantined while other rows remain usable.
    """

    if not isinstance(rows, Mapping):
        raise TypeError("recovery rows must be a mapping")
    normalized: dict[str, Mapping[str, Any]] = {}
    invalid_symbols: set[str] = set()
    anomaly_codes: set[str] = set()
    for raw_symbol, raw_values in rows.items():
        try:
            symbol = normalize_symbol(raw_symbol)
        except (TypeError, ValueError, UnicodeError):
            anomaly_codes.add("INVALID_SYMBOL")
            continue
        if not isinstance(raw_values, Mapping):
            normalized.pop(symbol, None)
            invalid_symbols.add(symbol)
            anomaly_codes.add("ROW_NOT_MAPPING")
            continue
        if symbol in invalid_symbols:
            continue
        values = dict(raw_values)
        previous = normalized.get(symbol)
        if previous is None:
            normalized[symbol] = values
        elif semantic_hash(previous) != semantic_hash(values):
            normalized.pop(symbol, None)
            invalid_symbols.add(symbol)
            anomaly_codes.add("DUPLICATE_NORMALIZED_SYMBOL")
    return (
        {symbol: normalized[symbol] for symbol in sorted(normalized)},
        tuple(sorted(invalid_symbols)),
        tuple(sorted(anomaly_codes)),
    )


@dataclass(frozen=True)
class RecoveryPlanV1:
    plan_id: str
    trade_date: str
    tag: str
    requested_symbols: Tuple[str, ...]
    missing_fields: Tuple[str, ...]
    current_revision: int
    requested_at_ms: int
    reason: str
    preferred_sources: Tuple[str, ...] = ("REDIS_ANCHOR", "TDENGINE", "WENCAI")
    idempotency_key: str = ""
    soft_deadline_ms: Optional[int] = None
    source_layers: Tuple[str, ...] = ()
    recovery_state: str = RECOVERY_REQUESTED
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.plan_id or not self.trade_date or self.tag not in {"0920", "0924", "0925"}:
            raise ValueError("recovery plan identity is invalid")
        if self.current_revision < 0 or self.requested_at_ms <= 0:
            raise ValueError("recovery plan timing/revision is invalid")
        symbols = tuple(sorted({normalize_symbol(item) for item in self.requested_symbols}))
        fields = tuple(sorted({str(item) for item in self.missing_fields if str(item)}))
        if not fields and not symbols:
            raise ValueError("recovery plan must identify missing symbols or fields")
        object.__setattr__(self, "requested_symbols", symbols)
        object.__setattr__(self, "missing_fields", fields)
        key = self.idempotency_key or semantic_hash({
            "trade_date": self.trade_date,
            "tag": self.tag,
            "requested_symbols": symbols,
            "missing_fields": fields,
            "current_revision": self.current_revision,
        })
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "preferred_sources", tuple(self.preferred_sources))
        object.__setattr__(self, "source_layers", tuple(self.source_layers))
        if self.recovery_state not in {RECOVERY_REQUESTED, RECOVERY_IN_FLIGHT, RECOVERY_APPLIED}:
            raise ValueError("unsupported recovery plan state")
        object.__setattr__(self, "content_hash", semantic_hash({
            "contract": RECOVERY_PLAN_CONTRACT_VERSION,
            "plan_id": self.plan_id,
            "trade_date": self.trade_date,
            "tag": self.tag,
            "requested_symbols": symbols,
            "missing_fields": fields,
            "current_revision": self.current_revision,
            "reason": self.reason,
            "preferred_sources": self.preferred_sources,
            "idempotency_key": self.idempotency_key,
            "soft_deadline_ms": self.soft_deadline_ms,
            "source_layers": self.source_layers,
            "recovery_state": self.recovery_state,
        }))


@dataclass(frozen=True)
class RecoveryResultV1:
    """External recovery outcome carrying returned recovery rows.

    ``rows`` may be a sparse provider response. When applied to an
    ``AuctionTimeline``, omitted primary symbols/fields are restored from the
    exact observed base cohort; recovered values may fill requested missing
    fields only. A conflicting row is quarantined at symbol scope by the
    timeline; out-of-plan members/fields are quarantined at the smallest useful
    scope and reported as anomalies. Invalid plan identity remains an error;
    a row whose embedded symbol disagrees with its normalized key is
    quarantined at member scope and is never reassigned to another symbol.
    ``filled_symbols`` and ``filled_fields`` describe usable returned fills,
    not restored primary facts.
    """

    plan_id: str
    idempotency_key: str
    source: str
    recovery_state: str
    observed_at_ms: Optional[int]
    available_at_ms: Optional[int]
    rows: Mapping[str, Mapping[str, Any]]
    filled_symbols: Tuple[str, ...]
    filled_fields: Tuple[str, ...]
    base_revision: int
    resulting_revision: Optional[int]
    notes: Tuple[str, ...] = ()
    invalid_symbols: Tuple[str, ...] = ()
    source_anomaly_codes: Tuple[str, ...] = ()
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.plan_id or not self.idempotency_key or not self.source:
            raise ValueError("recovery result identity is required")
        if self.recovery_state not in {
            RECOVERY_APPLIED,
            RECOVERY_NO_DATA,
            RECOVERY_ERROR,
            RECOVERY_IN_FLIGHT,
        }:
            raise ValueError("unsupported recovery state")
        normalized, row_invalid_symbols, row_anomaly_codes = _normalize_recovery_mapping(
            self.rows
        )
        object.__setattr__(self, "rows", deep_freeze(normalized))
        anomaly_code_set = (
            {str(item) for item in self.source_anomaly_codes if str(item)}
            | set(row_anomaly_codes)
        )
        declared_filled_symbols: set[str] = set()
        for item in self.filled_symbols:
            try:
                declared_filled_symbols.add(normalize_symbol(item))
            except (TypeError, ValueError, UnicodeError):
                # A malformed member declaration must not discard other valid
                # fills in this response. Keep a stable diagnostic, not the raw
                # malformed token, in canonical evidence.
                anomaly_code_set.add("INVALID_FILLED_SYMBOL_DECLARATION")
        supplied_invalid_symbols: set[str] = set()
        for item in self.invalid_symbols:
            try:
                supplied_invalid_symbols.add(normalize_symbol(item))
            except (TypeError, ValueError, UnicodeError):
                anomaly_code_set.add("INVALID_DIAGNOSTIC_SYMBOL")
        declared_filled_fields = {str(item) for item in self.filled_fields if str(item)}
        missing_filled_symbols = (
            declared_filled_symbols
            - set(normalized)
            - set(row_invalid_symbols)
            - supplied_invalid_symbols
        )
        invalid_symbols = tuple(
            sorted(
                supplied_invalid_symbols
                | set(row_invalid_symbols)
                | missing_filled_symbols
            )
        )
        if missing_filled_symbols:
            anomaly_code_set.add("FILLED_SYMBOL_NOT_RETURNED")
        if invalid_symbols and not anomaly_code_set:
            anomaly_code_set.add("INVALID_RECOVERY_SYMBOL")
        candidate_filled_symbols = declared_filled_symbols - set(invalid_symbols)
        filled_symbols = tuple(sorted(
            symbol
            for symbol in candidate_filled_symbols
            if symbol in normalized
            and any(
                field_name in normalized[symbol]
                and _recovery_field_value_available(
                    field_name, normalized[symbol][field_name]
                )
                for field_name in declared_filled_fields
            )
        ))
        if set(filled_symbols) != candidate_filled_symbols:
            anomaly_code_set.add("DECLARED_FILL_NOT_AVAILABLE")
        filled_fields = tuple(sorted(
            field_name
            for field_name in declared_filled_fields
            if any(
                symbol in normalized
                and field_name in normalized[symbol]
                and _recovery_field_value_available(
                    field_name, normalized[symbol][field_name]
                )
                for symbol in filled_symbols
            )
        ))
        if set(filled_fields) != declared_filled_fields:
            anomaly_code_set.add("DECLARED_FIELD_NOT_AVAILABLE")
        anomaly_codes = tuple(sorted(anomaly_code_set))
        if self.base_revision < 0:
            raise ValueError("base_revision must be non-negative")
        if self.resulting_revision is not None and self.resulting_revision <= self.base_revision:
            raise ValueError("resulting_revision must be greater than base_revision")
        if self.recovery_state == RECOVERY_APPLIED:
            if not filled_symbols or not filled_fields:
                # Keep the clean contract strict, but let a response whose
                # declared members were explicitly quarantined reach the
                # timeline. It can then record an ERROR revision and preserve
                # the PARTIAL primary facts/retry requirement instead of
                # turning one malformed member into an exception for the
                # entire recovery pass.
                quarantined_fill_anomalies = {
                    "INVALID_SYMBOL",
                    "ROW_NOT_MAPPING",
                    "DUPLICATE_NORMALIZED_SYMBOL",
                    "INVALID_FILLED_SYMBOL_DECLARATION",
                    "INVALID_DIAGNOSTIC_SYMBOL",
                    "FILLED_SYMBOL_NOT_RETURNED",
                    "DECLARED_FILL_NOT_AVAILABLE",
                    "DECLARED_FIELD_NOT_AVAILABLE",
                }
                if not quarantined_fill_anomalies.intersection(anomaly_codes):
                    raise ValueError("APPLIED result must identify filled symbols and fields")
            else:
                if any(
                    not any(
                        field_name in normalized[symbol]
                        and normalized[symbol][field_name] is not None
                        for field_name in filled_fields
                    )
                    for symbol in filled_symbols
                ):
                    raise ValueError("each filled symbol must contain a filled field value")
                if any(
                    not any(
                        field_name in normalized[symbol]
                        and normalized[symbol][field_name] is not None
                        for symbol in filled_symbols
                    )
                    for field_name in filled_fields
                ):
                    raise ValueError("each filled field must have a non-null result value")
        object.__setattr__(self, "filled_symbols", filled_symbols)
        object.__setattr__(self, "filled_fields", filled_fields)
        object.__setattr__(self, "notes", tuple(self.notes))
        object.__setattr__(self, "invalid_symbols", invalid_symbols)
        object.__setattr__(self, "source_anomaly_codes", anomaly_codes)
        content_payload = {
            "contract": RECOVERY_RESULT_CONTRACT_VERSION,
            "plan_id": self.plan_id,
            "idempotency_key": self.idempotency_key,
            "source": self.source,
            "recovery_state": self.recovery_state,
            "rows": normalized,
            "filled_symbols": self.filled_symbols,
            "filled_fields": self.filled_fields,
            "base_revision": self.base_revision,
            "resulting_revision": self.resulting_revision,
        }
        if invalid_symbols or anomaly_codes:
            content_payload["invalid_symbols"] = invalid_symbols
            content_payload["source_anomaly_codes"] = anomaly_codes
        object.__setattr__(self, "content_hash", semantic_hash(content_payload))
        evidence_payload = {
            "observed_at_ms": self.observed_at_ms,
            "available_at_ms": self.available_at_ms,
            "notes": self.notes,
        }
        if invalid_symbols or anomaly_codes:
            evidence_payload["invalid_symbols"] = invalid_symbols
            evidence_payload["source_anomaly_codes"] = anomaly_codes
        object.__setattr__(self, "evidence_hash", evidence_hash(evidence_payload))

    def is_available_by(self, cutoff_ms: int) -> bool:
        """Compare the declared availability time to an explicit cutoff.

        This comparison does not authenticate the availability metadata; the
        source owner must still establish that ``available_at_ms`` is factual.
        """

        if isinstance(cutoff_ms, bool) or not isinstance(cutoff_ms, int) or cutoff_ms < 0:
            raise ValueError("cutoff_ms must be a non-negative integer timestamp")
        available_at_ms = self.available_at_ms
        if (
            isinstance(available_at_ms, bool)
            or not isinstance(available_at_ms, int)
            or available_at_ms < 0
        ):
            return False
        return available_at_ms <= cutoff_ms

    @property
    def source_layers(self) -> Tuple[str, ...]:
        return (self.source,)

    @property
    def remains_partial(self) -> bool:
        # Recovery never promotes a fact to READY; the caller must retain the
        # original completeness and record this result as an additional layer.
        return True


@dataclass(frozen=True)
class RecoveryMergeResultV1:
    rows: Mapping[str, Mapping[str, Any]]
    filled_symbols: Tuple[str, ...]
    filled_fields: Tuple[str, ...]
    source_layers: Tuple[str, ...]
    recovery_state: str
    invalid_symbols: Tuple[str, ...] = ()
    source_anomaly_codes: Tuple[str, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        normalized, row_invalid_symbols, row_anomaly_codes = _normalize_recovery_mapping(
            self.rows
        )
        object.__setattr__(self, "rows", deep_freeze(normalized))
        invalid_symbols = tuple(sorted(
            {normalize_symbol(item) for item in self.invalid_symbols}
            | set(row_invalid_symbols)
        ))
        anomaly_codes = tuple(sorted(
            {str(item) for item in self.source_anomaly_codes if str(item)}
            | set(row_anomaly_codes)
        ))
        if invalid_symbols and not anomaly_codes:
            anomaly_codes = ("INVALID_RECOVERY_SYMBOL",)
        filled_symbols = tuple(sorted(
            {normalize_symbol(item) for item in self.filled_symbols}
            - set(invalid_symbols)
        ))
        filled_fields = tuple(sorted({str(item) for item in self.filled_fields}))
        object.__setattr__(self, "filled_symbols", filled_symbols)
        object.__setattr__(self, "filled_fields", filled_fields)
        object.__setattr__(self, "source_layers", tuple(self.source_layers))
        object.__setattr__(self, "invalid_symbols", invalid_symbols)
        object.__setattr__(self, "source_anomaly_codes", anomaly_codes)
        if self.recovery_state != RECOVERY_APPLIED:
            raise ValueError("merge result must be APPLIED")
        content_payload = {
            "contract": RECOVERY_MERGE_CONTRACT_VERSION,
            "rows": normalized,
            "filled_symbols": self.filled_symbols,
            "filled_fields": self.filled_fields,
            "source_layers": self.source_layers,
            "recovery_state": self.recovery_state,
        }
        if invalid_symbols or anomaly_codes:
            content_payload["invalid_symbols"] = invalid_symbols
            content_payload["source_anomaly_codes"] = anomaly_codes
        object.__setattr__(self, "content_hash", semantic_hash(content_payload))


def build_recovery_plan(
    trade_date: str,
    tag: str,
    *,
    requested_symbols: Sequence[Any] = (),
    missing_fields: Sequence[str] = (),
    current_revision: int,
    requested_at_ms: int,
    reason: str,
    plan_id: Optional[str] = None,
    soft_deadline_ms: Optional[int] = None,
) -> RecoveryPlanV1:
    identity = plan_id or semantic_hash({
        "trade_date": trade_date,
        "tag": tag,
        "requested_symbols": tuple(sorted({normalize_symbol(item) for item in requested_symbols})),
        "missing_fields": tuple(sorted(set(missing_fields))),
        "current_revision": current_revision,
    })[:24]
    return RecoveryPlanV1(
        plan_id=identity,
        trade_date=trade_date,
        tag=tag,
        requested_symbols=tuple(normalize_symbol(item) for item in requested_symbols),
        missing_fields=tuple(missing_fields),
        current_revision=current_revision,
        requested_at_ms=requested_at_ms,
        reason=reason,
        soft_deadline_ms=soft_deadline_ms,
    )


def merge_recovery_rows(
    primary: Mapping[str, Mapping[str, Any]],
    recovery: Mapping[str, Mapping[str, Any]],
    *,
    source_layers: Sequence[str] = ("PRIMARY", "WENCAI"),
) -> RecoveryMergeResultV1:
    """Fill only missing fields; never overwrite an observed primary value."""

    normalized_primary, invalid_primary, primary_anomalies = _normalize_recovery_mapping(
        primary
    )
    normalized_recovery, invalid_recovery, recovery_anomalies = _normalize_recovery_mapping(
        recovery
    )
    invalid_symbols = set(invalid_primary) | set(invalid_recovery)
    anomaly_codes = set(primary_anomalies) | set(recovery_anomalies)
    merged = {symbol: dict(values) for symbol, values in normalized_primary.items()}
    filled_symbols: set[str] = set()
    filled_fields: set[str] = set()
    for symbol, raw_values in normalized_recovery.items():
        if symbol in invalid_symbols:
            continue
        target = merged.setdefault(symbol, {})
        for field_name, value in raw_values.items():
            current_value = target.get(field_name)
            zero_is_missing = (
                field_name in _ZERO_MEANS_MISSING_FIELDS
                and isinstance(current_value, (int, float))
                and not isinstance(current_value, bool)
                and current_value == 0
            )
            recovered_value_available = (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
                if field_name in _ZERO_MEANS_MISSING_FIELDS
                else value is not None
            )
            if (
                (field_name not in target or current_value is None or zero_is_missing)
                and recovered_value_available
            ):
                target[field_name] = value
                filled_symbols.add(symbol)
                filled_fields.add(str(field_name))
    return RecoveryMergeResultV1(
        rows=merged,
        filled_symbols=tuple(sorted(filled_symbols)),
        filled_fields=tuple(sorted(filled_fields)),
        source_layers=tuple(source_layers),
        recovery_state=RECOVERY_APPLIED,
        invalid_symbols=tuple(sorted(invalid_symbols)),
        source_anomaly_codes=tuple(sorted(anomaly_codes)),
    )
