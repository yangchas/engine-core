"""Point-in-time builder for the live ``limit_up_pool_today`` dataset (C582)."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


DATASET = "limit_up_pool_today"
SCHEMA_VERSION = 1
SHANGHAI = ZoneInfo("Asia/Shanghai")
CENT = Decimal("0.01")
ST_NAME = re.compile(r"(?:^|\*)\s*ST", re.IGNORECASE)


def normalize_symbol(value: Any) -> str | None:
    text = str(value or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else None


def board_limit_percent(symbol: str) -> int:
    """Return the exchange board price band; ST does not change the band."""
    code = normalize_symbol(symbol)
    if code is None:
        raise ValueError("symbol_invalid")
    if code.startswith(("688", "689", "30")):
        return 20
    if code.startswith(("8", "4", "92")):
        return 30
    return 10


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _local_datetime(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _is_st_name(name: str) -> bool:
    return bool(ST_NAME.search(name.strip()))


def _source_time(raw_ms: Any) -> tuple[int, str] | None:
    value = _decimal(raw_ms)
    if value is None or value <= 0 or value != value.to_integral_value():
        return None
    millis = int(value)
    try:
        stamp = datetime.fromtimestamp(millis / 1000, tz=SHANGHAI).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return None
    return millis, stamp


def build_redis_pool(trade_date: str, as_of: str | datetime,
                     symbols: Sequence[str], hashes: Mapping[str, Mapping[str, Any]], *,
                     st_names: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Derive the limit-up members from a real Q2 active-set read, excluding source values after ``as_of``.

    Q2 price fields are milli-yuan. ``br`` is deliberately never interpreted as a seal amount: evidence marks it as
    an auction residual order amount, while ``ls`` is retained only as a limit-state flag.
    """
    day = date.fromisoformat(trade_date).isoformat()
    cutoff = _local_datetime(as_of)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    names = st_names or {}
    rows: list[dict[str, Any]] = []
    counters = {"active_count": len(symbols), "missing_hash": 0, "missing_quote": 0,
                "future_quote": 0, "stale_quote": 0, "valid_quote": 0,
                "st_excluded": 0, "st_unavailable": 0}
    candidate_phases: list[str] = []
    all_timestamps_before_cutoff = True

    for raw_symbol in symbols:
        symbol = normalize_symbol(raw_symbol)
        if symbol is None:
            counters["missing_quote"] += 1
            all_timestamps_before_cutoff = False
            continue
        fields = hashes.get(str(raw_symbol)) or hashes.get(symbol)
        if not isinstance(fields, Mapping) or not fields:
            counters["missing_hash"] += 1
            all_timestamps_before_cutoff = False
            continue
        source_ts = _source_time(fields.get("ts"))
        if source_ts is None:
            counters["missing_quote"] += 1
            all_timestamps_before_cutoff = False
            continue
        timestamp_ms, source_ts_iso = source_ts
        if timestamp_ms > cutoff_ms:
            counters["future_quote"] += 1
            all_timestamps_before_cutoff = False
            continue
        if datetime.fromtimestamp(timestamp_ms / 1000, tz=SHANGHAI).date().isoformat() != day:
            counters["stale_quote"] += 1
            all_timestamps_before_cutoff = False
            continue
        px_milli, pc_milli = _decimal(fields.get("px")), _decimal(fields.get("pc"))
        if px_milli is None or pc_milli is None or px_milli <= 0 or pc_milli <= 0:
            counters["missing_quote"] += 1
            all_timestamps_before_cutoff = False
            continue
        counters["valid_quote"] += 1
        pct = (px_milli / pc_milli - Decimal(1)) * Decimal(100)
        band = board_limit_percent(symbol)
        limit_price = ((pc_milli / Decimal(1000)) *
                       (Decimal(1) + Decimal(band) / Decimal(100))).quantize(CENT, rounding=ROUND_HALF_UP)
        last_price = (px_milli / Decimal(1000)).quantize(CENT, rounding=ROUND_HALF_UP)
        if last_price != limit_price:
            continue

        phase = str(fields.get("ph", "")).strip()
        name = names.get(symbol)
        if name is None:
            st_status = "UNAVAILABLE"
            counters["st_unavailable"] += 1
        elif _is_st_name(str(name)):
            counters["st_excluded"] += 1
            continue
        else:
            st_status = "NON_ST"
        candidate_phases.append(str(fields.get("ph", "")).strip())
        rows.append({
            "symbol": symbol,
            "date": day,
            "as_of": cutoff.isoformat(timespec="milliseconds"),
            "pct": float(pct),
            "limit_up": True,
            "limit_price": float(limit_price),
            "seal_amount": None,
            "first_limit_time": None,
            "final_limit_time": None,
            "open_count": None,
            "source": "redis_q2",
            "source_ts": source_ts_iso,
            "st_status": st_status,
            "redis_phase": phase or None,
            "wencai_confirmed": False,
        })

    terminal = (cutoff.strftime("%H:%M:%S") >= "15:00:00" and bool(rows) and
                all_timestamps_before_cutoff and all(phase == "3" for phase in candidate_phases))
    if counters["valid_quote"] == 0:
        raise ValueError("q2_snapshot_has_no_point_in_time_quotes")
    return {"rows": sorted(rows, key=lambda row: row["symbol"]), "quality": counters,
            "redis_terminal": terminal, "as_of": cutoff.isoformat(timespec="milliseconds")}


