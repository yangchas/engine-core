"""Pure selection and attribution rules for the C581 yesterday-reason dataset."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date as _date_type
from typing import Any, Mapping, Sequence

from ..limit_reason_theme import attribute_main_reason


PROVIDER_CHAIN = (
    "kaipan_getHisBans",
    "kaipan_getBanReasons(st=200)",
    "wencai_limit_up_pool.wc_reason_category",
    "historical_attribution",
)


def _symbol(value: Any) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits.zfill(6) if digits and len(digits) <= 6 else None


def _day(value: Any) -> str:
    text = str(value or "")[:10]
    try:
        return _date_type.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _reason_codes(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    if isinstance(value, Sequence):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _map_rows(rows: Sequence[Mapping[str, Any]], *, symbol_key: str = "symbol") -> dict[str, Mapping[str, Any]]:
    result = {}
    for row in rows:
        symbol = _symbol(row.get(symbol_key, row.get("code")))
        if symbol and symbol not in result:
            result[symbol] = row
    return result


def build_limit_reasons(trade_date: str, pool_rows: Sequence[Mapping[str, Any]], *,
                        kaipan_rows: Sequence[Mapping[str, Any]] = (),
                        wencai_rows: Sequence[Mapping[str, Any]] = (),
                        plate_registry_rows: Sequence[Mapping[str, Any]] = (),
                        plate_member_rows: Sequence[Mapping[str, Any]] = (),
                        plate_members_loaded: bool | None = None,
                        plate_registry_loaded: bool | None = None,
                        pool_source: str | None = None) -> dict[str, Any]:
    """Build one row per yesterday-pool member with explicit source, lookahead, and identity status.

    ``trade_date`` is the date described by the pool. Rows dated after it are never eligible. Thus a D premarket
    run whose partition is D-1 can only consume reason dates strictly before D.
    """
    day = _day(trade_date)
    if not day:
        raise ValueError("limit_reasons_bad_trade_date")
    pool = _map_rows(pool_rows)
    if not pool:
        raise ValueError("limit_reasons_pool_required")
    reasons_by_symbol: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in kaipan_rows:
        symbol = _symbol(row.get("symbol", row.get("code")))
        event_date = _day(row.get("event_date", row.get("Date")))
        kind = str(row.get("type", row.get("Type", "0")) or "0")
        if symbol in pool and event_date and event_date <= day and kind == "0":
            reasons_by_symbol[symbol].append(row)
    for values in reasons_by_symbol.values():
        values.sort(key=lambda row: _day(row.get("event_date", row.get("Date"))), reverse=True)

    wencai = _map_rows(wencai_rows)
    registry_codes = {str(row.get("plate_code", row.get("code", ""))).strip()
                      for row in plate_registry_rows
                      if row.get("plate_code", row.get("code")) not in (None, "")}
    member_pairs = {(_symbol(row.get("symbol")), str(row.get("plate_code", "")).strip())
                    for row in plate_member_rows if _symbol(row.get("symbol")) and row.get("plate_code")}
    if plate_members_loaded is None:
        plate_members_loaded = bool(plate_member_rows)
    if plate_registry_loaded is None:
        plate_registry_loaded = bool(plate_registry_rows)

    result_rows = []
    source_counts: Counter[str] = Counter()
    mismatch_codes: Counter[str] = Counter()
    for symbol, pool_row in sorted(pool.items()):
        row_pool_source = str(pool_row.get("provider", pool_row.get("source", pool_source or ""))).lower()
        kaipan_pool_source = ("kaipan" in row_pool_source or
                              (not row_pool_source and pool_row.get("reason_category") not in (None, "")))
        pool_reason = pool_row.get("reason_category")
        pool_wc_reason = pool_row.get("wc_reason_category", pool_row.get("reason_class"))
        candidates = reasons_by_symbol.get(symbol, [])
        exact = next((row for row in candidates
                      if _day(row.get("event_date", row.get("Date"))) == day), None)
        historical = next((row for row in candidates
                           if _day(row.get("event_date", row.get("Date"))) < day), None)

        if kaipan_pool_source and pool_reason not in (None, ""):
            record = {"reason": pool_reason, "plate_code": pool_row.get("plate_code")}
            source, event_date, detail, source_status = "KAIPAN_GET_HIS_BANS", day, pool_reason, "READY"
        elif exact is not None:
            record = {"Reason": exact.get("reason", exact.get("Reason")),
                      "ZSCode": exact.get("plate_codes", exact.get("ZSCode", [])),
                      "PZSCode": exact.get("parent_plate_code", exact.get("PZSCode"))}
            source, event_date, detail, source_status = "KAIPAN_GET_BAN_REASONS", day, exact.get("reason"), "READY"
        elif pool_wc_reason not in (None, ""):
            record = {"reason": pool_wc_reason}
            source, event_date, detail, source_status = "WENCAI_LIMIT_UP_POOL", day, pool_wc_reason, "READY"
        elif symbol in wencai and wencai[symbol].get("reason_class", wencai[symbol].get("wc_reason_category")) not in (None, ""):
            wc_reason = wencai[symbol].get("reason_class", wencai[symbol].get("wc_reason_category"))
            record = {"reason": wc_reason}
            source, event_date, detail, source_status = "WENCAI_LIMIT_UP_POOL", day, wc_reason, "READY"
        elif historical is not None:
            record = {"Reason": historical.get("reason", historical.get("Reason")),
                      "ZSCode": historical.get("plate_codes", historical.get("ZSCode", [])),
                      "PZSCode": historical.get("parent_plate_code", historical.get("PZSCode"))}
            source = "HISTORICAL_ATTRIBUTION"
            event_date = _day(historical.get("event_date", historical.get("Date")))
            detail, source_status = historical.get("reason"), "DEGRADED"
        else:
            source_counts["MISSING"] += 1
            continue

        codes = _reason_codes(record.get("ZSCode", record.get("plate_codes", record.get("plate_code"))))
        if record.get("plate_code") and not codes:
            codes = [str(record["plate_code"]).strip()]
        reason_record = dict(record)
        if codes and "ZSCode" not in reason_record and "plate_codes" not in reason_record:
            reason_record["ZSCode"] = codes
        member_codes = ({code for member_symbol, code in member_pairs if member_symbol == symbol}
                        if plate_members_loaded else None)
        assigned = attribute_main_reason(reason_record,
                                         registry_codes=registry_codes if plate_registry_loaded else None,
                                         member_codes=member_codes)
        identity = ("IDENTITY_MISMATCH" if assigned["identity_mismatch_codes"] else
                    "UNVERIFIED" if assigned["plate_registry_match"] is None else
                    "REGISTERED_AND_MEMBER" if (assigned["plate_registry_match"] is True and
                                                 assigned["plate_member_match"] is True) else
                    "REGISTERED_NOT_MEMBER" if (assigned["plate_registry_match"] is True and
                                                 assigned["plate_member_match"] is False) else
                    "REGISTERED")
        mismatch_codes.update(assigned["identity_mismatch_codes"])
        source_counts[source] += 1
        result_rows.append({
            "date": day, "symbol": symbol, "event_date": event_date or None,
            "reason": detail, "main_reason": assigned["main_reason"],
            "reason_unknown": assigned["reason_unknown"],
            "plate_code": assigned["main_plate_code"],
            "plate_codes": assigned["plate_codes"],
            "parent_plate_code": assigned["parent_plate_code"],
            "plate_registry_match": assigned["plate_registry_match"],
            "plate_member_match": assigned["plate_member_match"],
            "plate_identity_status": identity,
            "source": source, "status": source_status,
        })

    missing = source_counts["MISSING"]
    historical_count = source_counts["HISTORICAL_ATTRIBUTION"]
    available = len(result_rows)
    overall = "MISSING" if not available else "DEGRADED" if missing or historical_count else "READY"
    return {
        "rows": result_rows, "status": overall, "error_code": "ALL_SOURCES_FAILED" if not available else None,
        "expected_count": len(pool), "coverage_count": available,
        "coverage": available / len(pool), "missing_count": missing,
        "historical_attribution_count": historical_count,
        "reason_unknown_count": sum(row["reason_unknown"] is True for row in result_rows),
        "source_counts": dict(source_counts), "identity_mismatch_count": sum(mismatch_codes.values()),
        "identity_mismatch_codes": dict(mismatch_codes), "provider_chain": list(PROVIDER_CHAIN),
    }
