"""Minimal query-data boundary for the first real DataFunction."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Tuple

from .calendar import CalendarCoverageError, TradingCalendarSnapshot, parse_trade_date
from .contracts import (
    DataRequest,
    DataResult,
    DataStatus,
    FrozenDataBundle,
    Provenance,
    deep_freeze,
)


@dataclass(frozen=True)
class DataContext:
    evaluation_id: str
    phase: str
    observed_at_ms: int


@dataclass(frozen=True)
class ProviderResult:
    """Raw result emitted by a physical access path.

    ``available_at_ms`` is only populated when the source provides historical
    availability evidence.  A successful query by itself only establishes
    ``observed_at_ms``.
    """

    raw_data: Any
    source_id: str
    source_schema: str
    effective_at_ms: Optional[int]
    available_at_ms: Optional[int]
    observed_at_ms: int
    availability_status: str = "UNKNOWN"
    evidence_ref: Optional[str] = None
    error: Optional[str] = None


class DataProvider(Protocol):
    """Physical source boundary; providers do not create ``DataResult``."""

    def fetch(
        self,
        request: DataRequest,
        *,
        previous_trade_date: str,
    ) -> ProviderResult:
        ...


def normalize_previous_day_stats_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    actual_trade_date: str,
) -> Mapping[str, Any]:
    """Map verified legacy daily-kline rows to the canonical payload.

    The caller owns the existing TD/Redis access path.  This function only
    validates the row shape and preserves explicit numeric zero values.  It
    deliberately rejects market-qualified symbols and missing required
    ``close``/``amount`` fields instead of silently repairing them.
    """

    if not isinstance(actual_trade_date, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", actual_trade_date
    ):
        raise ValueError("actual_trade_date must be YYYY-MM-DD")
    close_by_symbol = {}
    amount_by_symbol = {}
    volume_by_symbol = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TypeError("daily-kline row %d must be a mapping" % index)
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("daily-kline row %d has invalid symbol" % index)
        if symbol in close_by_symbol:
            raise ValueError("duplicate daily-kline symbol: %s" % symbol)
        for field in ("close", "amount"):
            if field not in row or not _finite_number(row[field]):
                raise ValueError(
                    "daily-kline row %d has invalid %s" % (index, field)
                )
        close_by_symbol[symbol] = row["close"]
        amount_by_symbol[symbol] = row["amount"]
        if "volume" in row and row["volume"] is not None:
            if not _finite_number(row["volume"]):
                raise ValueError(
                    "daily-kline row %d has invalid volume" % index
                )
            volume_by_symbol[symbol] = row["volume"]
    return {
        "previous_trade_date": actual_trade_date,
        "close_by_symbol": dict(sorted(close_by_symbol.items())),
        "amount_by_symbol": dict(sorted(amount_by_symbol.items())),
        "volume_by_symbol": dict(sorted(volume_by_symbol.items())),
        "row_count": len(close_by_symbol),
    }


def provider_result_from_previous_day_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    actual_trade_date: str,
    source_id: str,
    source_schema: str,
    effective_at_ms: Optional[int],
    available_at_ms: Optional[int],
    observed_at_ms: int,
    availability_status: str = "UNKNOWN",
    evidence_ref: Optional[str] = None,
) -> ProviderResult:
    """Build a thin ``ProviderResult`` around normalized legacy rows."""

    payload = normalize_previous_day_stats_rows(
        rows,
        actual_trade_date=actual_trade_date,
    )
    return ProviderResult(
        raw_data=payload,
        source_id=source_id,
        source_schema=source_schema,
        effective_at_ms=effective_at_ms,
        available_at_ms=available_at_ms,
        observed_at_ms=observed_at_ms,
        availability_status=availability_status,
        evidence_ref=evidence_ref,
    )


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


class PreviousDayStatsProvider(DataProvider, Protocol):
    """Specialized raw provider contract for previous-session statistics."""


def _data_result_from_provider(
    request: DataRequest,
    physical: ProviderResult,
) -> DataResult:
    """Translate one physical result into the business DataResult boundary."""

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
    actual_trade_date = (
        payload.get("previous_trade_date")
        if isinstance(payload, Mapping)
        else None
    )
    if status is DataStatus.READY and not isinstance(actual_trade_date, str):
        status = DataStatus.INVALID
    availability_status = str(physical.availability_status or "UNKNOWN").upper()
    available_at_ms = (
        physical.available_at_ms
        if availability_status == "VERIFIED"
        else None
    )
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


class TDPreviousDayStatsProvider:
    """Thin TD provider around an already verified read-only access callable.

    ``fetch_rows`` owns the existing TD connection, authentication, query,
    timeout and retry behavior.  This class only supplies the target date and
    maps rows into ``ProviderResult``.  It never initializes or mutates TD.
    """

    def __init__(
        self,
        fetch_rows: Callable[[str, Tuple[str, ...]], Iterable[Mapping[str, Any]]],
        *,
        observed_at_ms: Callable[[], int],
        available_at_ms: Optional[Callable[[], Optional[int]]] = None,
        source_id: str = "tdengine_daily_kline",
        source_schema: str = "daily_kline",
        evidence_ref: Optional[str] = None,
    ) -> None:
        self._fetch_rows = fetch_rows
        self._observed_at_ms = observed_at_ms
        self._available_at_ms = available_at_ms
        self._source_id = source_id
        self._source_schema = source_schema
        self._evidence_ref = evidence_ref

    def fetch(
        self,
        request: DataRequest,
        *,
        previous_trade_date: str,
    ) -> ProviderResult:
        try:
            # Materialize the iterable before sampling observation time.  The
            # legacy access path may return a generator whose actual fetch or
            # decode work happens during iteration, not at call return.
            rows = tuple(self._fetch_rows(previous_trade_date, tuple(request.symbols)))
            # Observation time means when this process obtained the result,
            # not when the request started.  Sampling after the legacy access
            # callable returns keeps provenance truthful for slow/blocked IO.
            observed = self._observed_at_ms()
            available = self._available_at_ms() if self._available_at_ms else None
            availability_status = "VERIFIED" if available is not None else "OBSERVED"
            return provider_result_from_previous_day_rows(
                rows,
                actual_trade_date=previous_trade_date,
                source_id=self._source_id,
                source_schema=self._source_schema,
                effective_at_ms=None,
                available_at_ms=available,
                observed_at_ms=observed,
                availability_status=availability_status,
                evidence_ref=self._evidence_ref,
            )
        except Exception as exc:
            # An access failure is an ERROR, not an empty dataset.  Keep the
            # error distinguishable from a successful query with zero rows.
            observed = self._observed_at_ms()
            available = None
            availability_status = "UNKNOWN"
            return ProviderResult(
                raw_data=None,
                source_id=self._source_id,
                source_schema=self._source_schema,
                effective_at_ms=None,
                available_at_ms=available,
                observed_at_ms=observed,
                availability_status=availability_status,
                evidence_ref=self._evidence_ref,
                error=type(exc).__name__ + ": " + str(exc),
            )


class DataFunction(Protocol):
    """Business data contract executed outside the reducer."""

    def execute(self, context: DataContext, request: DataRequest) -> DataResult:
        ...


class TemporalDataGuard:
    """Reject data whose historical availability is not cutoff-safe.

    ``available_at_ms`` is the only runtime knowledge-cutoff gate.  A provider
    observation proves when this process acquired a result, but it does not
    prove when the upstream source first made the result knowable.  Therefore
    ``observed_at_ms`` is audit/provenance data only and can never substitute
    for a missing ``available_at_ms``.
    """

    @staticmethod
    def check(result: DataResult, request: DataRequest) -> DataResult:
        if result.status in (
            DataStatus.MISSING,
            DataStatus.ERROR,
            DataStatus.INVALID,
            DataStatus.UNAVAILABLE,
        ):
            return result
        reasons = []
        if (
            result.effective_at_ms is not None
            and result.effective_at_ms > request.effective_as_of_ms
        ):
            reasons.append("effective_at_after_cutoff")
        if result.available_at_ms is None:
            reasons.append("available_at_unknown")
        elif result.available_at_ms > request.knowledge_as_of_ms:
            reasons.append("available_at_after_knowledge_cutoff")
        if not reasons:
            return result
        return replace(
            result,
            status=DataStatus.UNAVAILABLE,
            completeness=0.0,
            missing_fields=tuple(sorted(set(result.missing_fields + tuple(reasons)))),
        )


class PreviousDayStatsFunction:
    """Fetch previous-session statistics without silently changing the date."""

    function_id = "previous_day_stats"
    _core_fields = frozenset(
        {
            "previous_trade_date",
            "close_by_symbol",
            "amount_by_symbol",
            "row_count",
        }
    )
    _optional_fields = frozenset({"volume_by_symbol"})

    def __init__(
        self,
        provider: PreviousDayStatsProvider,
        calendar: TradingCalendarSnapshot,
    ) -> None:
        self._provider = provider
        self._calendar = calendar

    def execute(self, context: DataContext, request: DataRequest) -> DataResult:
        if request.function_id != self.function_id:
            raise ValueError(
                "request function_id must be %s" % self.function_id
            )
        allowed_fields = self._core_fields | self._optional_fields
        required_fields = set(request.required_fields) or set(self._core_fields)
        unknown_required = sorted(required_fields - allowed_fields)
        if unknown_required:
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
                missing_fields=tuple(
                    "unknown_required_field:" + field for field in unknown_required
                ),
            )
        try:
            requested_date = parse_trade_date(request.trade_date)
            if not self._calendar.is_trading_day(requested_date):
                raise ValueError("request trade_date is not a trading day")
            expected = self._calendar.previous_trade_day(requested_date).isoformat()
        except (TypeError, ValueError, CalendarCoverageError) as exc:
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
                missing_fields=("trade_date",),
                provenance=(
                    Provenance(
                        source_id=self._calendar.calendar_id,
                        source_kind="calendar",
                        source_schema="TradingCalendarSnapshotV1",
                        source_trade_date=request.trade_date,
                        effective_at_ms=None,
                        observed_at_ms=self._calendar.observed_at_ms,
                        evidence_ref=self._calendar.evidence_ref,
                        notes=("invalid_request: " + str(exc),),
                    ),
                ),
            )
        physical = self._provider.fetch(
            request,
            previous_trade_date=expected,
        )
        result = _data_result_from_provider(request, physical)
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
        # Preserve physical access/contract failures.  Only a successful
        # provider result with a non-mapping payload is classified as MISSING.
        if result.status in (
            DataStatus.ERROR,
            DataStatus.INVALID,
            DataStatus.UNAVAILABLE,
        ):
            return result
        if not isinstance(result.data, Mapping):
            return replace(result, status=DataStatus.MISSING, completeness=0.0)
        missing_fields = sorted(
            field for field in required_fields if field not in result.data
        )
        if missing_fields:
            return replace(
                result,
                status=DataStatus.INVALID,
                completeness=0.0,
                missing_fields=tuple(sorted(set(result.missing_fields) | set(missing_fields))),
            )
        result = TemporalDataGuard.check(result, request)
        if result.status in (
            DataStatus.MISSING,
            DataStatus.ERROR,
            DataStatus.INVALID,
            DataStatus.UNAVAILABLE,
        ):
            return result
        if result.actual_trade_date is None:
            return replace(result, status=DataStatus.INVALID)
        if result.actual_trade_date != expected:
            return replace(result, status=DataStatus.STALE)
        if not isinstance(result.data, Mapping):
            return replace(result, status=DataStatus.INVALID)
        close_by_symbol = result.data.get("close_by_symbol")
        amount_by_symbol = result.data.get("amount_by_symbol")
        if not isinstance(close_by_symbol, Mapping) or not isinstance(
            amount_by_symbol, Mapping
        ):
            return replace(
                result,
                status=DataStatus.INVALID,
                completeness=0.0,
                missing_fields=("close_by_symbol", "amount_by_symbol"),
            )
        requested_symbols = tuple(sorted(set(request.symbols)))
        if requested_symbols:
            missing_symbols = tuple(
                symbol
                for symbol in requested_symbols
                if symbol not in close_by_symbol or symbol not in amount_by_symbol
            )
            present_count = len(requested_symbols) - len(missing_symbols)
            completeness = present_count / float(len(requested_symbols))
            if present_count == 0:
                status = DataStatus.MISSING
            elif missing_symbols:
                status = DataStatus.PARTIAL
            else:
                status = DataStatus.READY
            result = replace(
                result,
                status=status,
                completeness=completeness,
                missing_symbols=missing_symbols,
            )
        return result


class ReadyDataStore:
    """Small in-memory store for data observed before an evaluation node.

    This is intentionally a dictionary-shaped readiness cache, not a catalog,
    registry or durable checkpoint.  Entries are keyed by business function,
    requested date, exact symbol scope and semantic content hash.  A caller
    must re-run :class:`TemporalDataGuard` on retrieval because a cached result
    can be valid for one knowledge cut-off and invalid for an earlier one.
    """

    def __init__(self) -> None:
        self._entries: dict[
            tuple[str, str, tuple[str, ...], str], DataResult
        ] = {}

    @staticmethod
    def _scope(request: DataRequest) -> tuple[str, ...]:
        return tuple(sorted(request.symbols))

    @classmethod
    def _key(
        cls,
        request: DataRequest,
        result: DataResult,
    ) -> tuple[str, str, tuple[str, ...], str]:
        if not result.content_hash:
            raise ValueError("ready data requires a semantic content_hash")
        return (
            request.function_id,
            request.trade_date,
            cls._scope(request),
            result.content_hash,
        )

    def put(self, request: DataRequest, result: DataResult) -> None:
        """Store only an already-guarded, complete result for its request."""

        if result.function_id != request.function_id:
            raise ValueError("result function_id does not match request")
        if result.requested_trade_date != request.trade_date:
            raise ValueError("result requested_trade_date does not match request")
        guarded = TemporalDataGuard.check(result, request)
        if guarded.status is not DataStatus.READY:
            reason = ",".join(guarded.missing_fields) or guarded.status.value
            raise ValueError("result is not ready for request: " + reason)
        if not self._satisfies_required_fields(request, guarded):
            raise ValueError("result does not contain request required_fields")
        self._entries[self._key(request, guarded)] = guarded

    @staticmethod
    def _satisfies_required_fields(
        request: DataRequest,
        result: DataResult,
    ) -> bool:
        """Check request-specific fields before accepting/reusing a cache entry.

        A result can be READY for a permissive request while still lacking a
        field required by a later node.  The readiness store must not turn
        that narrower result into a false cache hit.
        """

        if not request.required_fields:
            return True
        if not isinstance(result.data, Mapping):
            return False
        return all(field in result.data for field in request.required_fields)

    def get(self, request: DataRequest) -> Optional[DataResult]:
        """Return the newest temporally valid result for an exact scope."""

        candidates = []
        scope = self._scope(request)
        for (
            function_id,
            trade_date,
            entry_scope,
            _,
        ), result in self._entries.items():
            if (
                function_id != request.function_id
                or trade_date != request.trade_date
                or entry_scope != scope
            ):
                continue
            guarded = TemporalDataGuard.check(result, request)
            if (
                guarded.status is DataStatus.READY
                and self._satisfies_required_fields(request, guarded)
            ):
                candidates.append(guarded)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (item.observed_at_ms, item.content_hash),
        )

    def __len__(self) -> int:
        return len(self._entries)


def prefetch_ready_data(
    function: DataFunction,
    context: DataContext,
    request: DataRequest,
    store: ReadyDataStore,
) -> DataResult:
    """Execute and cache a result observed before a later evaluation node."""

    result = TemporalDataGuard.check(function.execute(context, request), request)
    if result.status is DataStatus.READY:
        store.put(request, result)
    return result


class FixturePreviousDayStatsProvider:
    """Deterministic provider used by unit tests and local replay fixtures."""

    def __init__(
        self,
        values_by_trade_date: Mapping[str, Mapping[str, Any]],
        *,
        observed_at_ms: int,
        available_at_ms: Optional[int] = None,
        effective_at_ms: Optional[int] = None,
        evidence_ref: str = "fixture://previous_day_stats",
    ) -> None:
        if observed_at_ms <= 0:
            raise ValueError("observed_at_ms must be positive")
        self._values_by_trade_date = deep_freeze(values_by_trade_date)
        self._observed_at_ms = observed_at_ms
        self._available_at_ms = available_at_ms
        self._effective_at_ms = effective_at_ms
        self._evidence_ref = evidence_ref

    def fetch(
        self,
        request: DataRequest,
        *,
        previous_trade_date: str,
    ) -> ProviderResult:
        values = self._values_by_trade_date.get(previous_trade_date)
        if values is None:
            return ProviderResult(
                raw_data=None,
                source_id="fixture",
                source_schema="PreviousDayStatsV1",
                effective_at_ms=None,
                available_at_ms=None,
                observed_at_ms=self._observed_at_ms,
                availability_status="UNKNOWN",
                evidence_ref=self._evidence_ref,
            )
        payload = deep_freeze(values)
        availability_status = (
            "VERIFIED" if self._available_at_ms is not None else "OBSERVED"
        )
        return ProviderResult(
            raw_data=payload,
            source_id="fixture",
            source_schema="PreviousDayStatsV1",
            effective_at_ms=self._effective_at_ms,
            available_at_ms=self._available_at_ms,
            observed_at_ms=self._observed_at_ms,
            availability_status=availability_status,
            evidence_ref=self._evidence_ref,
        )


def build_frozen_bundle(
    evaluation_id: str,
    knowledge_as_of_ms: int,
    function_order: Tuple[str, ...],
    results_by_function: Mapping[str, DataResult],
) -> FrozenDataBundle:
    """Freeze completed results in declaration order, never completion order."""

    return FrozenDataBundle.from_results(
        evaluation_id=evaluation_id,
        knowledge_as_of_ms=knowledge_as_of_ms,
        function_order=function_order,
        results_by_function=results_by_function,
    )