def _record_symbol(record: Mapping[str, Any]) -> str | None:
    try:
        from ..wencai_fallback import _symbol
        return normalize_symbol(_symbol(record))
    except (ImportError, TypeError, ValueError):
        for key in ("股票代码", "代码", "symbol", "code"):
            value = normalize_symbol(record.get(key))
            if value:
                return value
    return None


def _wencai_value(record: Mapping[str, Any], names: Sequence[str], trade_date: str
                  ) -> tuple[Any, str | None, str | None]:
    expected = trade_date.replace("-", "")
    candidates: list[tuple[int, str, Any, str | None]] = []
    for header, value in record.items():
        text = str(header).strip()
        normalized = text.lower().replace(" ", "")
        if not any(name.lower().replace(" ", "") in normalized for name in names):
            continue
        suffix = re.search(r"\[(\d{8})\]$", text)
        date_suffix = suffix.group(1) if suffix else None
        if date_suffix and date_suffix != expected:
            continue
        if not date_suffix and any(token in normalized for token in ("最新", "现价")):
            continue
        priority = 0 if date_suffix == expected else 1 if date_suffix is None else 2
        candidates.append((priority, text, value, date_suffix))
    if not candidates:
        return None, None, None
    _, header, value, suffix = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    return value, suffix, header


def _number_yuan(value: Any, header: str | None) -> float | None:
    number = _decimal(value)
    if number is None:
        return None
    unit = (header or "").lower()
    if "亿元" in unit:
        number *= Decimal(100_000_000)
    elif "万元" in unit:
        number *= Decimal(10_000)
    elif "封单额" not in unit and "元" not in unit:
        return None
    return float(number)


def _percent(value: Any) -> Decimal | None:
    if isinstance(value, str):
        value = value.strip().removesuffix("%").strip()
    return _decimal(value)


def parse_wencai_pool(records: Sequence[Mapping[str, Any]], trade_date: str) -> dict[str, Any]:
    """Normalize one bounded WenCai response while retaining dated-column evidence."""
    normalized: dict[str, dict[str, Any]] = {}
    suffixes: set[str] = set()
    for record in records:
        symbol = _record_symbol(record)
        if symbol is None:
            continue
        seal, seal_suffix, seal_header = _wencai_value(record, ("封单额", "封单金额"), trade_date)
        open_count, open_suffix, _ = _wencai_value(record, ("开板次数", "炸板次数"), trade_date)
        first_time, first_suffix, _ = _wencai_value(record, ("首次涨停时间", "首次封板时间"), trade_date)
        final_time, final_suffix, _ = _wencai_value(record, ("最终涨停时间", "最后涨停时间", "最终封板时间"), trade_date)
        pct_value, pct_suffix, _ = _wencai_value(record, ("涨跌幅", "涨幅"), trade_date)
        selected_suffixes = {value for value in (seal_suffix, open_suffix, first_suffix, final_suffix, pct_suffix) if value}
        suffixes.update(selected_suffixes)
        seal_amount = _number_yuan(seal, seal_header)
        count_decimal = _decimal(open_count)
        pct_decimal = _percent(pct_value)
        normalized[symbol] = {
            "symbol": symbol,
            "seal_amount": seal_amount,
            "open_count": (int(count_decimal) if count_decimal is not None and
                           count_decimal == count_decimal.to_integral_value() else None),
            "first_limit_time": str(first_time).strip() or None if first_time not in (None, "") else None,
            "final_limit_time": str(final_time).strip() or None if final_time not in (None, "") else None,
            "pct": float(pct_decimal) if pct_decimal is not None else None,
            "wencai_date_suffixes": sorted(selected_suffixes),
        }
    return {"rows": [normalized[symbol] for symbol in sorted(normalized)],
            "date_suffixes": sorted(suffixes), "date_suffix_verified": suffixes == {trade_date.replace("-", "")}}


