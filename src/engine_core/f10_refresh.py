"""Pure normalization and merge helpers for dated F10 reference data."""

from __future__ import annotations

import math
import re
from collections import Counter
from statistics import median
from typing import Any, Iterable, Mapping

IDENTITY_COLUMNS = {"股票代码", "股票简称"}
TEXT_COLUMNS = IDENTITY_COLUMNS | {
    "业绩变化原因", "主营产品名称", "城市", "所属同花顺行业", "新股上市日期",
}
NUMERIC_COLUMNS = {
    "a股市值(不含限售股)", "净资产收益率roe(加权,公布值)", "市净率(pb)", "市盈率(pe)",
    "归属于母公司所有者的净利润", "总市值", "总股本", "流通a股", "营业收入",
    "资产负债率", "销售毛利率",
}
OLD_ASOF = "2026-09-14"
_DATE_SUFFIX = re.compile(r"\[(\d{8})\]$")
_ALIASES = {
    "流通a股市值": "a股市值(不含限售股)",
    "a股市值": "a股市值(不含限售股)",
    "业绩变动原因": "业绩变化原因",
    "归属母公司股东的净利润": "归属于母公司所有者的净利润",
    "净资产收益率(加权,公布值)": "净资产收益率roe(加权,公布值)",
    "股票代码": "股票代码", "股票简称": "股票简称",
}


def _symbol(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    text = re.sub(r"\.(SZ|SH|BJ)$", "", text)
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "-", "N/A", "nan", "NaN"}:
        return None
    try:
        result = float(text)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _column(raw_name: Any) -> tuple[str, str | None]:
    name = str(raw_name).strip()
    match = _DATE_SUFFIX.search(name)
    asof = match.group(1) if match else None
    if match:
        name = name[:match.start()]
    return _ALIASES.get(name, name), asof


def normalize_wencai_rows(rows: Iterable[Mapping[str, Any]], group: str) -> dict[str, dict[str, Any]]:
    """Normalize rows from one mkt/ind/fin query group; latest dated cell wins."""
    del group  # group identity is carried by the caller; columns share one contract.
    out: dict[str, dict[str, Any]] = {}
    dates: dict[str, dict[str, str]] = {}
    for raw in rows:
        values: dict[str, Any] = {}
        row_dates: dict[str, str] = {}
        explicit_units: dict[str, Any] = {}
        for raw_key, value in raw.items():
            if raw_key == "_units" and isinstance(value, Mapping):
                for unit_key, unit in value.items():
                    normalized_key, _ = _column(unit_key)
                    explicit_units[normalized_key] = unit
                continue
            key, date = _column(raw_key)
            if date:
                row_dates[key] = max(date, row_dates.get(key, ""))
            values[key] = value
        symbol = _symbol(values.get("股票代码"))
        if symbol is None:
            continue
        target = out.setdefault(symbol, {"股票代码": symbol})
        target_dates = dates.setdefault(symbol, {})
        for key, value in values.items():
            if key == "股票代码":
                continue
            incoming_date = row_dates.get(key, "")
            previous_date = target_dates.get(key, "")
            if key not in target or incoming_date >= previous_date:
                target[key] = _number(value) if key in NUMERIC_COLUMNS else (str(value).strip() or None if value is not None else None)
                if incoming_date:
                    target_dates[key] = incoming_date
        target["_asof"] = max(row_dates.values(), default=target.get("_asof", "")) or None
        if explicit_units:
            target.setdefault("_units", {}).update(explicit_units)
        if row_dates:
            target.setdefault("_asof_by_column", {}).update(row_dates)
    return out


def _old_map(old_rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for raw in old_rows:
        row = dict(raw)
        symbol = _symbol(row.get("股票代码"))
        if symbol is None:
            continue
        row["股票代码"] = symbol
        for col in NUMERIC_COLUMNS:
            if col in row:
                row[col] = _number(row[col])
        result[symbol] = row
    return result


def _ratio_distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "median": None, "p10": None, "p90": None}
    ordered = sorted(values)
    def pct(p: float) -> float:
        pos = (len(ordered) - 1) * p
        lo = int(pos); hi = min(lo + 1, len(ordered) - 1)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)
    return {"count": len(ordered), "median": median(ordered), "p10": pct(.1), "p90": pct(.9)}


