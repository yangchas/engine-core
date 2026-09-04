"""Minimal query-data boundary for the first real DataFunction."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Tuple

from .contracts import (
    DataRequest,
    DataResult,
    DataStatus,
    FrozenDataBundle,
    Provenance,
    semantic_hash,
)


@dataclass(frozen=True)
class DataContext:
    evaluation_id: str
    phase: str
    observed_at_ms: int
    expected_previous_trade_date: Optional[str] = None


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
    semantic_content = {
        "function_id": request.function_id,
        "requested_trade_date": request.trade_date,
        "actual_trade_date": actual_trade_date,
        "data": payload,
        "effective_at_ms": physical.effective_at_ms,
        "available_at_ms": available_at_ms,
        "status": status,
    }
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
        content_hash=semantic_hash(semantic_content),
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
        source_id: str = "tdengine_daily_kline",
        source_schema: str = "daily_kline",
        evidence_ref: Optional[str] = None,
    ) -> None:
        self._fetch_rows = fetch_rows
        self._observed_at_ms = observed_at_ms
        self._source_id = source_id
        self._source_schema = source_schema
        self._evidence_ref = evidence_ref

    def fetch(
        self,
        request: DataRequest,
        *,
        previous_trade_date: str,
    ) -> ProviderResult:
        observed = self._observed_at_ms()
        try:
            rows = self._fetch_rows(previous_trade_date, tuple(request.symbols))
            return provider_result_from_previous_day_rows(
                rows,
                actual_trade_date=previous_trade_date,
                source_id=self._source_id,
                source_schema=self._source_schema,
                effective_at_ms=None,
                available_at_ms=None,
                observed_at_ms=observed,
                availability_status="OBSERVED",
                evidence_ref=self._evidence_ref,
            )
        except Exception as exc:
            return ProviderResult(
                raw_data=None,
                source_id=self._source_id,
                source_schema=self._source_schema,
                effective_at_ms=None,
                available_at_ms=None,
                observed_at_ms=observed,
                availability_status="OBSERVED",
                evidence_ref=self._evidence_ref,
                error=type(exc).__name__ + ": " + str(exc),
            )


class DataFunction(Protocol):
    """Business data contract executed outside the reducer."""

    def execute(self, context: DataContext, request: DataRequest) -> DataResult:
        ...


class TemporalDataGuard:
    """Reject data that was not observed by the evaluation knowledge cut-off.

    A provider query proves when this process observed a result, not when the
    upstream source first published it.  When historical ``available_at_ms``
    evidence is absent, an explicitly pre-observed result is still usable for
    a later node because ``observed_at_ms`` is the strongest available runtime
    evidence.  It must never be used to infer or populate ``available_at_ms``.
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
        if result.observed_at_ms > request.knowledge_as_of_ms:
            reasons.append("observed_at_after_knowledge_cutoff")
        if (
            result.available_at_ms is not None
            and result.available_at_ms > request.knowledge_as_of_ms
        ):
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

    def __init__(self, provider: PreviousDayStatsProvider) -> None:
        self._provider = provider

    def execute(self, context: DataContext, request: DataRequest) -> DataResult:
        if request.function_id != self.function_id:
            raise ValueError(
                "request function_id must be %s" % self.function_id
            )
        expected = context.expected_previous_trade_date
        if expected is None:
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
                missing_fields=("expected_previous_trade_date",),
            )
        physical = self._provider.fetch(
            request,
            previous_trade_date=expected,
        )
        result = _data_result_from_provider(request, physical)
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
        if "previous_trade_date" not in result.data:
            return replace(result, status=DataStatus.INVALID)
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
        guarded = TemporalDataGuard.check(result, request)
        if guarded.status is not DataStatus.READY:
            reason = ",".join(guarded.missing_fields) or guarded.status.value
            raise ValueError("result is not ready for request: " + reason)
        self._entries[self._key(request, guarded)] = guarded

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
            if guarded.status is DataStatus.READY:
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

    def __init__(self, values_by_trade_date: Mapping[str, Mapping[str, Any]]) -> None:
        self._values_by_trade_date = values_by_trade_date

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
                observed_at_ms=request.effective_as_of_ms,
                availability_status="UNKNOWN",
            )
        payload = dict(values)
        observed = request.effective_as_of_ms
        return ProviderResult(
            raw_data=payload,
            source_id="fixture",
            source_schema="PreviousDayStatsV1",
            effective_at_ms=observed,
            available_at_ms=observed,
            observed_at_ms=observed,
            availability_status="VERIFIED",
            evidence_ref="fixture/previous_day_stats",
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
