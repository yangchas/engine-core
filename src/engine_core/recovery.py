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
        normalized = {
            normalize_symbol(symbol): dict(values)
            for symbol, values in self.rows.items()
        }
        object.__setattr__(self, "rows", deep_freeze(normalized))
        object.__setattr__(self, "filled_symbols", tuple(sorted({normalize_symbol(item) for item in self.filled_symbols})))
        object.__setattr__(self, "filled_fields", tuple(sorted({str(item) for item in self.filled_fields})))
        object.__setattr__(self, "notes", tuple(self.notes))
        object.__setattr__(self, "content_hash", semantic_hash({
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
        }))
        object.__setattr__(self, "evidence_hash", evidence_hash({
            "observed_at_ms": self.observed_at_ms,
            "available_at_ms": self.available_at_ms,
            "notes": self.notes,
        }))

    @property
    def historical_cutoff_safe(self) -> bool:
        """Available-at is required before a result can enter cutoff replay."""

        return self.available_at_ms is not None

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
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        normalized = {normalize_symbol(symbol): dict(values) for symbol, values in self.rows.items()}
        object.__setattr__(self, "rows", deep_freeze(normalized))
        object.__setattr__(self, "filled_symbols", tuple(sorted(set(self.filled_symbols))))
        object.__setattr__(self, "filled_fields", tuple(sorted(set(self.filled_fields))))
        object.__setattr__(self, "source_layers", tuple(self.source_layers))
        if self.recovery_state != RECOVERY_APPLIED:
            raise ValueError("merge result must be APPLIED")
        object.__setattr__(self, "content_hash", semantic_hash({
            "contract": RECOVERY_MERGE_CONTRACT_VERSION,
            "rows": normalized,
            "filled_symbols": self.filled_symbols,
            "filled_fields": self.filled_fields,
            "source_layers": self.source_layers,
            "recovery_state": self.recovery_state,
        }))


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

    merged = {normalize_symbol(symbol): dict(values) for symbol, values in primary.items()}
    filled_symbols: set[str] = set()
    filled_fields: set[str] = set()
    for raw_symbol, raw_values in recovery.items():
        symbol = normalize_symbol(raw_symbol)
        target = merged.setdefault(symbol, {})
        for field_name, value in raw_values.items():
            if field_name not in target or target[field_name] is None:
                target[field_name] = value
                filled_symbols.add(symbol)
                filled_fields.add(str(field_name))
    return RecoveryMergeResultV1(
        rows=merged,
        filled_symbols=tuple(sorted(filled_symbols)),
        filled_fields=tuple(sorted(filled_fields)),
        source_layers=tuple(source_layers),
        recovery_state=RECOVERY_APPLIED,
    )
