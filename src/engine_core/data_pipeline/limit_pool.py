"""Pure core of the ``limit_up_pool`` dataset: previous-day (D-1) limit-up pool, three sources (C580).

Contract (fw/design/24-DATASET-CONTRACT.md): no I/O, no environment, no clock.  Every function takes plain data and
returns plain data; the guardian adapter (``engine_core.guardian.limit_pool_d1``) does the fetching, the catalog
commit and the TD sync.

Source order is NOT decided here (it is the ladder in ``schedule.json``):
  * ``kaipan_getHisBans``  first choice, FORMAL, 23 positional fields per record kept in full
  * ``wencai``             "{date}涨停;连板天数;去除st", PROVISIONAL, extra columns (seal amount, open count ...)
  * ``td_derived``         derived from TD daily bars (read-only), DEGRADED, an inferred pool, not a vendor fact

Units: prices yuan, ``pct_chg`` percent, amounts yuan, ``limit_time_epoch`` unix seconds (UTC epoch, shown in
Asia/Shanghai).  ``kp_NN`` columns are the Kaipan positional values NN, meaning/unit NOT verified (kept verbatim).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 2
DATASET = "limit_up_pool"
KAIPAN, WENCAI, TD_DERIVED = "kaipan_getHisBans", "wencai", "td_derived"
PROVIDER_RANK = {KAIPAN: 0, WENCAI: 1, TD_DERIVED: 2}
PROVIDER_STATUS = {KAIPAN: "FORMAL", WENCAI: "PROVISIONAL", TD_DERIVED: "DEGRADED"}
PRIMARY_PROVIDER = KAIPAN
EXPECTED_BASIS = "pool_members"      # coverage denominator = number of members the source itself reports
KAIPAN_FULL_FIELDS = 23               # real getHisBans record width (2026-10-27 probe); the card said 16
KAIPAN_MIN_FIELDS = 16                # legacy lower bound: shorter records are noise and are dropped and counted
TD_RULE_MARGIN_PCT = 0.6              # FACTS: pct_chg >= board limit - 0.6 and close == high

# (name, kind, unit, source-of-meaning) ---------------------------------------------------------------------------------
# kind: str | int | float | bool.  ``None`` unit = unitless / text.
COMMON_COLUMNS = (
    ("symbol", "str", None, "6-digit code"),
    ("date", "str", None, "described trade date D-1 (YYYY-MM-DD), never the fetch date"),
    ("trade_date", "str", None, "legacy alias of date, kept for existing readers"),
    ("name", "str", None, "short name"),
    ("lb_days", "int", "days", "consecutive limit-up days up to and including date"),
    ("in_legacy_pool", "bool", None, "always true: kept for existing readers"),
)
VALUE_COLUMNS = (   # present for more than one source
    ("price", "float", "yuan", "close of date (Kaipan [21], TD close)"),
    ("pct_chg", "float", "percent", "close change of date (Kaipan [22], TD pct_chg)"),
)
KAIPAN_COLUMNS = (
    ("limit_time_epoch", "int", "unix_seconds", "Kaipan [4]; 2026-10-27 sample 1793064300 = 09:25:00 CST"),
    ("seal_time_text", "str", None, "Kaipan [3]; empty in every sample (null)"),
    ("reason_category", "str", None, "Kaipan [5] short limit-up reason / plate category (e.g. 商业航天)"),
    ("reason_plates_text", "str", None, "Kaipan [12] plate / theme text, '、'-separated"),
    ("plate_code", "str", None, "Kaipan [19] plate index code (e.g. 801843)"),
    ("kp_02", "float", None, "Kaipan [2] raw, unverified (always 0 in samples)"),
    ("kp_06", "float", None, "Kaipan [6] raw, unverified amount-like"),
    ("kp_07", "float", None, "Kaipan [7] raw, unverified amount-like"),
    ("kp_08", "float", None, "Kaipan [8] raw, unverified amount-like"),
    ("kp_09", "float", None, "Kaipan [9] raw, unverified amount-like"),
    ("kp_10", "float", None, "Kaipan [10] raw, equals [8]-[9] in every sample"),
    ("kp_11", "float", None, "Kaipan [11] raw, unverified amount-like"),
    ("kp_13", "float", None, "Kaipan [13] raw, unverified (cap-like magnitude)"),
    ("kp_14", "float", None, "Kaipan [14] raw, unverified"),
    ("kp_15", "float", None, "Kaipan [15] raw consecutive-limit-up days; lb_days is taken from it (ladder level if empty)"),
    ("kp_16", "float", None, "Kaipan [16] raw 0/1 flag, unverified"),
    ("kp_17", "float", None, "Kaipan [17] raw, unverified"),
    ("kp_18", "str", None, "Kaipan [18] raw text, empty in samples (null)"),
    ("kp_20", "float", None, "Kaipan [20] raw integer, unverified"),
)
KAIPAN_POSITIONS = {   # position -> column
    3: "seal_time_text", 4: "limit_time_epoch", 5: "reason_category", 12: "reason_plates_text", 19: "plate_code",
    2: "kp_02", 6: "kp_06", 7: "kp_07", 8: "kp_08", 9: "kp_09", 10: "kp_10", 11: "kp_11", 13: "kp_13", 14: "kp_14",
    15: "kp_15", 16: "kp_16", 17: "kp_17", 18: "kp_18", 20: "kp_20", 21: "price", 22: "pct_chg"}
WENCAI_COLUMNS = (
    ("wc_limit_type", "str", None, "WenCai 涨停类型 (e.g. 放量涨停)"),
    ("wc_streak_text", "str", None, "WenCai 几天几板 (e.g. 首板涨停)"),
    ("wc_first_limit_time", "str", "HH:MM:SS", "WenCai 首次涨停时间"),
    ("wc_last_limit_time", "str", "HH:MM:SS", "WenCai 最终涨停时间"),
    ("wc_open_count", "int", "count", "WenCai 涨停开板次数"),
    ("wc_seal_volume_shares", "float", "shares", "WenCai 涨停封单量"),
    ("wc_seal_amount_yuan", "float", "yuan", "WenCai 涨停封单额"),
    ("wc_seal_vol_ratio", "float", "percent_unverified", "WenCai 涨停封单量占成交量比, as reported"),
    ("wc_seal_float_ratio", "float", "percent_unverified", "WenCai 涨停封单量占流通a股比, as reported"),
    ("wc_float_mktcap_yuan", "float", "yuan", "WenCai a股市值(不含限售股) of the dated column"),
    ("wc_reason_category", "str", None, "WenCai 涨停原因类别, '+'-separated"),
)
TD_COLUMNS = (
    ("limit_pct", "float", "percent", "board limit used by the derivation (10/20/30), from the symbol prefix only"),
)
COLUMNS = COMMON_COLUMNS + VALUE_COLUMNS + KAIPAN_COLUMNS + WENCAI_COLUMNS + TD_COLUMNS
COLUMN_KIND = {name: kind for name, kind, _u, _d in COLUMNS}
FIELD_UNITS = {name: unit for name, _k, unit, _d in COLUMNS if unit}
REQUIRED_COLUMNS = tuple(name for name, *_ in COMMON_COLUMNS)

_ST_NAME = re.compile(r"^(S\*ST|\*ST|SST|ST)")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CODE = re.compile(r"(\d{6})$")


class PoolSourceError(Exception):
    """Typed source failure; ``code`` is the guardian error code, ``exit_code`` the handler exit code."""

    def __init__(self, code: str, detail: str, *, exit_code: int, retryable: bool = False):
        super().__init__(f"{code}:{detail}")
        self.code, self.detail, self.exit_code, self.retryable = code, detail, exit_code, retryable


@dataclass
class Partition:
    rows: list[dict[str, Any]]
    manifest: dict[str, Any]
    notes: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------------------------
def limit_pct(symbol: str) -> float:
    """Board limit by listing board only (688/689/30 -> 20, 8/4/92 -> 30, else 10).  There is NO ST 5% tier."""
    if symbol.startswith(("688", "689", "30")):
        return 20.0
    if symbol.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def is_st_name(name: Any) -> bool:
    return bool(_ST_NAME.match(str(name or "").strip()))


def symbol_of(value: Any) -> str | None:
    match = _CODE.search(str(value or "").strip())
    return match.group(1) if match else None


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    number = _finite(value)
    return None if number is None else int(round(number))


def _base_row(symbol: str, trade_date: str, name: Any, lb_days: int) -> dict[str, Any]:
    return {"symbol": symbol, "date": trade_date, "trade_date": trade_date, "name": _text(name),
            "lb_days": int(lb_days), "in_legacy_pool": True}


def _completeness(rows: Sequence[Mapping[str, Any]], columns: Iterable[str]) -> float:
    names = list(columns)
    if not rows or not names:
        return 0.0
    filled = sum(1 for row in rows for name in names if row.get(name) is not None)
    return round(filled / (len(rows) * len(names)), 6)


def _finish(rows: list[dict[str, Any]], provider: str, trade_date: str, *, expected_count: int,
            source_columns: Sequence[str], evidence: Mapping[str, Any], notes: Mapping[str, Any],
            extra_quality: Mapping[str, Any] | None = None) -> Partition:
    rows.sort(key=lambda row: row["symbol"])
    count = len(rows)
    missing = max(0, expected_count - count)
    quality = {"status": "READY" if provider != TD_DERIVED else "DEGRADED",
               "coverage": round(count / expected_count, 6) if expected_count else None,
               "expected_basis": EXPECTED_BASIS, "expected_count": expected_count, "missing": missing,
               "field_completeness": _completeness(rows, source_columns), "partial": missing > 0,
               "row_count": count, **dict(extra_quality or {})}
    manifest = {"schema_version": SCHEMA_VERSION, "dataset": DATASET, "trade_date": trade_date, "provider": provider,
                "source": provider, "business_status": PROVIDER_STATUS[provider],
                "degraded": provider != PRIMARY_PROVIDER or PROVIDER_STATUS[provider] == "DEGRADED",
                "row_count": count, "quality": quality, "field_units": dict(FIELD_UNITS),
                "source_dates": dict(evidence), "provider_details": dict(notes)}
    return Partition(rows, manifest, dict(notes))


# ------------------------------------------------------------------------------------------------------------------
# Tier 1: Kaipan getHisBans
# ------------------------------------------------------------------------------------------------------------------
def kaipan_row(raw: Sequence[Any], level: int, trade_date: str) -> dict[str, Any] | None:
    """One positional record -> row, or None when it is not a usable record (no 6-digit code / too short)."""
    if len(raw) < KAIPAN_MIN_FIELDS:
        return None
    symbol = symbol_of(raw[0])
    if symbol is None:
        return None
    row = _base_row(symbol, trade_date, raw[1], level)
    for position, column in KAIPAN_POSITIONS.items():
        value = raw[position] if position < len(raw) else None
        kind = COLUMN_KIND[column]
        row[column] = _text(value) if kind == "str" else (_int(value) if kind == "int" else _finite(value))
    # design/03 section 5.2: lb_days is the record's own [15] (consecutive limit-up days), the ladder level only when
    # [15] is empty.  Real 2026-10-23: 2 of 82 records sit on level 5 but say [15]=6, WenCai and TD both say 6.
    own = row.get("kp_15")
    if own is not None and own >= 1 and own == int(own):
        row["lb_days"] = int(own)
    return row


def build_kaipan(trade_date: str, pages: Mapping[int, Mapping[str, Any]]) -> Partition:
    """``pages``: ladder level -> {"records": [positional lists], "date_echo": str|None} as fetched by the provider.

    Date evidence is the date string the vendor echoes inside ``info`` (``info[1]``); a missing or different echo
    rejects the whole response (TIER_DATE_MISMATCH).  Level 1 without any record means the day is not available yet.
    """
    if not pages or not pages.get(1) or not pages[1].get("records"):
        raise PoolSourceError("KAIPAN_NOT_READY", "level_1_empty", exit_code=30, retryable=True)
    echoes: dict[int, str | None] = {level: page.get("date_echo") for level, page in sorted(pages.items())}
    bad = {level: echo for level, echo in echoes.items() if echo != trade_date}
    if bad:
        raise PoolSourceError("TIER_DATE_MISMATCH", f"requested {trade_date}, vendor echoed {bad}", exit_code=40)
    rows: dict[str, dict[str, Any]] = {}
    seen = short = duplicate = level_mismatch = 0
    for level, page in sorted(pages.items()):
        for raw in page.get("records") or ():
            seen += 1
            row = kaipan_row(raw, level, trade_date)
            if row is None:
                short += 1
            elif row["symbol"] in rows:
                duplicate += 1
            else:
                rows[row["symbol"]] = row
                level_mismatch += row["lb_days"] != level
    if not rows:
        raise PoolSourceError("TIER_NO_DATA", "no_usable_record", exit_code=40)
    columns = [name for name, *_ in COMMON_COLUMNS + VALUE_COLUMNS + KAIPAN_COLUMNS]
    notes = {"levels": sorted(pages), "records_seen": seen, "short_records_dropped": short,
             "duplicate_symbols_dropped": duplicate, "lb_days_differs_from_ladder_level": level_mismatch,
             "lb_days_source": "record field [15], ladder level when empty",
             "record_width": KAIPAN_FULL_FIELDS, "st_filter": "VENDOR_EXCLUDES_ST"}
    evidence = {"kind": "kaipan_info_date_echo", "values": sorted({str(v) for v in echoes.values()}),
                "levels": len(echoes)}
    return _finish(list(rows.values()), KAIPAN, trade_date, expected_count=seen, source_columns=columns,
                   evidence=evidence, notes=notes,
                   extra_quality={"dropped_short_records": short, "dropped_duplicates": duplicate})


# ------------------------------------------------------------------------------------------------------------------
# Tier 2: WenCai "{date}涨停;连板天数;去除st"
# ------------------------------------------------------------------------------------------------------------------
_WC_MAP = (   # (header base, column, kind)
    ("连续涨停天数", "lb_days", "int"), ("涨停类型", "wc_limit_type", "str"), ("几天几板", "wc_streak_text", "str"),
    ("首次涨停时间", "wc_first_limit_time", "str"), ("最终涨停时间", "wc_last_limit_time", "str"),
    ("涨停开板次数", "wc_open_count", "int"), ("涨停封单量", "wc_seal_volume_shares", "float"),
    ("涨停封单额", "wc_seal_amount_yuan", "float"), ("涨停封单量占成交量比", "wc_seal_vol_ratio", "float"),
    ("涨停封单量占流通a股比", "wc_seal_float_ratio", "float"),
    ("a股市值(不含限售股)", "wc_float_mktcap_yuan", "float"), ("涨停原因类别", "wc_reason_category", "str"))
_SUFFIX = re.compile(r"^(?P<base>.*)\[(?P<day>\d{8})\]$")


def wencai_header_dates(records: Sequence[Mapping[str, Any]]) -> set[str]:
    days: set[str] = set()
    for record in records:
        for header in record:
            match = _SUFFIX.match(str(header))
            if match:
                days.add(f"{match['day'][:4]}-{match['day'][4:6]}-{match['day'][6:]}")
    return days


def build_wencai(trade_date: str, records: Sequence[Mapping[str, Any]], *, reported_total: int | None) -> Partition:
    """Dated WenCai records -> rows.  Date evidence = the ``[YYYYMMDD]`` header suffix, never the request text."""
    if not records:
        raise PoolSourceError("TIER_NO_DATA", "empty_response", exit_code=40)
    days = wencai_header_dates(records)
    if days != {trade_date}:
        raise PoolSourceError("TIER_DATE_MISMATCH", f"requested {trade_date}, column dates {sorted(days) or 'none'}",
                              exit_code=40)
    suffix = trade_date.replace("-", "")
    rows: dict[str, dict[str, Any]] = {}
    duplicate = unusable = 0
    for record in records:
        symbol = symbol_of(record.get("code")) or symbol_of(record.get("股票代码"))
        lb = _int(record.get(f"连续涨停天数[{suffix}]"))
        if symbol is None or lb is None or lb < 1 or str(record.get(f"涨停[{suffix}]") or "").strip() != "涨停":
            unusable += 1
            continue
        if symbol in rows:
            duplicate += 1
            continue
        row = _base_row(symbol, trade_date, record.get("股票简称"), lb)
        for base, column, kind in _WC_MAP:
            if column == "lb_days":
                continue
            value = record.get(f"{base}[{suffix}]")
            row[column] = _text(value) if kind == "str" else (_int(value) if kind == "int" else _finite(value))
        rows[symbol] = row
    if not rows:
        raise PoolSourceError("TIER_NO_DATA", "no_usable_record", exit_code=40)
    expected = int(reported_total) if reported_total is not None else len(records)
    columns = [name for name, *_ in COMMON_COLUMNS + WENCAI_COLUMNS]
    notes = {"records_seen": len(records), "unusable_records_dropped": unusable, "duplicate_symbols_dropped": duplicate,
             "query_suffix": "去除st", "st_filter": "VENDOR_EXCLUDES_ST"}
    evidence = {"kind": "wencai_column_suffix", "values": sorted(days)}
    return _finish(list(rows.values()), WENCAI, trade_date, expected_count=expected, source_columns=columns,
                   evidence=evidence, notes=notes,
                   extra_quality={"dropped_duplicates": duplicate, "dropped_unusable": unusable})


# ------------------------------------------------------------------------------------------------------------------
# Tier 3: derived from TD daily bars
# ------------------------------------------------------------------------------------------------------------------
Bar = tuple  # (date 'YYYY-MM-DD', close, high, pct_chg)


def infer_sessions(bars_by_symbol: Mapping[str, Sequence[Bar]], *, min_share: float = 0.5) -> list[str]:
    """Trading sessions = days on which at least ``min_share`` of the busiest day's symbols have a bar."""
    per_day: dict[str, int] = {}
    for bars in bars_by_symbol.values():
        for bar in bars:
            per_day[bar[0]] = per_day.get(bar[0], 0) + 1
    if not per_day:
        return []
    floor = min_share * max(per_day.values())
    return sorted(day for day, count in per_day.items() if count >= floor)


