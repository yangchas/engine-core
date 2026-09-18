"""Strict previous-session limit-up pool fact boundary.

The physical Redis/Kaipan access path stays outside ``engine_core``.  This
module only validates the already observed normalized rows and applies the
same calendar and historical-availability guards as the first daily-kline
function.  It does not infer a full-market denominator, merge Wencai truth,
or convert percentage fields into another unit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
import math
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Tuple

from .calendar import CalendarCoverageError, TradingCalendarSnapshot, parse_trade_date
from .contracts import DataRequest, DataResult, DataStatus, Provenance
from .data import DataContext, ProviderResult, TemporalDataGuard


PREVIOUS_DAY_LIMIT_POOL_CONTRACT_VERSION = "PreviousDayLimitPoolV1"


def canonical_previous_day_limit_pool_payload_hash(
    rows: Iterable[Mapping[str, Any]],
) -> str:
    """Hash the symbol-keyed Redis payload using the producer contract."""

    canonical = {
        str(row.get("symbol") or ""): dict(row)
        for row in rows
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_SYMBOL_PATTERN = re.compile(r"\d{6}")


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


_VERIFIED_UNIT_FIELDS = frozenset({"lb_days", "turnover", "close_pct"})


def _verified_available_at_ms(metadata: Any) -> Optional[int]:
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise ValueError("source metadata must be a mapping")
    if "available_at_ms" not in metadata or metadata.get("available_at_ms") is None:
        return None
    value = metadata.get("available_at_ms")
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


def normalize_previous_day_limit_pool_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    actual_trade_date: str,
    verified_field_units: Optional[Mapping[str, str]] = None,
) -> Mapping[str, Any]:
    """Validate and canonically order the legacy Kaipan pool rows.

    ``trade_date`` is required in every row.  The caller must not backfill it
    from the request date because the cache is a delayed, date-bucketed
    source.  ``turnover`` keeps its raw value but remains ``UNKNOWN`` until
    the source explicitly verifies its unit; ``close_pct`` remains a
    percentage-point value and is never silently converted.
    """

    actual_trade_date = _strict_date(actual_trade_date, field="actual_trade_date")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise TypeError("limit-pool row %d must be a mapping" % index)
        row_date = _strict_date(raw.get("trade_date"), field="trade_date")
        if row_date != actual_trade_date:
            raise ValueError("limit-pool row %d trade_date mismatch" % index)
        symbol = raw.get("symbol")
        if not isinstance(symbol, str) or _SYMBOL_PATTERN.fullmatch(symbol) is None:
            raise ValueError("limit-pool row %d has invalid symbol" % index)
        if symbol in seen:
            raise ValueError("duplicate limit-pool symbol: " + symbol)
        seen.add(symbol)

        name = raw.get("name")
        source = raw.get("source")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("limit-pool row %d has invalid name" % index)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("limit-pool row %d has invalid source" % index)
        lb_days = raw.get("lb_days")
        if isinstance(lb_days, bool) or not isinstance(lb_days, int) or lb_days < 1:
            raise ValueError("limit-pool row %d has invalid lb_days" % index)

        plate = raw.get("plate")
        seal_time = raw.get("seal_time")
        if plate is not None and not isinstance(plate, str):
            raise ValueError("limit-pool row %d has invalid plate" % index)
        if seal_time is not None and not isinstance(seal_time, str):
            raise ValueError("limit-pool row %d has invalid seal_time" % index)

        normalized.append(
            {
                "trade_date": row_date,
                "symbol": symbol,
                "name": name,
                "lb_days": lb_days,
                "plate": plate,
                "seal_time": seal_time,
                "turnover": _finite_number(raw.get("turnover"), field="turnover"),
                "close_pct": _finite_number(raw.get("close_pct"), field="close_pct"),
                "source": source,
            }
        )

    normalized.sort(key=lambda item: item["symbol"])
    by_symbol = {item["symbol"]: item for item in normalized}
    field_units = {
        "lb_days": "boards",
        "turnover": "UNKNOWN",
        "close_pct": "percent",
    }
    if verified_field_units:
        for field, unit in verified_field_units.items():
            if field not in field_units or not isinstance(unit, str) or not unit.strip():
                raise ValueError("invalid verified field unit: " + str(field))
            field_units[field] = unit
    unit_uncertainties = tuple(
        sorted(field for field, unit in field_units.items() if unit == "UNKNOWN")
    )
    return {
        "contract_version": PREVIOUS_DAY_LIMIT_POOL_CONTRACT_VERSION,
        "previous_trade_date": actual_trade_date,
        "rows": normalized,
        "by_symbol": by_symbol,
        "row_count": len(normalized),
        "scope": "provider_declared_pool",
        "field_units": field_units,
        "unit_uncertainties": unit_uncertainties,
    }


class PreviousDayLimitPoolProvider(Protocol):
    """Physical source boundary for a delayed previous-session pool."""

    def fetch(
        self,
        request: DataRequest,
        *,
        previous_trade_date: str,
    ) -> ProviderResult:
        ...


class RedisPreviousDayLimitPoolProvider:
    """Thin provider around a bounded, read-only Redis callable."""

    def __init__(
        self,
        fetch_rows: Callable[[str], Iterable[Mapping[str, Any]]],
        *,
        observed_at_ms: Callable[[], int],
        available_at_ms: Optional[Callable[[], Optional[int]]] = None,
        metadata: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
        source_id: str = "redis_yest_limit_pool",
        source_schema: str = PREVIOUS_DAY_LIMIT_POOL_CONTRACT_VERSION,
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

    def fetch(
        self,
        request: DataRequest,
        *,
        previous_trade_date: str,
    ) -> ProviderResult:
        try:
            rows = tuple(self._fetch_rows(previous_trade_date))
            observed = self._observed_at_ms()
            metadata = self._metadata(previous_trade_date) if self._metadata else None
            available = self._available_at_ms() if self._available_at_ms else None
            verified_units = dict(self._verified_field_units)
            if metadata is not None:
                if not isinstance(metadata, Mapping):
                    raise ValueError("source metadata must be a mapping")
                has_contract_fields = any(
                    field in metadata for field in ("available_at_ms", "field_units")
                )
                if has_contract_fields and metadata.get("schema_version") != PREVIOUS_DAY_LIMIT_POOL_CONTRACT_VERSION:
                    raise ValueError("source metadata schema_version is not verified")
                if has_contract_fields:
                    expected_hash = metadata.get("payload_sha256")
                    if not isinstance(expected_hash, str) or not expected_hash:
                        raise ValueError("source metadata payload_sha256 is missing")
                    actual_hash = canonical_previous_day_limit_pool_payload_hash(rows)
                    if expected_hash != actual_hash:
                        raise ValueError("source metadata payload_sha256 mismatch")
                if isinstance(metadata, Mapping) and "available_at_ms" in metadata:
                    available = _verified_available_at_ms(metadata)
                metadata_units = _verified_field_units(metadata)
                if metadata_units:
                    verified_units = dict(metadata_units)
            return ProviderResult(
                raw_data=normalize_previous_day_limit_pool_rows(
                    rows,
                    actual_trade_date=previous_trade_date,
                    verified_field_units=verified_units,
                ),
                source_id=self._source_id,
                source_schema=self._source_schema,
                effective_at_ms=None,
                available_at_ms=available,
                observed_at_ms=observed,
                fetch_completed_at_ms=observed,
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
    actual_trade_date = payload.get("previous_trade_date") if isinstance(payload, Mapping) else None
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
        temporal_mode=request.temporal_mode,
        fetch_completed_at_ms=physical.fetch_completed_at_ms,
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


class PreviousDayLimitPoolFunction:
    """Fetch a date-bound pool without changing its source or scope."""

    function_id = "previous_day_limit_pool"
    _core_fields = frozenset({"previous_trade_date", "rows", "by_symbol", "row_count"})

    def __init__(
        self,
        provider: PreviousDayLimitPoolProvider,
        calendar: TradingCalendarSnapshot,
    ) -> None:
        self._provider = provider
        self._calendar = calendar

    def execute(self, context: DataContext, request: DataRequest) -> DataResult:
        if request.function_id != self.function_id:
            raise ValueError("request function_id must be " + self.function_id)
        required_fields = set(request.required_fields) or set(self._core_fields)
        unknown = sorted(required_fields - self._core_fields)
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
            expected = self._calendar.previous_trade_day(requested).isoformat()
        except (TypeError, ValueError, CalendarCoverageError) as exc:
            return self._invalid_result(request, context, ("trade_date",), reason=str(exc))

        result = _result_from_provider(
            request,
            self._provider.fetch(request, previous_trade_date=expected),
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
                        "derived_previous_trade_date=" + expected,
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

        by_symbol = result.data.get("by_symbol")
        if not isinstance(by_symbol, Mapping):
            return replace(
                result,
                status=DataStatus.INVALID,
                completeness=0.0,
                missing_fields=("by_symbol",),
            )
        requested_symbols = tuple(sorted(set(request.symbols)))
        if requested_symbols:
            missing_symbols = tuple(symbol for symbol in requested_symbols if symbol not in by_symbol)
            present_count = len(requested_symbols) - len(missing_symbols)
            result = replace(
                result,
                status=(
                    DataStatus.MISSING
                    if present_count == 0
                    else DataStatus.PARTIAL
                    if missing_symbols
                    else DataStatus.READY
                ),
                completeness=present_count / float(len(requested_symbols)),
                missing_symbols=missing_symbols,
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
            temporal_mode=request.temporal_mode,
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
