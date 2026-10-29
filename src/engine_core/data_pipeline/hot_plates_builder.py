"""Pure conversion of a Kaipan hot-plates response into immutable snapshot rows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class HotPlatesPartition:
    rows: list[dict[str, Any]]
    manifest: dict[str, Any]


def _aware_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("kaipan_hot_plates_captured_at_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("kaipan_hot_plates_captured_at_invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("kaipan_hot_plates_captured_at_timezone_missing")
    return parsed.isoformat()


def _response_rows(response: Mapping[str, Any]) -> list[Any]:
    for key in ("list", "List", "data", "info"):
        rows = response.get(key)
        if isinstance(rows, (list, tuple)):
            return list(rows)
    raise ValueError("kaipan_hot_plates_rows_missing")


def _response_day(response: Mapping[str, Any], trade_date: str) -> str:
    value = response.get("Day")
    values = value if isinstance(value, (list, tuple, set)) else [value]
    days = {str(item)[:10] for item in values if item not in (None, "")}
    if trade_date not in days:
        raise ValueError("kaipan_hot_plates_response_date")
    return trade_date


def _reported_total(response: Mapping[str, Any]) -> int:
    value = response.get("Total", response.get("Count"))
    try:
        total = int(value)
    except (TypeError, ValueError):
        raise ValueError("kaipan_hot_plates_total_missing") from None
    if total < 0:
        raise ValueError("kaipan_hot_plates_total_invalid")
    return total


def build_hot_plates(request: Mapping[str, Any]) -> HotPlatesPartition:
    """Map confirmed vendor positions and retain every source field without interpreting unknowns."""
    try:
        trade_date = date.fromisoformat(str(request["trade_date"])).isoformat()
    except (KeyError, TypeError, ValueError):
        raise ValueError("kaipan_hot_plates_trade_date_invalid") from None
    captured_at = _aware_time(request.get("captured_at"))
    response = request.get("response")
    if not isinstance(response, Mapping):
        raise ValueError("kaipan_hot_plates_response_shape")
    source_date = _response_day(response, trade_date)
    reported_total = _reported_total(response)
    try:
        page_size = int(request.get("page_size", 50))
    except (TypeError, ValueError):
        raise ValueError("kaipan_hot_plates_page_size_invalid") from None
    if page_size <= 0:
        raise ValueError("kaipan_hot_plates_page_size_invalid")

    page_count = int(request.get("pages_received", 1))
    if page_count < 1:
        raise ValueError("kaipan_hot_plates_pages_received_invalid")
    response_rows = _response_rows(response)[:page_size * page_count]
    rows: list[dict[str, Any]] = []
    source_time = response.get("Time")
    source_min = response.get("Min")
    source_max = response.get("Max")
    source_time = source_time if source_time is None else source_time
    source_min = source_min if source_min is None else str(source_min)
    source_max = source_max if source_max is None else str(source_max)
    for rank, raw in enumerate(response_rows, start=1):
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            raise ValueError("kaipan_hot_plates_row_shape")
        if raw[0] in (None, ""):
            raise ValueError("kaipan_hot_plates_plate_code_missing")
        rows.append({
            "plate_code": str(raw[0]),
            "name": raw[1],
            "strength": raw[2],
            "change_pct": raw[3],
            "amount_yuan": raw[5],
            "raw_fields": list(raw),
            "rank": rank,
            "date": trade_date,
            "trade_date": trade_date,
            "captured_at": captured_at,
            "available_at": captured_at,
            "event_id": request.get("event_id"),
            "source_time": source_time,
            "source_min": source_min,
            "source_max": source_max,
            "source_day": [source_date],
        })

    expected_count = reported_total
    coverage = len(rows) / expected_count if expected_count else (1.0 if not rows else 0.0)
    coverage_missing = max(0, expected_count - len(rows))
    expected_pages = (reported_total + page_size - 1) // page_size
    partial = coverage_missing > 0
    attempt = request.get("provider_attempt")
    if not isinstance(attempt, Mapping):
        attempt = {"provider": "kaipan_getHisPlates", "result": "SUCCESS",
                   "elapsed_ms": None, "reason": "single_response", "row_count": len(rows)}
    manifest = {
        "captured_at": captured_at,
        "available_at": captured_at,
        "event_id": request.get("event_id"),
        "requested_api_date": (trade_date if request.get("requested_api_date") is None
                                else request.get("requested_api_date")),
        "scheduled_at": request.get("scheduled_at"),
        "deadline": request.get("deadline"),
        "source_dates": {"kaipan_getHisPlates": source_date},
        "response_total": reported_total,
        "page_size": page_size,
        "pages_received": page_count,
        "expected_pages": expected_pages,
        "coverage_denominator": expected_count,
        "coverage_count": len(rows),
        "coverage": coverage,
        "missing_count": coverage_missing,
        "expected_basis": "response_total",
        "expected_count": expected_count,
        "field_units": {
            "plate_code": "Kaipan plate code",
            "name": "text",
            "strength": "integer; scale and normalization UNKNOWN; raw positional field [2]",
            "change_pct": "%",
            "amount_yuan": "CNY",
            "raw_fields": "raw Kaipan positional row",
            "rank": "1-based response order",
            "date": "YYYY-MM-DD",
            "trade_date": "YYYY-MM-DD",
            "captured_at": "ISO 8601 timestamp with timezone",
            "available_at": "ISO 8601 timestamp with timezone",
            "event_id": "schedule event identifier or null",
            "source_time": "UNKNOWN; raw Kaipan Time metadata",
            "source_min": "UNKNOWN; raw Kaipan Min metadata",
            "source_max": "UNKNOWN; raw Kaipan Max metadata",
            "source_day": "YYYY-MM-DD response evidence",
        },
        "response_metadata": {"Time": source_time, "Min": source_min,
                               "Max": source_max, "Day": [source_date]},
        "provider": "kaipan_getHisPlates",
        "provider_chain": ["kaipan_getHisPlates"],
        "provider_attempts": [dict(attempt)],
        "business_status": "PARTIAL" if partial else "PROVISIONAL",
        "partial": partial,
        "degraded": partial,
    }
    return HotPlatesPartition(rows=rows, manifest=manifest)
