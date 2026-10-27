"""Canonical row shape and source-unit conversion for daily K-lines."""
from __future__ import annotations

import math
from typing import Any, Mapping


# C579: the canonical row is the compatible superset of every real partition column and every
# consumer-read column (docs/DAILY_KLINE.md "Consumer evidence"). pct_chg is a PERCENT float
# (1.5044 = +1.5044%) exactly like all live partitions; turnover_pct is percent and may be null.
CANONICAL_ROW_FIELDS = (
    "symbol", "code", "date", "open", "high", "low", "close", "pre_close",
    "pct_chg", "turnover_pct", "amount_yuan", "volume", "suspended",
    "source", "degraded", "tier",
)
CANONICAL_FIELD_UNITS = {
    "open": "yuan", "high": "yuan", "low": "yuan", "close": "yuan",
    "pre_close": "yuan", "pct_chg": "percent", "turnover_pct": "percent",
    "amount_yuan": "yuan", "volume": "shares",
}
TIER_BY_SOURCE = {"baostock": 0, "wencai": 1, "q2_final": 2}
SOURCE_FIELD_UNITS = {
    "baostock": {"price": "yuan", "pct_chg": "percent", "turnover_pct": "percent",
                 "amount_yuan": "yuan", "volume": "shares"},
    "wencai": {"price": "yuan", "pct_chg": "percent", "turnover_pct": "percent",
               "amount_yuan": "yuan", "volume": "shares"},
    "q2_final": {"price": "yuan", "pct_chg": "percent", "turnover_pct": "percent",
                 "amount_yuan": "yuan", "volume": "hands"},
}


def _number(value: Any) -> float | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if value in (None, "", "--", "-", "None"):
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("%", "").strip()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _unit(units: Mapping[str, Any], field: str, source: str) -> str:
    value = units.get(field)
    if value is None and field in {"open", "high", "low", "close", "pre_close"}:
        value = units.get("price")
    if value is None:
        value = SOURCE_FIELD_UNITS[source].get(field, "UNKNOWN")
    return str(value).strip().lower().replace(" ", "")


def _amount_multiplier(unit: str) -> float:
    if unit in {"yuan", "元", "rmb", "cny"}:
        return 1.0
    if unit in {"wan_yuan", "万元", "10k_yuan", "10,000yuan"}:
        return 10_000.0
    if unit in {"yi_yuan", "亿元", "100m_yuan"}:
        return 100_000_000.0
    raise ValueError("schema_invalid:amount_unit_unknown")


def _volume_multiplier(unit: str) -> float:
    if unit in {"shares", "share", "股", "股数"}:
        return 1.0
    if unit in {"hands", "hand", "手"}:
        return 100.0
    if unit in {"wan_shares", "万股", "10k_shares"}:
        return 10_000.0
    if unit in {"wan_hands", "万手", "10k_hands"}:
        return 1_000_000.0
    raise ValueError("schema_invalid:volume_unit_unknown")


def _market_code(symbol: str, raw_code: Any) -> str:
    from ..daily_kline_capture import to_baostock_code

    code = str(raw_code or "").strip().lower()
    if "." in code:
        aliases = {"sh": "sh", "sse": "sh", "ss": "sh", "sz": "sz", "szse": "sz",
                   "bj": "bj", "bse": "bj"}
        left, right = code.split(".", 1)
        if left in aliases and right.isdigit() and right.zfill(6) == symbol:
            return f"{aliases[left]}.{symbol}"
        if right in aliases and left.isdigit() and left.zfill(6) == symbol:
            return f"{aliases[right]}.{symbol}"
    if code.isdigit() and code.zfill(6) == symbol:
        return to_baostock_code(symbol)
    if not code:
        return to_baostock_code(symbol)
    raise ValueError("schema_invalid:code_symbol_mismatch")


def canonicalize_daily_kline_row(source: str, row: Mapping[str, Any], *,
                                 field_units: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Convert one source row to the registry's shared units and keys."""
    if source not in TIER_BY_SOURCE:
        raise ValueError("schema_invalid:unknown_daily_kline_source")
    units = field_units if isinstance(field_units, Mapping) else {}
    raw_symbol = row.get("symbol", row.get("code", ""))
    symbol = str(raw_symbol or "").strip()
    if "." in symbol:
        left, right = symbol.split(".", 1)
        symbol = left if left.isdigit() else right
    if symbol.isdigit():
        symbol = symbol.zfill(6)
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("schema_invalid:symbol_invalid")

    values: dict[str, float | None] = {}
    for field in ("open", "high", "low", "close", "pre_close"):
        value = row.get(field)
        if value is None and field == "pre_close":
            value = row.get("preclose", row.get("preClose"))
        values[field] = _number(value)

    pct = _number(row.get("pct_chg", row.get("pctChg")))
    pct_unit = _unit(units, "pct_chg", source)
    if pct is not None:
        if pct_unit in {"bp", "bps"}:
            pct /= 100.0
        elif pct_unit not in {"percent", "%", "百分比"}:
            raise ValueError("schema_invalid:pct_chg_unit_unknown")

    turnover = _number(row.get("turnover_pct", row.get("turnover", row.get("turn"))))
    if turnover is not None and _unit(units, "turnover_pct", source) not in {"percent", "%", "百分比"}:
        raise ValueError("schema_invalid:turnover_pct_unit_unknown")
    suspended = row.get("suspended")
    suspended = suspended if isinstance(suspended, bool) else None  # no flag from source -> null, never inferred

    amount = _number(row.get("amount_yuan", row.get("amount", row.get("成交额"))))
    if amount is not None:
        amount *= _amount_multiplier(_unit(units, "amount_yuan", source))

    volume = _number(row.get("volume", row.get("成交量")))
    if volume is not None:
        volume *= _volume_multiplier(_unit(units, "volume", source))

    tier = TIER_BY_SOURCE[source]
    return {
        "symbol": symbol,
        "code": _market_code(symbol, row.get("code")),
        "date": str(row.get("date") or ""),
        **values,
        "pct_chg": round(pct, 4) if pct is not None else None,
        "turnover_pct": round(turnover, 4) if turnover is not None else None,
        "amount_yuan": round(amount, 2) if amount is not None else None,
        "volume": float(round(volume)) if volume is not None else None,
        "suspended": suspended,
        "source": source,
        "degraded": tier > 0,
        "tier": tier,
    }


def canonicalize_daily_kline_payload(source: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize rows while retaining source units and date evidence."""
    result = dict(payload)
    conversion_units = payload.get("field_units")
    conversion_units = dict(conversion_units) if isinstance(conversion_units, Mapping) else {}
    source_units = payload.get("source_field_units")
    source_units = (dict(source_units) if isinstance(source_units, Mapping) else
                    dict(conversion_units))
    rows = payload.get("rows", [])
    if not isinstance(rows, (list, tuple)):
        raise ValueError("schema_invalid:rows_not_a_list")
    result["rows"] = [canonicalize_daily_kline_row(source, row, field_units=conversion_units or source_units)
                      for row in rows if isinstance(row, Mapping)]
    result["source_field_units"] = source_units or dict(SOURCE_FIELD_UNITS[source])
    result["field_units"] = dict(CANONICAL_FIELD_UNITS)
    result["tier"] = TIER_BY_SOURCE[source]
    result["degraded"] = TIER_BY_SOURCE[source] > 0
    result["source_dates"] = payload.get("source_dates", {})
    return result
