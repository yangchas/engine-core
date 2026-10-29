"""Pure normalization of Kaipan ``getHisBans`` rows plus ``getBanReasons`` evidence (C611)."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

PARSER_VERSION = "c611-theme-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYMBOL = re.compile(r"^\d{6}$")

# Position types observed in captured raw Kaipan responses. Empty strings normalize to NULL, while zero stays zero.
_RAW_KINDS = {
    2: "int", 3: "str", 5: "str", 6: "int", 7: "int", 8: "int", 9: "int", 10: "int",
    13: "int", 14: "float", 16: "int", 17: "float", 18: "str", 20: "int",
}


class LimitUpDetailInputError(ValueError):
    """A source response does not meet the request-date or positional contract."""


def _pool_rows(response: Mapping[str, Any]) -> list[list[Any]]:
    info = response.get("info")
    if not isinstance(info, (list, tuple)):
        raise LimitUpDetailInputError("kaipan_pool_info_missing")
    rows: list[list[Any]] = []
    for value in info:
        if not isinstance(value, (list, tuple)):
            continue
        if len(value) == 23 and not any(isinstance(item, (list, tuple, dict)) for item in value):
            rows.append(list(value))
        else:
            rows.extend(list(item) for item in value if isinstance(item, (list, tuple)))
    return rows


def _date_echo(response: Mapping[str, Any]) -> list[str]:
    info = response.get("info")
    if not isinstance(info, (list, tuple)):
        return []
    return [str(value) for value in info if isinstance(value, str) and _DATE.fullmatch(value)]


def _raw_value(value: Any, position: int) -> Any:
    if value is None or value == "":
        return None
    kind = _RAW_KINDS[position]
    if kind == "str":
        return str(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise LimitUpDetailInputError(f"kaipan_position_type_invalid:{position}")
    if kind == "int":
        if int(value) != value:
            raise LimitUpDetailInputError(f"kaipan_position_type_invalid:{position}")
        return int(value)
    return float(value)


def _number(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise LimitUpDetailInputError("kaipan_high_confidence_number_invalid:" + label)
    return float(value)


def _lb_days(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            or int(value) != value or int(value) < 1):
        raise LimitUpDetailInputError("kaipan_high_confidence_lb_days_invalid")
    return int(value)


def _reason_records(response: Any) -> list[Mapping[str, Any]]:
    if isinstance(response, Mapping):
        value = response.get("List", response.get("list", response.get("info", [])))
    else:
        value = response
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _codes(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


def _reason_for_day(symbol: str, trade_date: str, response: Any) -> dict[str, Any]:
    if response is None:
        return {"status": "REASON_NOT_FETCHED", "reason_text_raw": None, "theme": None,
                "reason_zscodes_json": None, "main_reason_plate_code": None, "reason_source_date": None}
    if isinstance(response, Mapping) and response.get("StockID"):
        response_symbol = str(response["StockID"]).zfill(6)
        if response_symbol != symbol:
            raise LimitUpDetailInputError("kaipan_reason_symbol_mismatch")
    eligible = [item for item in _reason_records(response)
                if str(item.get("Type", "")) == "0"
                and _DATE.fullmatch(str(item.get("Date", "")))
                and str(item.get("Date", "")) <= trade_date]
    exact = [item for item in eligible if str(item.get("Date", "")) == trade_date]
    historical = not exact
    selected_date = max((str(item.get("Date", "")) for item in eligible), default="") if historical else trade_date
    selected = [item for item in eligible if str(item.get("Date", "")) == selected_date]
    if not selected:
        return {"status": "NO_ELIGIBLE_DATE_REASON", "reason_text_raw": None, "theme": None,
                "reason_zscodes_json": None, "main_reason_plate_code": None, "reason_source_date": None}
    signatures = {(str(item.get("Reason") or ""), tuple(_codes(item.get("ZSCode"))),
                   str(item.get("PZSCode") or "")) for item in selected}
    if len(signatures) != 1:
        raise LimitUpDetailInputError("kaipan_reason_same_day_conflict")
    item = selected[0]
    reason = item.get("Reason")
    reason = str(reason) if reason not in (None, "") else None
    separators = [position for marker in (";", "；") if reason and (position := reason.find(marker)) >= 0]
    theme = reason[:min(separators)] if separators else reason
    zscodes = _codes(item.get("ZSCode"))
    pzscode = str(item.get("PZSCode") or "").strip()
    main_code = pzscode if pzscode and pzscode != "0" else (zscodes[0] if zscodes else None)
    return {"status": "HISTORICAL_ATTRIBUTION" if historical else "MATCHED",
            "reason_text_raw": reason, "theme": theme,
            "reason_zscodes_json": json.dumps(zscodes, ensure_ascii=False, separators=(",", ":")),
            "main_reason_plate_code": main_code, "reason_source_date": str(item.get("Date"))}


def build_limit_up_detail_rows(trade_date: str, pool_responses: Sequence[Mapping[str, Any]],
                               reason_responses: Mapping[str, Any], *, captured_at: str,
                               reason_captured_at: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Normalize real raw API responses; a mismatched/missing getHisBans date echo is rejected."""
    if not _DATE.fullmatch(str(trade_date)):
        raise LimitUpDetailInputError("trade_date_invalid")
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
    except ValueError as exc:
        raise LimitUpDetailInputError("trade_date_invalid") from exc
    if not pool_responses:
        raise LimitUpDetailInputError("kaipan_pool_response_missing")
    raw_by_symbol: dict[str, list[Any]] = {}
    for response in pool_responses:
        if not isinstance(response, Mapping) or str(response.get("errcode", "0")) != "0":
            raise LimitUpDetailInputError("kaipan_pool_response_error")
        echoes = _date_echo(response)
        if not echoes:
            raise LimitUpDetailInputError("kaipan_pool_date_echo_missing")
        if any(day != trade_date for day in echoes):
            raise LimitUpDetailInputError("kaipan_pool_date_echo_mismatch")
        for raw in _pool_rows(response):
            if len(raw) != 23:
                raise LimitUpDetailInputError("kaipan_pool_row_width_invalid")
            symbol = str(raw[0] or "").strip()
            if not _SYMBOL.fullmatch(symbol):
                continue
            previous = raw_by_symbol.get(symbol)
            if previous is not None and previous != list(raw):
                raise LimitUpDetailInputError("kaipan_pool_duplicate_symbol_conflict")
            raw_by_symbol[symbol] = list(raw)
    if not raw_by_symbol:
        return []

    captured = str(captured_at)
    rows: list[dict[str, Any]] = []
    for symbol, raw in sorted(raw_by_symbol.items()):
        name = str(raw[1]).strip() if raw[1] not in (None, "") else None
        epoch_value = raw[4]
        if epoch_value in (None, ""):
            epoch = None
            first_seal_time = None
        elif isinstance(epoch_value, bool) or not isinstance(epoch_value, (int, float)) or \
                not math.isfinite(float(epoch_value)) or int(epoch_value) != epoch_value:
            raise LimitUpDetailInputError("kaipan_high_confidence_epoch_invalid")
        else:
            epoch = int(epoch_value)
            try:
                first_seal_time = datetime.fromtimestamp(epoch, timezone.utc).astimezone(SHANGHAI).strftime("%H:%M:%S")
            except (OverflowError, OSError, ValueError):
                raise LimitUpDetailInputError("kaipan_high_confidence_epoch_out_of_range")
        raw_reason = _reason_for_day(symbol, trade_date, reason_responses.get(symbol))
        row_quality = ("FORMAL" if raw_reason["status"] == "MATCHED" else
                       "DEGRADED" if raw_reason["status"] == "HISTORICAL_ATTRIBUTION" else "PARTIAL")
        row: dict[str, Any] = {
            "trade_date": trade_date, "symbol": symbol, "name": name,
            "kp_02_raw": _raw_value(raw[2], 2), "kp_03_raw": _raw_value(raw[3], 3),
            "limit_time_epoch": epoch, "first_seal_time": first_seal_time,
            "kp_05_raw": _raw_value(raw[5], 5), "kp_06_raw": _raw_value(raw[6], 6),
            "kp_07_raw": _raw_value(raw[7], 7), "kp_08_raw": _raw_value(raw[8], 8),
            "kp_09_raw": _raw_value(raw[9], 9), "kp_10_raw": _raw_value(raw[10], 10),
            "trade_amount_yuan": _number(raw[11], "trade_amount_yuan"),
            "reason_plates_text": str(raw[12]) if raw[12] not in (None, "") else None,
            "kp_13_raw": _raw_value(raw[13], 13), "kp_14_raw": _raw_value(raw[14], 14),
            "lb_days": _lb_days(raw[15]),
            "kp_16_raw": _raw_value(raw[16], 16), "kp_17_raw": _raw_value(raw[17], 17),
            "kp_18_raw": _raw_value(raw[18], 18),
            "pool_plate_code": str(raw[19]).strip() if raw[19] not in (None, "") else None,
            "kp_20_raw": _raw_value(raw[20], 20), "price": _number(raw[21], "price"),
            "pct_chg": _number(raw[22], "pct_chg"),
            # Candidate positions remain raw-only until C610's pending semantics are independently verified.
            "close_pct": None, "seal_time_text": None, "reason_category": None, "open_count": None,
            "seal_amount_yuan": None, "max_seal_amount_yuan": None, "main_net_yuan": None,
            "main_buy_yuan": None, "turnover_yuan": None, "main_sell_signed_yuan": None,
            "actual_float_mktcap_yuan": None, "actual_turnover_pct": None, "amplitude_pct": None,
            "streak_text": None, "daily_type": None, "plate_count_candidate": None,
            "last_seal_time": None, "streak_n_snapshot": None, "streak_source_version": None,
            "reason_text_raw": raw_reason["reason_text_raw"], "theme": raw_reason["theme"],
            "parser_version": PARSER_VERSION, "reason_zscodes_json": raw_reason["reason_zscodes_json"],
            "main_reason_plate_code": raw_reason["main_reason_plate_code"],
            "reason_source_date": raw_reason["reason_source_date"], "plate_limit_up_count": None,
            "limit_type_wencai": None, "one_word_kline": None,
            "limit_type_check": "NOT_COMPARABLE", "source_quality": row_quality,
            "source": "kaipan", "provider": "getHisBans+getBanReasons", "available_at": None,
            "source_available_at": None, "first_observed_at": captured, "captured_at": captured,
            "reason_captured_at": (reason_captured_at or {}).get(symbol),
        }
        row["quality_json"] = json.dumps(
            {"status": row["source_quality"], "reason_status": raw_reason["status"],
             "position_count": len(raw),
             "reason_source_date": row["reason_source_date"], "parser_version": PARSER_VERSION},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        rows.append(row)

    counts = Counter(row["main_reason_plate_code"] for row in rows if row["main_reason_plate_code"])
    for row in rows:
        code = row["main_reason_plate_code"]
        row["plate_limit_up_count"] = counts.get(code) if code else None
    return rows