def bar_is_limit_up(symbol: str, bar: Bar | None) -> bool:
    """FACTS rule: pct_chg >= board limit - 0.6 and close == high (float32 values compare exactly)."""
    if bar is None:
        return False
    close, high, pct = _finite(bar[1]), _finite(bar[2]), _finite(bar[3])
    if close is None or high is None or pct is None or close <= 0:
        return False
    return pct >= limit_pct(symbol) - TD_RULE_MARGIN_PCT and close == high


def build_td_derived(trade_date: str, bars_by_symbol: Mapping[str, Sequence[Bar]], *,
                     names: Mapping[str, str] | None, sessions: Sequence[str] | None = None,
                     st_source: str | None = None) -> Partition:
    """Derive the pool for ``trade_date`` from daily bars.  ``names`` None => ST cannot be filtered (UNAVAILABLE)."""
    sessions = sorted(sessions) if sessions is not None else infer_sessions(bars_by_symbol)
    if trade_date not in sessions:
        raise PoolSourceError("TIER_NO_DATA", f"no_td_bars_for_{trade_date}", exit_code=40)
    earlier = [day for day in sessions if day < trade_date]
    by_day = {symbol: {bar[0]: bar for bar in bars} for symbol, bars in bars_by_symbol.items()}
    st_known = names is not None
    rows: list[dict[str, Any]] = []
    excluded_st = no_bar = 0
    for symbol, days in by_day.items():
        today = days.get(trade_date)
        if today is None:
            no_bar += 1
            continue
        if not bar_is_limit_up(symbol, today):
            continue
        name = (names or {}).get(symbol)
        if st_known and is_st_name(name):
            excluded_st += 1
            continue
        lb = 1
        for day in reversed(earlier):
            if not bar_is_limit_up(symbol, days.get(day)):
                break
            lb += 1
        row = _base_row(symbol, trade_date, name, lb)
        row.update(price=_finite(today[1]), pct_chg=_finite(today[3]), limit_pct=limit_pct(symbol))
        rows.append(row)
    if not rows:
        raise PoolSourceError("TIER_NO_DATA", "derivation_found_no_limit_up", exit_code=40)
    columns = [name for name, *_ in COMMON_COLUMNS + VALUE_COLUMNS + TD_COLUMNS]
    notes = {"derived": True, "rule": "pct_chg >= board_limit - 0.6 and close == high",
             "board_limits": "688/689/30*:20, 8*/4*/92*:30, other:10, no ST tier",
             "st_filter": "EXCLUDED_BY_NAME" if st_known else "UNAVAILABLE",
             "st_source": st_source if st_known else None, "st_excluded": excluded_st,
             "symbols_in_bars": len(by_day), "symbols_without_bar_on_date": no_bar,
             "sessions_used": len(sessions), "lb_basis": "consecutive earlier sessions with the same rule",
             "no_seal_evidence": True, "note": "inferred pool: close at the limit price, seal not verified"}
    evidence = {"kind": "td_bar_date", "values": [trade_date]}
    return _finish(rows, TD_DERIVED, trade_date, expected_count=len(rows), source_columns=columns,
                   evidence=evidence, notes=notes,
                   extra_quality={"st_filter": notes["st_filter"], "dropped_duplicates": 0})