def merge_confirmations(redis_rows: Sequence[Mapping[str, Any]] | None,
                        wencai_rows: Sequence[Mapping[str, Any]], trade_date: str,
                        as_of: str | datetime) -> dict[str, Any]:
    """Join the low-frequency WenCai confirmation to the Redis snapshot; preserve one-sided members."""
    cutoff = _local_datetime(as_of).isoformat(timespec="milliseconds")
    redis_by_symbol = {str(row["symbol"]): dict(row) for row in (redis_rows or [])}
    wencai_by_symbol = {str(row["symbol"]): dict(row) for row in wencai_rows}
    output: list[dict[str, Any]] = []
    for symbol in sorted(redis_by_symbol.keys() | wencai_by_symbol.keys()):
        q2 = redis_by_symbol.get(symbol)
        wc = wencai_by_symbol.get(symbol)
        if q2 is not None:
            merged = dict(q2)
            merged["as_of"] = cutoff
            if wc is not None:
                for key in ("seal_amount", "open_count", "first_limit_time", "final_limit_time"):
                    if wc.get(key) is not None:
                        merged[key] = wc[key]
                merged["source"] = "redis_q2+wencai"
                merged["wencai_confirmed"] = True
                if merged.get("st_status") == "UNAVAILABLE":
                    merged["st_status"] = "NON_ST"
            else:
                merged["source"] = "redis_q2"
                merged["wencai_confirmed"] = False
        else:
            merged = {"symbol": symbol, "date": trade_date, "as_of": cutoff,
                      "pct": wc.get("pct") if wc else None, "limit_up": True, "limit_price": None,
                      "seal_amount": wc.get("seal_amount") if wc else None,
                      "first_limit_time": wc.get("first_limit_time") if wc else None,
                      "final_limit_time": wc.get("final_limit_time") if wc else None,
                      "open_count": wc.get("open_count") if wc else None, "source": "wencai",
                      "source_ts": None, "st_status": "NON_ST", "redis_phase": None,
                      "wencai_confirmed": True}
        output.append(merged)
    redis_set, wencai_set = set(redis_by_symbol), set(wencai_by_symbol)
    return {"rows": output, "redis_only": sorted(redis_set - wencai_set),
            "wencai_only": sorted(wencai_set - redis_set),
            "confirmed": bool(redis_rows is not None) and redis_set == wencai_set}


def derive_daily_kline_limit_up(rows: Sequence[Mapping[str, Any]], trade_date: str,
                               *, st_names: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Independently derive the close set from daily K-line prices for cross-source verification."""
    names = st_names or {}
    output: set[str] = set()
    unavailable_st = 0
    for row in rows:
        symbol = normalize_symbol(row.get("symbol", row.get("code")))
        if symbol is None:
            continue
        name = names.get(symbol)
        if name is None:
            unavailable_st += 1
        elif _is_st_name(str(name)):
            continue
        close, pre_close = _decimal(row.get("close")), _decimal(row.get("pre_close"))
        if close is None or pre_close is None or close <= 0 or pre_close <= 0:
            continue
        limit_price = (pre_close * (Decimal(1) + Decimal(board_limit_percent(symbol)) / 100)).quantize(
            CENT, rounding=ROUND_HALF_UP)
        if close.quantize(CENT, rounding=ROUND_HALF_UP) == limit_price:
            output.add(symbol)
    return {"symbols": sorted(output), "st_filter": "UNAVAILABLE" if unavailable_st else "AVAILABLE",
            "date": date.fromisoformat(trade_date).isoformat()}
