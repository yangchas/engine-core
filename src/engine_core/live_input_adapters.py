"""Pure shaping of ``live_inputs`` reads into data-function results.

No I/O.  A read that is not FRESH is passed through with its state so the
consumer can see it; an EXPIRED or MISSING input yields no rows rather than a
guess.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .calendar import build_calendar_snapshot
from .contracts import DataRequest, DataResult
from .data import DataContext
from .live_inputs import EXPIRED_PREVIOUS, MISSING, LiveInputState
from .previous_day_limit_pool import (
    PreviousDayLimitPoolFunction,
    RedisPreviousDayLimitPoolProvider,
)


def json_rows_from_state(
    state: LiveInputState, *, keep_undecodable: bool = False
) -> Tuple[List[Mapping[str, Any]], int]:
    """Decode a hash read's JSON rows; ``(rows, decode_error_count)``.

    The hash field is used as ``symbol`` when the row does not carry one.
    With ``keep_undecodable`` an undecodable entry is kept as a marker row
    (``_undecodable``) so a downstream validator can reject and count it
    instead of the entry silently disappearing.
    """

    if state.read is None or state.status == MISSING or state.read.kind != "hash":
        return [], 0
    rows: List[Mapping[str, Any]] = []
    errors = 0
    for field, raw in state.read.rows:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            value.setdefault("symbol", field)
            rows.append(value)
        else:
            errors += 1
            if keep_undecodable:
                rows.append({"symbol": field, "_undecodable": True})
    return rows, errors


def previous_day_limit_pool_from_state(
    state: LiveInputState,
    *,
    trade_date: str,
    previous_trade_date: str,
    cutoff_ms: int,
    observed_at_ms: Optional[int] = None,
) -> DataResult:
    """Serve a live pool read through ``PreviousDayLimitPoolFunction``.

    An expired or missing input produces an empty row set, which the function
    reports as missing data (never as an empty-but-complete pool).
    """

    rows, _decode_errors = json_rows_from_state(state, keep_undecodable=True)
    if state.status == EXPIRED_PREVIOUS:
        rows = []
    observed = int(observed_at_ms if observed_at_ms is not None else state.last_attempt_ms)
    calendar = build_calendar_snapshot(
        (previous_trade_date, trade_date),
        version="live-two-session-v1",
        declared_valid_from=previous_trade_date,
        declared_valid_to=trade_date,
        source_guard_valid_from=previous_trade_date,
        source_guard_valid_to=trade_date,
        observed_at_ms=observed,
        evidence_ref="injected calendar: %s and %s" % (previous_trade_date, trade_date),
    )
    provider = RedisPreviousDayLimitPoolProvider(
        lambda _previous: rows,
        observed_at_ms=lambda: observed,
        evidence_ref="redis://cache:yest_limit_pool/%s#%s" % (
            previous_trade_date, state.status),
        isolate_invalid_rows=True,
    )
    request = DataRequest(
        request_id="live-previous-day-limit-pool",
        function_id="previous_day_limit_pool",
        trade_date=trade_date,
        effective_as_of_ms=cutoff_ms,
        knowledge_as_of_ms=cutoff_ms,
        temporal_mode="HISTORICAL",
        allow_unverified_previous_day_availability=True,
    )
    context = DataContext("live-previous-day-limit-pool", "live", observed, temporal_mode="HISTORICAL")
    return PreviousDayLimitPoolFunction(provider, calendar).execute(context, request)


def plate_mapping_from_states(
    stock_plate: LiveInputState, s2p: LiveInputState
) -> Mapping[str, Any]:
    """Symbol->plate text and symbol->theme list; expired/missing -> empty."""

    def usable(state: LiveInputState) -> bool:
        return state.read is not None and state.status not in (MISSING, EXPIRED_PREVIOUS)

    plate: Dict[str, str] = {}
    themes: Dict[str, List[str]] = {}
    bad = 0
    if usable(stock_plate):
        plate = {field: raw for field, raw in stock_plate.read.rows}
    if usable(s2p):
        for field, raw in s2p.read.rows:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                bad += 1
                continue
            if isinstance(value, list):
                themes[field] = [str(item) for item in value]
            else:
                bad += 1
    return {
        "stock_plate": plate,
        "s2p": themes,
        "s2p_invalid_count": bad,
        "stock_plate_status": stock_plate.status,
        "s2p_status": s2p.status,
    }


__all__ = [
    "json_rows_from_state",
    "plate_mapping_from_states",
    "previous_day_limit_pool_from_state",
]
