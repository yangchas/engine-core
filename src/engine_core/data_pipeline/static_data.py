"""Pure builders and clock policy for immutable static-data snapshots.

Provider access and artifact writes belong in callers.  These functions only
normalize supplied source rows and report the evidence gaps they can observe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class StaticPartition:
    rows: list[dict[str, Any]]
    manifest: dict[str, Any]


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("as_of must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("as_of must be a timezone-aware ISO timestamp")
    return parsed


def _identity_date(trade_date: str, as_of: str) -> tuple[str, str]:
    if not isinstance(trade_date, str) or not _DATE_RE.fullmatch(trade_date):
        raise ValueError("trade_date must be YYYY-MM-DD")
    _timestamp(as_of)
    return trade_date, as_of


def _manifest(*, dataset: str, trade_date: str, as_of: str, provider: str,
              source_dates: Mapping[str, Any], row_count: int, quality: Mapping[str, Any],
              field_units: Mapping[str, str], provider_attempts: list[dict[str, Any]],
              business_status: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset": dataset,
        "trade_date": trade_date,
        "source": provider,
        "provider": provider,
        "provider_chain": [provider],
        "provider_attempts": provider_attempts,
        "as_of": as_of,
        "fetched_at": as_of,
        # ArtifactCatalog.commit replaces this with the measured commit time.
        "available_at": as_of,
        "business_status": business_status,
        "degraded": business_status in {"DEGRADED", "PARTIAL"},
        "row_count": row_count,
        "quality": dict(quality),
        "field_units": dict(field_units),
        "source_dates": dict(source_dates),
        # Make each retrieval an immutable snapshot version even when the source
        # returns byte-for-byte identical rows on a later refresh.
        "inputs": {"snapshot_as_of": as_of, "source_dates": dict(source_dates)},
    }


def _attempt(provider: str, result: str, row_count: int, reason: str = "") -> dict[str, Any]:
    return {"provider": provider, "result": result, "elapsed_ms": None,
            "reason": reason, "row_count": row_count}


def _field_completeness(rows: list[Mapping[str, Any]], fields: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in fields:
        count = sum(row.get(field) is not None for row in rows)
        result[field] = ("COMPLETE" if count == len(rows) else "MISSING" if count == 0 else "PARTIAL")
    return result


def build_plate_registry(source_rows: Iterable[Mapping[str, Any]], *, trade_date: str,
                         as_of: str, source_dates: Mapping[str, Any],
                         provider: str = "kaipan_registry") -> StaticPartition:
    """Normalize a complete registry snapshot; code identity is never name-derived."""
    trade_date, as_of = _identity_date(trade_date, as_of)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in source_rows:
        code = str(source.get("plate_code") or source.get("code") or "").strip()
        if not code:
            raise ValueError("plate_registry identity missing")
        if code in seen:
            raise ValueError("plate_registry duplicate identity")
        seen.add(code)
        size = source.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("plate_registry size missing or invalid")
        rows.append({
            "code": code,
            "plate_code": code,
            "name": source.get("name"),
            "class": str(source.get("class") or "UNKNOWN"),
            "family": str(source.get("family") or code),
            "parent": source.get("parent"),
            "children": list(source.get("children") or []),
            "flags": list(source.get("flags") or []),
            "generic_factor": bool(source.get("generic_factor", False)),
            "size": size,
            "pit_date": source.get("pit_date"),
            "available_at": str(source.get("available_at") or as_of),
            "date": trade_date,
        })
    if not rows:
        raise ValueError("plate_registry_empty")
    rows.sort(key=lambda row: row["plate_code"])
    complete = _field_completeness(rows, ("name", "parent"))
    quality = {"coverage": None, "expected_basis": "none",
               "expected_count": len(rows), "missing": 0, "field_completeness": complete,
               "partial": False, "status": "READY"}
    manifest = _manifest(
        dataset="plate_registry", trade_date=trade_date, as_of=as_of, provider=provider,
        source_dates=source_dates, row_count=len(rows), quality=quality,
        field_units={}, provider_attempts=[_attempt(provider, "ACCEPTED", len(rows))],
        business_status="FORMAL",
    )
    return StaticPartition(rows, manifest)


def build_plate_members(source_rows: Iterable[Mapping[str, Any]], registry_rows: Iterable[Any], *,
                        trade_date: str, as_of: str, source_dates: Mapping[str, Any],
                        provider: str = "kaipan_getHisPlateIds") -> StaticPartition:
    """Keep every valid membership row and count plate codes absent from the registry."""
    trade_date, as_of = _identity_date(trade_date, as_of)
    registry_codes = {
        str(row.get("plate_code") or row.get("code") or "").strip()
        if isinstance(row, Mapping) else str(row).strip()
        for row in registry_rows
    }
    registry_codes.discard("")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    unregistered: set[str] = set()
    for source in source_rows:
        plate_code = str(source.get("plate_code") or "").strip()
        symbol = str(source.get("symbol") or "").strip()
        if not plate_code or not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("plate_members identity missing or invalid")
        key = (plate_code, symbol)
        if key in seen:
            raise ValueError("plate_members duplicate identity")
        seen.add(key)
        source_day = str(source.get("trade_date") or trade_date)
        if source_day != trade_date:
            raise ValueError("plate_members source trade_date identity mismatch")
        if plate_code not in registry_codes:
            unregistered.add(plate_code)
        rows.append({
            "symbol": symbol,
            "trade_date": trade_date,
            "plate_code": plate_code,
            "plate_class": str(source.get("plate_class") or "UNKNOWN"),
            "plate_member_label": str(source.get("plate_member_label") or ""),
            "date": trade_date,
        })
    if not rows:
        raise ValueError("plate_members_empty")
    rows.sort(key=lambda row: (row["plate_code"], row["symbol"]))
    registry_only = registry_codes - {row["plate_code"] for row in rows}
    partial = bool(unregistered)
    quality = {
        "coverage": None,
        "expected_basis": "none",
        "expected_count": len(registry_codes),
        "missing": len(registry_only),
        "field_completeness": _field_completeness(rows, ("plate_class", "plate_member_label")),
        "partial": partial,
        "status": "PARTIAL" if partial else "READY",
        "unregistered_plate_code_count": len(unregistered),
        "unregistered_plate_code_rows": sum(row["plate_code"] in unregistered for row in rows),
        "registry_only_plate_code_count": len(registry_only),
    }
    manifest = _manifest(
        dataset="plate_members", trade_date=trade_date, as_of=as_of, provider=provider,
        source_dates=source_dates, row_count=len(rows), quality=quality, field_units={},
        provider_attempts=[_attempt(provider, "ACCEPTED", len(rows))],
        business_status="PARTIAL" if partial else "FORMAL",
    )
    return StaticPartition(rows, manifest)


def _basic_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields = payload.get("fields")
    values = payload.get("rows")
    if not isinstance(fields, list) or not isinstance(values, list):
        raise ValueError("baostock stock_basic payload shape invalid")
    result: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, Mapping):
            result.append(dict(value))
        elif isinstance(value, (list, tuple)) and len(value) == len(fields):
            result.append(dict(zip(fields, value)))
    return result


def _symbol(value: Any) -> str:
    text = str(value or "").strip()
    suffix = text.rsplit(".", 1)[-1]
    return suffix if re.fullmatch(r"\d{6}", suffix) else ""


def _market_identity(symbol: str, source_code: Any = None) -> tuple[str, str]:
    code = str(source_code or "").strip().lower()
    prefix = code.split(".", 1)[0] if "." in code else ""
    exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(prefix, "UNKNOWN")
    if symbol.startswith(("688", "689")):
        return exchange, "STAR"
    if symbol.startswith(("300", "301")):
        return exchange, "CHINEXT"
    if symbol.startswith(("4", "8", "92")):
        return exchange, "BEIJING"
    if symbol.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return exchange, "MAIN"
    return exchange, "UNKNOWN"


def _is_st(name: str | None) -> bool:
    return bool(name and re.match(r"^\s*(?:\*?ST|S\*ST)", name, flags=re.IGNORECASE))


def build_stock_static(universe_rows: Iterable[Mapping[str, Any]], basic_payload: Mapping[str, Any], *,
                       trade_date: str, as_of: str, source_dates: Mapping[str, Any],
                       provider: str = "baostock_stock_basic") -> StaticPartition:
    """Build a current reference view, retaining universe symbols outside a capped basic response."""
    trade_date, as_of = _identity_date(trade_date, as_of)
    universe: dict[str, dict[str, Any]] = {}
    for item in universe_rows:
        symbol = _symbol(item.get("symbol") or item.get("code"))
        if not symbol:
            continue
        if symbol in universe:
            raise ValueError("symbol_universe duplicate identity")
        universe[symbol] = dict(item)
    if not universe:
        raise ValueError("symbol_universe_empty")
    basic: dict[str, dict[str, Any]] = {}
    for item in _basic_records(basic_payload):
        if str(item.get("type") or "") != "1":
            continue
        symbol = _symbol(item.get("code"))
        if symbol:
            basic[symbol] = item

    rows = []
    name_mismatches = 0
    st_mismatches = 0
    basic_missing = 0
    for symbol in sorted(universe):
        urow = universe[symbol]
        brow = basic.get(symbol)
        uname = str(urow.get("name") or "").strip() or None
        bname = str((brow or {}).get("code_name") or "").strip() or None
        if uname and bname and uname != bname:
            name_mismatches += 1
            st_mismatches += _is_st(uname) != _is_st(bname)
        name = bname or uname
        if brow is None:
            basic_missing += 1
        exchange, board = _market_identity(symbol, (brow or {}).get("code") or urow.get("code"))
        ipo = str((brow or {}).get("ipoDate") or "")
        listing_date = ipo if _DATE_RE.fullmatch(ipo) else None
        rows.append({
            "symbol": symbol,
            "name": name,
            "industry": None,
            "listing_date": listing_date,
            "is_new_stock": None,
            "is_st": _is_st(name) if name is not None else None,
            "market_board": board,
            "exchange": exchange,
            "limit_width_pct": None,
            "float_cap_yuan": None,
            "total_cap_yuan": None,
            "cap_as_of": None,
            "pit_date": None,
            "source_quality": "baostock_stock_basic+symbol_universe",
            "available_at": as_of,
            "date": trade_date,
        })
    missing_listing = sum(row["listing_date"] is None for row in rows)
    partial = bool(basic_missing or missing_listing or any(row["name"] is None for row in rows))
    quality = {
        "coverage": (len(rows) / len(universe)) if universe else None,
        "expected_basis": "none",
        "expected_count": len(universe),
        "missing": max(0, len(universe) - len(rows)),
        "field_completeness": _field_completeness(
            rows, ("name", "listing_date", "is_st", "market_board", "exchange", "industry",
                   "float_cap_yuan", "total_cap_yuan")),
        "partial": partial,
        "status": "PARTIAL" if partial else "READY",
        "missing_baostock_basic_count": basic_missing,
        "missing_listing_date_count": missing_listing,
        "name_mismatch_count": name_mismatches,
        "st_mismatch_count": st_mismatches,
        "baostock_basic_row_count": len(basic),
    }
    manifest = _manifest(
        dataset="stock_static", trade_date=trade_date, as_of=as_of, provider=provider,
        source_dates=source_dates, row_count=len(rows), quality=quality,
        field_units={"limit_width_pct": "percent", "float_cap_yuan": "yuan",
                     "total_cap_yuan": "yuan"},
        provider_attempts=[_attempt(provider, "PARTIAL" if partial else "ACCEPTED", len(basic),
                                    "basic response omits some universe symbols" if basic_missing else ""),
                           _attempt("baostock_query_all_stock", "ACCEPTED", len(universe))],
        business_status="PARTIAL" if partial else "PROVISIONAL",
    )
    return StaticPartition(rows, manifest)


def static_refresh_decision(*, now: datetime, trigger_time: str,
                            last_available_at: datetime | None, max_age_days: int,
                            weekly_days: int, universe_symbols: Iterable[str],
                            snapshot_symbols: Iterable[str]) -> dict[str, Any]:
    """Evaluate weekly/new-symbol refresh and freshness alert against a fakeable clock."""
    if now.tzinfo is None or (last_available_at is not None and last_available_at.tzinfo is None):
        raise ValueError("clock values must be timezone-aware")
    try:
        hh, mm = (int(value) for value in trigger_time.split(":"))
        trigger = time(hh, mm)
    except (TypeError, ValueError) as exc:
        raise ValueError("trigger_time must be HH:MM") from exc
    local_now = now.astimezone(SHANGHAI)
    local_last = last_available_at.astimezone(SHANGHAI) if last_available_at else None
    if max_age_days < 1 or weekly_days < 1:
        raise ValueError("refresh periods must be positive")
    universe = {str(value) for value in universe_symbols}
    snapshot = {str(value) for value in snapshot_symbols}
    new_count = len(universe - snapshot)
    age_days = (local_now.date() - local_last.date()).days if local_last else None
    stale = local_last is None or age_days > max_age_days
    weekly_due = local_last is None or age_days >= weekly_days
    scheduled = local_now.timetz().replace(tzinfo=None) >= trigger
    trigger_due = weekly_due or new_count > 0
    refresh = scheduled and trigger_due
    return {"refresh": refresh, "catch_up": refresh and local_now.timetz().replace(tzinfo=None) > trigger,
            "stale_alert": stale, "weekly_due": weekly_due, "new_symbol_count": new_count,
            "age_days": age_days}


def retry_delay_seconds(attempt: int, *, base_seconds: int = 60, cap_seconds: int = 600) -> int:
    if isinstance(attempt, bool) or attempt < 1 or base_seconds < 1 or cap_seconds < base_seconds:
        raise ValueError("invalid retry policy")
    return min(cap_seconds, base_seconds * (2 ** (attempt - 1)))
