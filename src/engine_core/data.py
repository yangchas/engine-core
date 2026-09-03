"""Minimal query-data boundary for the first real DataFunction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Protocol, Tuple

from .contracts import (
    DataRequest,
    DataResult,
    DataStatus,
    FrozenDataBundle,
    Provenance,
    canonical_hash,
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


class DataFunction(Protocol):
    """Business data contract executed outside the reducer."""

    def execute(self, context: DataContext, request: DataRequest) -> DataResult:
        ...


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
        if result.status in (
            DataStatus.MISSING,
            DataStatus.ERROR,
            DataStatus.INVALID,
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
