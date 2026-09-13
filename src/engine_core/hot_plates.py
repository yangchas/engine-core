"""Strict boundary for the date-bucketed Kaipan hot-plate snapshot.

The physical Redis/Kaipan access path remains outside ``engine_core``.  This
module validates normalized rows, preserves the provider-declared top-list
scope, and refuses to treat a cache update timestamp as historical
availability evidence.
"""

from __future__ import annotations

from dataclasses import replace
import math
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from .calendar import CalendarCoverageError, TradingCalendarSnapshot, parse_trade_date
from .contracts import DataRequest, DataResult, DataStatus, Provenance
from .data import DataContext, ProviderResult, TemporalDataGuard


HOT_PLATES_CONTRACT_VERSION = "HotPlatesV1"
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_VERIFIED_UNIT_FIELDS = frozenset(
    {"rank", "strength", "hot", "change_pct", "net_inflow_yi"}
)


def _strict_date(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError(field + " must be strict YYYY-MM-DD")
    parsed = parse_trade_date(value)
    if parsed.isoformat() != value:
        raise ValueError(field + " must be a valid YYYY-MM-DD")
    return value


def _finite_number(value: Any, *, field: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(field + " must be a finite number")
    if not math.isfinite(float(value)):
        raise ValueError(field + " must be a finite number")
    return value


def _optional_finite_number(value: Any, *, field: str) -> float | int | None:
    if value is None:
        return None
    return _finite_number(value, field=field)


def _verified_available_at_ms(metadata: Any) -> Optional[int]:
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise ValueError("source metadata must be a mapping")
    value = metadata.get("available_at_ms")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("available_at_ms must be a positive integer")
    return value


def _verified_field_units(metadata: Any) -> Mapping[str, str]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise ValueError("source metadata must be a mapping")
    raw_units = metadata.get("field_units")
    if raw_units is None:
        return {}
    if not isinstance(raw_units, Mapping):
        raise ValueError("field_units must be a mapping")
    units: dict[str, str] = {}
    for field, unit in raw_units.items():
        if field not in _VERIFIED_UNIT_FIELDS:
            raise ValueError("unknown verified field unit: " + str(field))
        if not isinstance(unit, str) or not unit.strip():
            raise ValueError("verified field unit must be non-empty")
        units[str(field)] = unit.strip()
    return units


def normalize_hot_plates_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    actual_trade_date: str,
    verified_field_units: Optional[Mapping[str, str]] = None,
) -> Mapping[str, Any]:
    """Validate and canonically order provider-declared hot-plate rows."""

    actual_trade_date = _strict_date(actual_trade_date, field="actual_trade_date")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise TypeError("hot-plate row %d must be a mapping" % index)
        row_date = _strict_date(raw.get("trade_date"), field="trade_date")
        if row_date != actual_trade_date:
            raise ValueError("hot-plate row %d trade_date mismatch" % index)

        plate_name = raw.get("plate_name")
        if not isinstance(plate_name, str) or not plate_name.strip():
            raise ValueError("hot-plate row %d has invalid plate_name" % index)
        plate_name = plate_name.strip()
        if plate_name in seen:
            raise ValueError("duplicate hot-plate name: " + plate_name)
        seen.add(plate_name)

        rank = raw.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ValueError("hot-plate row %d has invalid rank" % index)
        source = raw.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("hot-plate row %d has invalid source" % index)

        normalized.append(
            {
                "trade_date": row_date,
                "plate_name": plate_name,
                "rank": rank,
                "strength": _optional_finite_number(raw.get("strength"), field="strength"),
                "hot": _optional_finite_number(raw.get("hot"), field="hot"),
                "change_pct": _optional_finite_number(raw.get("change_pct"), field="change_pct"),
                "net_inflow_yi": _optional_finite_number(raw.get("net_inflow_yi"), field="net_inflow_yi"),
                "source": source.strip(),
            }
        )

    normalized.sort(key=lambda item: (item["rank"], item["plate_name"]))
    by_plate = {item["plate_name"]: item for item in normalized}
    field_units = {
        "rank": "ordinal",
        "strength": "UNKNOWN",
        "hot": "UNKNOWN",
        "change_pct": "percent",
        "net_inflow_yi": "UNKNOWN",
    }
    if verified_field_units:
        for field, unit in verified_field_units.items():
            if field not in field_units or not isinstance(unit, str) or not unit.strip():
                raise ValueError("invalid verified field unit: " + str(field))
            field_units[field] = unit.strip()
    unit_uncertainties = tuple(
        sorted(field for field, unit in field_units.items() if unit == "UNKNOWN")
    )
    return {
        "contract_version": HOT_PLATES_CONTRACT_VERSION,
        "trade_date": actual_trade_date,
        "rows": normalized,
        "by_plate": by_plate,
        "row_count": len(normalized),
        "scope": "provider_declared_top_plates",
        "field_units": field_units,
        "unit_uncertainties": unit_uncertainties,
    }


class HotPlatesProvider(Protocol):
    def fetch(self, request: DataRequest, *, trade_date: str) -> ProviderResult:
        ...


class RedisHotPlatesProvider:
    """Thin provider around a bounded, read-only Redis callable."""

    def __init__(
        self,
        fetch_rows: Callable[[str], Iterable[Mapping[str, Any]]],
        *,
        observed_at_ms: Callable[[], int],
        available_at_ms: Optional[Callable[[], Optional[int]]] = None,
        metadata: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
        source_id: str = "redis_hot_plates",
        source_schema: str = HOT_PLATES_CONTRACT_VERSION,
        evidence_ref: Optional[str] = None,
        verified_field_units: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._fetch_rows = fetch_rows
        self._observed_at_ms = observed_at_ms
        self._available_at_ms = available_at_ms
        self._metadata = metadata
        self._source_id = source_id
        self._source_schema = source_schema
        self._evidence_ref = evidence_ref
        self._verified_field_units = dict(verified_field_units or {})

    def fetch(self, request: DataRequest, *, trade_date: str) -> ProviderResult:
        try:
            rows = tuple(self._fetch_rows(trade_date))
            observed = self._observed_at_ms()
            metadata = self._metadata(trade_date) if self._metadata else None
            available = self._available_at_ms() if self._available_at_ms else None
            verified_units = dict(self._verified_field_units)
            if metadata is not None:
                if not isinstance(metadata, Mapping):
                    raise ValueError("source metadata must be a mapping")
                has_contract_fields = any(
                    field in metadata for field in ("available_at_ms", "field_units")
                )
                if has_contract_fields and metadata.get("schema_version") != HOT_PLATES_CONTRACT_VERSION:
                    raise ValueError("source metadata schema_version is not verified")
                if "available_at_ms" in metadata:
                    available = _verified_available_at_ms(metadata)
                metadata_units = _verified_field_units(metadata)
                if metadata_units:
                    verified_units = dict(metadata_units)
            return ProviderResult(
                raw_data=normalize_hot_plates_rows(
                    rows,
                    actual_trade_date=trade_date,
                    verified_field_units=verified_units,
                ),
                source_id=self._source_id,
                source_schema=self._source_schema,
                effective_at_ms=None,
                available_at_ms=available,
                observed_at_ms=observed,
                availability_status="VERIFIED" if available is not None else "OBSERVED",
                evidence_ref=self._evidence_ref,
            )
        except Exception as exc:
            return ProviderResult(
                raw_data=None,
                source_id=self._source_id,
                source_schema=self._source_schema,
                effective_at_ms=None,
                available_at_ms=None,
                observed_at_ms=self._observed_at_ms(),
                availability_status="UNKNOWN",
                evidence_ref=self._evidence_ref,
                error=type(exc).__name__ + ": " + str(exc),
            )


def _result_from_provider(request: DataRequest, physical: ProviderResult) -> DataResult:
    if physical.error:
        status = DataStatus.ERROR
    elif not isinstance(physical.raw_data, Mapping):
        status = DataStatus.MISSING
    else:
        status = DataStatus.READY
    payload = physical.raw_data
    if (
        status is DataStatus.READY
        and isinstance(payload.get("row_count"), int)
        and payload["row_count"] == 0
    ):
        status = DataStatus.MISSING
    availability_status = str(physical.availability_status or "UNKNOWN").upper()
    available_at_ms = physical.available_at_ms if availability_status == "VERIFIED" else None
    actual_trade_date = payload.get("trade_date") if isinstance(payload, Mapping) else None
    return DataResult(
        request_id=request.request_id,
        function_id=request.function_id,
        status=status,
        data=payload,
        actual_source=physical.source_id,
        requested_trade_date=request.trade_date,
        actual_trade_date=actual_trade_date,
        effective_at_ms=physical.effective_at_ms,
        available_at_ms=available_at_ms,
        observed_at_ms=physical.observed_at_ms,
        schema_version=1,
        completeness=1.0 if status is DataStatus.READY else 0.0,
        missing_fields=("error",) if physical.error else (),
        provenance=(
            Provenance(
                source_id=physical.source_id,
                source_kind="legacy_provider",
                source_schema=physical.source_schema,
                source_trade_date=actual_trade_date,
                effective_at_ms=physical.effective_at_ms,
                observed_at_ms=physical.observed_at_ms,
                evidence_ref=physical.evidence_ref,
                notes=("availability=" + availability_status,),
            ),
        ),
    )


class HotPlatesFunction:
    """Fetch a date-bound, provider-declared hot-plate snapshot."""

    function_id = "hot_plates"
    _core_fields = frozenset({"trade_date", "rows", "by_plate", "row_count"})
    _optional_fields = frozenset({"scope", "field_units", "unit_uncertainties"})

    def __init__(self, provider: HotPlatesProvider, calendar: TradingCalendarSnapshot) -> None:
        self._provider = provider
        self._calendar = calendar

    def execute(self, context: DataContext, request: DataRequest) -> DataResult:
        if request.function_id != self.function_id:
            raise ValueError("request function_id must be " + self.function_id)
        if request.symbols:
            return self._invalid_result(
                request,
                context,
                ("symbol_scope_not_supported",),
                reason="hot-plates is a plate snapshot, not a symbol-scoped function",
            )
        allowed_fields = self._core_fields | self._optional_fields
        required_fields = set(request.required_fields) or set(self._core_fields)
        unknown = sorted(required_fields - allowed_fields)
        if unknown:
            return self._invalid_result(
                request,
                context,
                ("unknown_required_field:" + field for field in unknown),
            )
        try:
            requested = parse_trade_date(request.trade_date)
            if not self._calendar.is_trading_day(requested):
                raise ValueError("request trade_date is not a trading day")
            expected = requested.isoformat()
        except (TypeError, ValueError, CalendarCoverageError) as exc:
            return self._invalid_result(request, context, ("trade_date",), reason=str(exc))

        result = _result_from_provider(
            request,
            self._provider.fetch(request, trade_date=expected),
        )
        result = replace(
            result,
            provenance=result.provenance
            + (
                Provenance(
                    source_id=self._calendar.calendar_id,
                    source_kind="calendar",
                    source_schema="TradingCalendarSnapshotV1",
                    source_trade_date=expected,
                    effective_at_ms=None,
                    observed_at_ms=self._calendar.observed_at_ms,
                    evidence_ref=self._calendar.evidence_ref,
                    notes=(
                        "calendar_version=" + self._calendar.version,
                        "calendar_semantic_hash=" + self._calendar.semantic_hash,
                        "requested_trade_date=" + expected,
                    ),
                ),
            ),
        )
        if result.status in (DataStatus.ERROR, DataStatus.INVALID, DataStatus.MISSING):
            return result
        if not isinstance(result.data, Mapping):
            return replace(result, status=DataStatus.MISSING, completeness=0.0)
        missing = sorted(field for field in required_fields if field not in result.data)
        if missing:
            return replace(
                result,
                status=DataStatus.INVALID,
                completeness=0.0,
                missing_fields=tuple(sorted(set(result.missing_fields) | set(missing))),
            )
        result = TemporalDataGuard.check(result, request)
        if result.status in (DataStatus.UNAVAILABLE, DataStatus.INVALID, DataStatus.ERROR):
            return result
        unit_uncertainties = result.data.get("unit_uncertainties", ())
        if unit_uncertainties:
            return replace(
                result,
                status=DataStatus.UNAVAILABLE,
                completeness=0.0,
                missing_fields=tuple(
                    sorted(
                        set(result.missing_fields)
                        | {"unit_unknown:" + str(field) for field in unit_uncertainties}
                    )
                ),
            )
        if result.actual_trade_date != expected:
            return replace(result, status=DataStatus.STALE, completeness=0.0)
        by_plate = result.data.get("by_plate")
        if not isinstance(by_plate, Mapping):
            return replace(
                result,
                status=DataStatus.INVALID,
                completeness=0.0,
                missing_fields=tuple(sorted(set(result.missing_fields) | {"by_plate"})),
            )
        return result

    @staticmethod
    def _invalid_result(
        request: DataRequest,
        context: DataContext,
        missing_fields: Iterable[str],
        *,
        reason: str = "",
    ) -> DataResult:
        notes = (("invalid_request: " + reason),) if reason else ()
        return DataResult(
            request_id=request.request_id,
            function_id=request.function_id,
            status=DataStatus.INVALID,
            data=None,
            actual_source=None,
            requested_trade_date=request.trade_date,
            actual_trade_date=None,
            effective_at_ms=None,
            available_at_ms=None,
            observed_at_ms=context.observed_at_ms,
            schema_version=1,
            completeness=0.0,
            missing_fields=tuple(missing_fields),
            provenance=(
                Provenance(
                    source_id="calendar",
                    source_kind="calendar",
                    source_schema="TradingCalendarSnapshotV1",
                    source_trade_date=request.trade_date,
                    effective_at_ms=None,
                    observed_at_ms=None,
                    evidence_ref=None,
                    notes=notes,
                ),
            ),
        )
