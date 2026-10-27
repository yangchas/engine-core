"""Bounded, read-only WenCai adapter for historical daily K-line rows."""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable, Mapping


PAGE_SIZE = 1000
MAX_PAGES = 60
_FIELDS = "开盘价，最高价，最低价，收盘价，前收盘价，涨跌幅，成交量(股)，成交额(元)，股票代码"
_DATE_FIELDS = ("open", "high", "low", "close", "pre_close", "pct_chg", "volume", "amount_yuan")


def build_daily_kline_query(trade_date: str) -> str:
    """Use a concise date-led query; verbose market prose can select latest-day columns."""
    day = date.fromisoformat(trade_date).isoformat()
    return f"{day}{_FIELDS}"


def _records(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, Mapping):
        if "rows" in result:
            return _records(result["rows"])
        if "data" in result:
            return _records(result["data"])
        return [dict(result)]
    if hasattr(result, "to_dict"):
        try:
            return [dict(row) for row in result.to_dict(orient="records") if isinstance(row, Mapping)]
        except (TypeError, ValueError):
            return []
    try:
        return [dict(row) for row in result if isinstance(row, Mapping)]
    except TypeError:
        return []


def _field_value(row: Mapping[str, Any], field: str, trade_date: str) -> Any:
    """Select the requested day's dated source column, ignoring undated latest fields."""
    expected = trade_date.replace("-", "")
    candidates = []
    for header, value in row.items():
        if _field_for_header(header) != field:
            continue
        match = re.search(r"\[(\d{8})\]$", str(header))
        if match:
            candidates.append((match.group(1), str(header), value))
        elif not any(token in str(header) for token in ("最新", "现价")):
            candidates.append((None, str(header), value))
    dated = [item for item in candidates if item[0] == expected]
    if not dated:
        dated = [item for item in candidates if item[0] is not None]
    if not dated:
        dated = [item for item in candidates if item[0] is None]
    if not dated:
        return None

    def priority(item: tuple[str | None, str, Any]) -> tuple[int, str]:
        header = item[1]
        if field in {"open", "high", "low", "close", "pre_close"}:
            adjustment_rank = 0 if "不复权" in header else 1 if "前复权" in header else 2
        elif field == "pct_chg":
            adjustment_rank = 0 if "不复权" in header else 1 if "前复权" in header else 2
        else:
            adjustment_rank = 0
        return adjustment_rank, header

    return sorted(dated, key=priority)[0][2]


def _field_for_header(header: Any) -> str | None:
    value = re.sub(r"\[\d{8}\]$", "", str(header)).lower().replace(" ", "")
    # WenCai may append live quote fields beside the requested historical
    # columns. They do not carry a date suffix and are not date evidence.
    if any(token in value for token in ("最新", "现价")):
        return None
    if any(item in value for item in ("前收盘", "昨收", "pre_close", "preclose")):
        return "pre_close"
    aliases = {
        "open": ("开盘", "open"), "high": ("最高", "high"),
        "low": ("最低", "low"), "close": ("收盘", "close"),
        "pct_chg": ("涨跌幅", "涨幅", "pct_chg", "pctchg"),
        "volume": ("成交量", "volume"),
        "amount_yuan": ("成交额", "成交金额", "amount"),
    }
    for field, words in aliases.items():
        if any(word in value for word in words):
            return field
    return None


def _source_dates(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    found: dict[str, set[str]] = {}
    missing: set[str] = set()
    all_dates: set[str] = set()
    for row in rows:
        for header in row:
            match = re.search(r"\[(\d{8})\]$", str(header))
            field = _field_for_header(header)
            if field and match:
                value = f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:]}"
                found.setdefault(field, set()).add(value)
                all_dates.add(value)
            elif field:
                missing.add(field)
    source_dates = {
        field: (None if field in missing or not found.get(field) else
                sorted(found[field])[0] if len(found[field]) == 1 else sorted(found[field]))
        for field in _DATE_FIELDS
    }
    return source_dates, sorted(all_dates)


def _source_units(rows: list[dict[str, Any]]) -> dict[str, str]:
    headers = [str(key).lower().replace(" ", "") for row in rows for key in row]
    volume = "shares" if "(股)" in _FIELDS else "UNKNOWN"
    amount = "yuan" if "(元)" in _FIELDS else "UNKNOWN"
    for header in headers:
        if _field_for_header(header) == "volume":
            if "万手" in header:
                volume = "wan_hands"
            elif "手" in header:
                volume = "hands"
            elif "万股" in header:
                volume = "wan_shares"
            elif "股" in header:
                volume = "shares"
        elif _field_for_header(header) == "amount_yuan":
            if "亿元" in header:
                amount = "yi_yuan"
            elif "万元" in header:
                amount = "wan_yuan"
            elif "元" in header:
                amount = "yuan"
    return {"price": "yuan", "pct_chg": "percent", "amount_yuan": amount,
            "volume": volume}


def fetch_daily_kline(trade_date: str, context: Any = None, *,
                      query_fn: Callable[..., Any] | None = None,
                      page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES) -> dict[str, Any]:
    """Fetch all bounded pages for one dated query; each page consumes a request."""
    if page_size < 1 or max_pages < 1:
        raise ValueError("invalid_wencai_pagination_bounds")
    if query_fn is None:
        from pywencai.wencai import get_robot_data
        query_fn = get_robot_data
    question = build_daily_kline_query(trade_date)
    rows: list[dict[str, Any]] = []
    page_count = 0
    total_count: int | None = None
    page_error: Exception | None = None
    for page in range(1, max_pages + 1):
        try:
            if page > 1 and context is not None:
                context.request()
            result = query_fn(query=question, page=page, perpage=page_size,
                              query_type="stock", sort_key="股票代码", sort_order="asc",
                              retry=0, strict=True)
        except Exception as exc:
            if not rows:
                raise
            page_error = exc
            break
        page_rows = _records(result)
        page_count += 1
        rows.extend(page_rows)
        if isinstance(result, Mapping):
            try:
                total_count = int(result.get("row_count")) if result.get("row_count") is not None else total_count
            except (TypeError, ValueError):
                pass
        expected_pages = (max(1, (total_count + page_size - 1) // page_size)
                          if total_count is not None else None)
        if not page_rows or (expected_pages is not None and page >= expected_pages):
            break
        if expected_pages is None and len(page_rows) < page_size:
            break
    source_dates, source_date_values = _source_dates(rows)
    payload: dict[str, Any] = {
        "rows": rows,
        "expected_count": None,
        "page_count": page_count,
        "query": question,
        "source_dates": source_dates,
        "source_date_values": source_date_values,
        "source_dates_consistent": (bool(source_date_values) and len(source_date_values) == 1 and
                                    all(source_dates.get(field) is not None for field in _DATE_FIELDS)),
        "field_units": _source_units(rows),
    }
    if total_count is not None:
        payload["expected_count"] = total_count
        payload["source_row_count"] = total_count
        payload["partial"] = len(rows) < total_count
    if page_error is not None:
        timed_out = isinstance(page_error, TimeoutError) or re.search(
            r"TIMEOUT|TIMED OUT|READ TIMEOUT", str(page_error), re.I)
        payload.update({"partial": True,
                        "error_code": "UPSTREAM_TIMEOUT" if timed_out else "TIER_PARTIAL",
                        "retryable": True,
                        "reason": f"page_fetch_failed:{type(page_error).__name__}"})
    if not rows:
        payload.update({"status": "TIER_NO_DATA", "error_code": "TIER_NO_DATA",
                        "retryable": False, "reason": "empty_response"})
    return payload
