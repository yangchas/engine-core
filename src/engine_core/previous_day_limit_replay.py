"""Build a guarded previous-day limit-pool result from a sealed artifact.

Pure shaping for replay/shadow use: the artifact's rows (as produced by the
read-only pool reader) are served through ``PreviousDayLimitPoolFunction`` with
the opt-in previous-day availability exception, so the result is ``PARTIAL``
with an explicit marker rather than ``UNAVAILABLE`` when ``available_at`` is
unknown.  Same-day or later data is still rejected by the temporal guard.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .calendar import build_calendar_snapshot
from .contracts import DataRequest, DataResult
from .data import DataContext, ProviderResult
from .previous_day_limit_pool import PreviousDayLimitPoolFunction


class _SealedPoolProvider:
    def __init__(self, payload: Mapping[str, Any], *, observed_at_ms: int, evidence_ref: str) -> None:
        self._payload = payload
        self._observed_at_ms = observed_at_ms
        self._evidence_ref = evidence_ref

    def fetch(self, request: DataRequest, *, previous_trade_date: str) -> ProviderResult:
        if previous_trade_date != self._payload.get("previous_trade_date"):
            raise ValueError("calendar-derived previous trade date mismatch")
        return ProviderResult(
            raw_data=self._payload,
            source_id="sealed_previous_day_limit_pool_json",
            source_schema="PreviousDayLimitPoolV1",
            effective_at_ms=None,
            available_at_ms=None,
            observed_at_ms=self._observed_at_ms,
            availability_status="OBSERVED",
            evidence_ref=self._evidence_ref,
        )


def previous_day_limit_pool_from_artifact(
    artifact: Mapping[str, Any],
    *,
    trade_date: str,
    cutoff_ms: int,
    evidence_ref: str = "sealed_previous_day_limit_pool_json",
    observed_at_ms: Optional[int] = None,
) -> DataResult:
    data = artifact["data"]
    rows = tuple(data["rows"])
    previous_trade_date = str(artifact["actual_trade_date"])
    payload = dict(data)
    payload.update(
        previous_trade_date=previous_trade_date,
        rows=rows,
        by_symbol={str(row["symbol"]): row for row in rows},
        row_count=len(rows),
    )
    observed = int(observed_at_ms if observed_at_ms is not None else artifact["observed_at_ms"])
    calendar = build_calendar_snapshot(
        (previous_trade_date, trade_date),
        version="sealed-two-session-v1",
        declared_valid_from=previous_trade_date,
        declared_valid_to=trade_date,
        source_guard_valid_from=previous_trade_date,
        source_guard_valid_to=trade_date,
        observed_at_ms=observed,
        evidence_ref="injected calendar: %s and %s" % (previous_trade_date, trade_date),
    )
    request = DataRequest(
        request_id="replay-previous-day-limit-pool",
        function_id="previous_day_limit_pool",
        trade_date=trade_date,
        effective_as_of_ms=cutoff_ms,
        knowledge_as_of_ms=cutoff_ms,
        temporal_mode="HISTORICAL",
        allow_unverified_previous_day_availability=True,
    )
    context = DataContext(
        evaluation_id="replay-previous-day-limit-pool",
        phase="replay",
        observed_at_ms=observed,
        temporal_mode="HISTORICAL",
    )
    provider = _SealedPoolProvider(payload, observed_at_ms=observed, evidence_ref=evidence_ref)
    return PreviousDayLimitPoolFunction(provider, calendar).execute(context, request)


__all__ = ["previous_day_limit_pool_from_artifact"]
