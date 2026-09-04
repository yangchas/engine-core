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
    canonical_hash,
    semantic_hash,
)


@dataclass(frozen=True)
class DataContext:
    evaluation_id: str
    phase: str
    observed_at_ms: int
    expected_previous_trade_date: Optional[str] = None


class DataProvider(Protocol):
    """Physical source boundary; a provider never defines business semantics."""

    def fetch(self, request: DataRequest) -> DataResult:
        ...


@dataclass(frozen=True)
class ProviderResult:
    """Minimal result emitted by an existing physical access path."""

    raw_data: Any
    source_id: str
    source_schema: str
    effective_at_ms: Optional[int]
    available_at_ms: Optional[int]
    observed_at_ms: int
    availability_status: str = "UNKNOWN"
    evidence_ref: Optional[str] = None
    error: Optional[str] = None


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


class CallablePreviousDayStatsProvider:
    """Thin wrapper around a verified legacy TD/Redis access callable.

    The callable owns connection, authentication, query shape, timeout and
    retry behavior.  This class only maps its result to the engine contract.
    """

    def __init__(
        self,
        fetcher: Callable[[DataRequest], ProviderResult],
    ) -> None:
        self._fetcher = fetcher

    def fetch(self, request: DataRequest) -> DataResult:
        physical = self._fetcher(request)
        if physical.error:
            status = DataStatus.ERROR
        elif not isinstance(physical.raw_data, Mapping):
            status = DataStatus.INVALID
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
        content = {
            "function_id": request.function_id,
            "requested_trade_date": request.trade_date,
            "actual_trade_date": actual_trade_date,
            "data": payload,
            "source_id": physical.source_id,
            "source_schema": physical.source_schema,
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
            content_hash=semantic_hash(content),
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
                    notes=("availability=" + physical.availability_status,),
                ),
            ),
        )


class DataFunction(Protocol):
    """Business data contract executed outside the reducer."""

    def execute(self, context: DataContext, request: DataRequest) -> DataResult:
        ...


class TemporalDataGuard:
    """Reject data that was not available at the evaluation knowledge cut-off."""

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

    def __init__(self, provider: DataProvider) -> None:
        self._provider = provider

    def execute(self, context: DataContext, request: DataRequest) -> DataResult:
        if request.function_id != self.function_id:
            raise ValueError(
                "request function_id must be %s" % self.function_id
            )
        result = self._provider.fetch(request)
        if result.function_id != self.function_id:
            raise ValueError("provider returned an unexpected function_id")
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
        expected = context.expected_previous_trade_date
        if expected is not None and result.actual_trade_date != expected:
            return replace(result, status=DataStatus.STALE)
        if not isinstance(result.data, Mapping):
            return replace(result, status=DataStatus.INVALID)
        if "previous_trade_date" not in result.data:
            return replace(result, status=DataStatus.INVALID)
        return result


class FixturePreviousDayStatsProvider:
    """Deterministic provider used by unit tests and local replay fixtures."""

    def __init__(self, values_by_trade_date: Mapping[str, Mapping[str, Any]]) -> None:
        self._values_by_trade_date = values_by_trade_date

    def fetch(self, request: DataRequest) -> DataResult:
        values = self._values_by_trade_date.get(request.trade_date)
        if values is None:
            return DataResult(
                request_id=request.request_id,
                function_id=request.function_id,
                status=DataStatus.MISSING,
                data=None,
                actual_source="fixture",
                requested_trade_date=request.trade_date,
                actual_trade_date=None,
                effective_at_ms=None,
                available_at_ms=None,
                observed_at_ms=request.effective_as_of_ms,
                schema_version=1,
                completeness=0.0,
                content_hash=canonical_hash(
                    {
                        "function_id": request.function_id,
                        "trade_date": request.trade_date,
                        "status": DataStatus.MISSING,
                    }
                ),
            )
        payload = dict(values)
        actual_trade_date = payload.get("previous_trade_date")
        status = (
            DataStatus.READY
            if isinstance(actual_trade_date, str)
            else DataStatus.INVALID
        )
        content = {
            "function_id": request.function_id,
            "requested_trade_date": request.trade_date,
            "actual_trade_date": actual_trade_date,
            "data": payload,
        }
        observed = request.effective_as_of_ms
        return DataResult(
            request_id=request.request_id,
            function_id=request.function_id,
            status=status,
            data=payload,
            actual_source="fixture",
            requested_trade_date=request.trade_date,
            actual_trade_date=actual_trade_date,
            effective_at_ms=observed,
            available_at_ms=observed,
            observed_at_ms=observed,
            schema_version=1,
            completeness=1.0 if status is DataStatus.READY else 0.0,
            content_hash=canonical_hash(content),
            provenance=(
                Provenance(
                    source_id="fixture_previous_day_stats",
                    source_kind="fixture",
                    source_schema="PreviousDayStatsV1",
                    source_trade_date=actual_trade_date,
                    effective_at_ms=observed,
                    observed_at_ms=observed,
                ),
            ),
        )


def freeze_data_results(
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
