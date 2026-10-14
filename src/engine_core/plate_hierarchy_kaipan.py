"""Parsers for sealed Kaipan pool and reason hierarchy responses."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any, Mapping, MutableMapping, Sequence

from .plate_membership import PlateHierarchy
from .plate_rules import normalize_plate_name


@dataclass(frozen=True)
class PoolRow:
    plate: str
    lb_days: int | None


@dataclass(frozen=True)
class KaipanReasonItem:
    symbol: str
    date: str | None
    type: str
    head_main: str
    head_detail: str
    zs_codes: tuple[str, ...]
    pzs_code: str


_REASON_HEAD = re.compile(r"([^()（）;；]+)[(（]([^()（）]+)[)）]")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@lru_cache(maxsize=32768)
def _parse_reason_head(reason: str) -> tuple[str, str]:
    match = _REASON_HEAD.search(reason)
    if not match:
        return "", ""
    return normalize_plate_name(match.group(1)), normalize_plate_name(match.group(2))


def _response_rows(response: Any) -> list[Any]:
    if isinstance(response, Mapping):
        info = response.get("info")
        if not isinstance(info, Sequence) or isinstance(info, (str, bytes)):
            return []
        # Kaipan's first info page contains rows; subsequent pages are metadata.
        pages = info if info and isinstance(info[0], Sequence) and not isinstance(info[0], (str, bytes)) else [info]
        output: list[Any] = []
        for page in pages:
            if isinstance(page, Sequence) and not isinstance(page, (str, bytes)):
                output.extend(row for row in page if isinstance(row, (Mapping, list, tuple)))
        return output
    if isinstance(response, Sequence) and not isinstance(response, (str, bytes)):
        output = []
        for item in response:
            output.extend(_response_rows(item))
        return output
    return []


def parse_kaipan_pool(responses: Any) -> dict[str, PoolRow]:
    """Parse raw getHisBans responses, retaining pool members with empty plates."""
    output: dict[str, PoolRow] = {}
    for row in _response_rows(responses):
        if isinstance(row, Mapping):
            symbol, plate, lb = row.get("symbol", row.get("code")), row.get("plate"), row.get("lb_days")
        elif len(row) > 15:
            symbol, plate, lb = row[0], row[12], row[15]
        else:
            continue
        symbol = re.sub(r"\D", "", str(symbol or ""))[-6:]
        if len(symbol) != 6:
            continue
        normalized_plate = normalize_plate_name(plate)
        if normalized_plate == "其他":
            normalized_plate = ""
        try:
            lb_days = int(lb) if lb is not None and not isinstance(lb, bool) else None
        except (TypeError, ValueError, OverflowError):
            lb_days = None
        existing = output.get(symbol)
        if existing is None or (lb_days is not None and (existing.lb_days is None or lb_days > existing.lb_days)):
            output[symbol] = PoolRow(normalized_plate, lb_days)
    return output


def _reason_records(response: Any) -> list[tuple[str, Mapping[str, Any]]]:
    if isinstance(response, Sequence) and not isinstance(response, (str, bytes, Mapping)):
        return [record for item in response for record in _reason_records(item)]
    if not isinstance(response, Mapping):
        return []
    symbol = str(response.get("StockID") or response.get("symbol") or "")
    rows = response.get("List", response.get("list", []))
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [(symbol, row) for row in rows if isinstance(row, Mapping)]


def parse_kaipan_reason_hierarchy(
    reason_responses: Any, *, as_of_date: str | None = None,
    counts: MutableMapping[str, int] | None = None, inclusive: bool = False,
) -> PlateHierarchy:
    """Extract parent edges from ``主(细)`` reasons with source code evidence."""
    items = parse_kaipan_reason_items(reason_responses)
    return build_hierarchy_as_of(items, as_of_date, inclusive=inclusive, counts=counts)


def parse_kaipan_reason_items(reason_responses: Any) -> tuple[KaipanReasonItem, ...]:
    """Parse immutable reason facts once so callers can reuse them across dates."""
    output: list[KaipanReasonItem] = []
    for symbol, row in _reason_records(reason_responses):
        reason = str(row.get("Reason") or row.get("reason") or "")
        parent, child = _parse_reason_head(reason)
        codes = row.get("ZSCode", row.get("zs_code", []))
        if isinstance(codes, str):
            codes = [codes]
        zs_codes = tuple(str(code) for code in codes
                         if code not in (None, "")) if isinstance(codes, Sequence) else ()
        raw_date = row.get("Date", row.get("date"))
        output.append(KaipanReasonItem(
            symbol=symbol, date=raw_date if isinstance(raw_date, str) else None,
            type=str(row.get("Type", row.get("type", "")) or ""),
            head_main=parent, head_detail=child, zs_codes=zs_codes,
            pzs_code=str(row.get("PZSCode") or row.get("pzs_code") or "").strip(),
        ))
    return tuple(output)


def build_hierarchy_as_of(
    items: Sequence[KaipanReasonItem], as_of_date: str | None,
    *, inclusive: bool = False, counts: MutableMapping[str, int] | None = None,
) -> PlateHierarchy:
    """Filter pre-parsed reason rows by date and deterministically choose parents."""
    candidates: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    cutoff = date.fromisoformat(as_of_date) if as_of_date is not None else None
    used = excluded_future = excluded_no_date = 0
    for item in items:
        if cutoff is not None:
            try:
                if item.date is None or not _ISO_DATE.fullmatch(item.date):
                    raise ValueError("Date must use YYYY-MM-DD")
                row_date = date.fromisoformat(item.date)
            except ValueError:
                excluded_no_date += 1
                continue
            if row_date > cutoff or (row_date == cutoff and not inclusive):
                excluded_future += 1
                continue
            used += 1
        parent, child = item.head_main, item.head_detail
        pzs_code = item.pzs_code
        if not pzs_code or pzs_code == "0" or not parent or not child or parent == child:
            continue
        bucket = candidates[child].setdefault(
            parent, {"symbols": set(), "occurrences": 0, "zs_codes": set(), "pzs_codes": set()},
        )
        bucket["occurrences"] += 1
        if item.symbol:
            bucket["symbols"].add(item.symbol)
        bucket["zs_codes"].update(item.zs_codes)
        bucket["pzs_codes"].add(pzs_code)

    parents: dict[str, str] = {}
    sources: dict[str, str] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for child, by_parent in sorted(candidates.items()):
        counts = Counter({parent: data["occurrences"] for parent, data in by_parent.items()})
        parent = sorted(by_parent, key=lambda name: (-counts[name], name))[0]
        chosen = by_parent[parent]
        parents[child] = parent
        sources[child] = "kaipan_pzs"
        pzs_codes = sorted(chosen["pzs_codes"])
        row_evidence: dict[str, Any] = {
            "zs_codes": sorted(chosen["zs_codes"]),
            "pzs_code": pzs_codes[0] if len(pzs_codes) == 1 else pzs_codes,
            "symbols_count": len(chosen["symbols"]),
        }
        if len(by_parent) > 1:
            row_evidence["conflict"] = True
        evidence[child] = row_evidence
    if counts is not None:
        counts.update(used=used, excluded_future=excluded_future, excluded_no_date=excluded_no_date)
    return PlateHierarchy(parents, sources, evidence)