def merge_f10(old_rows: Iterable[Mapping[str, Any]], new_by_group: Mapping[str, Mapping[str, Mapping[str, Any]]],
              caps: Mapping[str, Mapping[str, Any]] | None = None, *, new_asof: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    old = _old_map(old_rows)
    new: dict[str, dict[str, Any]] = {}
    units: dict[tuple[str, str], Any] = {}
    for group_rows in new_by_group.values():
        for raw_symbol, raw in group_rows.items():
            symbol = _symbol(raw.get("股票代码", raw_symbol))
            if symbol is None:
                continue
            target = new.setdefault(symbol, {"股票代码": symbol})
            target_dates = target.setdefault("_asof_by_column", {})
            for key, value in raw.items():
                if key.startswith("_") or key == "股票代码":
                    continue
                date = raw.get("_asof_by_column", {}).get(key, raw.get("_asof") or new_asof.replace("-", ""))
                if key not in target or date >= target_dates.get(key, ""):
                    target[key] = value
                    target_dates[key] = date
                unit = raw.get("_units", {}).get(key)
                if unit is not None:
                    units[(symbol, key)] = unit

    all_symbols = sorted(set(old) | set(new))
    caps = caps or {}
    out = []
    conflicts = []
    source_totals: dict[str, Counter] = {}
    ratios = []
    updated = retained = added = 0
    only_identity = []
    missing_caps = []
    all_columns = set().union(*(r.keys() for r in old.values()), *(r.keys() for r in new.values())) - {
        "_asof", "_asof_by_column", "_units", "_source", "股票代码"
    }
    all_columns |= set(IDENTITY_COLUMNS)
    for symbol in all_symbols:
        o, n = old.get(symbol, {}), new.get(symbol, {})
        row: dict[str, Any] = {"股票代码": symbol}
        sources: dict[str, str] = {}
        asofs: dict[str, str | None] = {}
        if symbol not in old:
            added += 1
        elif symbol not in new:
            retained += 1
        else:
            updated += 1
        for col in sorted(all_columns):
            value = n.get(col)
            source = "WENCAI_NEW" if value is not None else None
            asof = (n.get("_asof_by_column", {}).get(col) or n.get("_asof") or new_asof) if source else None
            conflict = False
            old_value = o.get(col)
            new_unit = units.get((symbol, col))
            # F10 CSV and Wencai numeric fields use yuan/shares/percent as named;
            # only an explicit incoming unit can establish a conflict.
            expected_unit = "yuan" if col in {"总市值", "a股市值(不含限售股)", "归属于母公司所有者的净利润", "营业收入"} else None
            if value is not None and new_unit and expected_unit and new_unit != expected_unit:
                conflict = True
                conflicts.append({"symbol": symbol, "column": col, "new_unit": new_unit,
                                  "expected_unit": expected_unit, "selected": "WENCAI_NEW"})
            if value is None and old_value is not None:
                value = old_value
                source = "STALE_OLD" if symbol in new else "OLD_CSV"
                asof = OLD_ASOF
            if value is None and col in {"总市值", "a股市值(不含限售股)"}:
                cap_key = "total_cap" if col == "总市值" else "float_cap"
                cap_value = _number((caps.get(symbol) or {}).get(cap_key))
                if cap_value is not None:
                    value, source, asof = cap_value, "KAIPAN_CAPS", None
            if value is None:
                source, asof = "MISSING", None
            if conflict:
                source = "UNIT_CONFLICT"
            row[col] = value
            sources[col] = source
            asofs[col] = asof
            source_totals.setdefault(col, Counter())[source] += 1
        row["_source"] = sources
        row["_asof_by_column"] = asofs
        row["_row_status"] = "STALE_OLD" if symbol in old and symbol not in new else ("ADDED" if symbol not in old else "UPDATED")
        if all(row.get(c) is None for c in NUMERIC_COLUMNS if c in all_columns):
            only_identity.append({"股票代码": symbol, "股票简称": row.get("股票简称")})
        if row.get("a股市值(不含限售股)") is None:
            missing_caps.append({"股票代码": symbol, "股票简称": row.get("股票简称")})
        old_cap, new_cap = _number(o.get("a股市值(不含限售股)")), _number(n.get("a股市值(不含限售股)"))
        if old_cap and new_cap:
            ratios.append(new_cap / old_cap)
        out.append(row)
    columns = sorted(all_columns)
    missing = {c: sum(r.get(c) is None for r in out) for c in columns}
    report = {
        "row_count": len(out),
        "missing": {c: {"count": n, "ratio": n / len(out) if out else None} for c, n in missing.items()},
        "source_counts": {c: dict(counts) for c, counts in sorted(source_totals.items())},
        "added_symbols": added, "retained_symbols": retained, "updated_symbols": updated,
        "old_new_a_share_cap_ratio": _ratio_distribution(ratios),
        "identity_only_symbols": only_identity, "market_cap_missing_symbols": missing_caps,
        "unit_conflicts": conflicts,
    }
    return out, report


__all__ = ["merge_f10", "normalize_wencai_rows"]
