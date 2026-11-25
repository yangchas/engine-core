"""Pure builder for ``ladder_daily`` (C577, fw/design/24-DATASET-CONTRACT.md, docs/LADDER_DAILY.md).

``build_ladder_daily(request)`` derives the consecutive-limit-up ladder (streak) of one trade day from per-day
limit-up sets. It never reads a previous ``ladder_daily`` partition (no relay accumulation), the environment, the
disk, the network or the wall clock; everything it needs is inside ``request``.

Per trade day the limit-up set comes from a configured source ladder (default ``daily_kline`` > ``kaipan`` >
``wencai``), chosen **per day**: a day whose daily bars are missing or not continuous with the previous trading day
drops to the next rung; a day no rung can answer is UNKNOWN and truncates the streak chain (streaks become lower
bounds, never invented numbers).

Request (dict)::

    trade_date        "YYYY-MM-DD"
    as_of             tz-aware ISO instant (inputs with available_at later than this are invisible)
    fetched_at        tz-aware ISO instant recorded in the manifest (default as_of)
    calendar          ascending list of trading days up to and including trade_date (the trading calendar)
    window            {"max_days": 20}  cap of the look-back; the walk itself stops as soon as no streak survives
    source_ladder     ["daily_kline", "kaipan", "wencai"]
    continuity_min_ratio  0.95  share of symbols whose pre_close equals the previous trading day's close
    days              {date: {rung: input}}; an absent rung means "not loaded yet" (NeedInput is raised),
                      {"status": "unavailable", "reason": ...} means "asked, nothing"
        daily_kline   {"status":"ok","version","business_status","source","available_at","rows":[...]}
        kaipan/wencai {"status":"ok","version"?,"business_status"?,"source","available_at","rows":[{"symbol",..}],
                       "date_evidence": "YYYY-MM-DD"?}
    listings          {symbol: "YYYY-MM-DD"} listing dates (stock_static); optional
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .derived_daily import (STATUS_RANK, Partition, UpstreamNotReady, canonical, lowest_status, normalize_status)

ALGORITHM = "ladder_v1"
PRIMARY = "derived_daily_kline"
SCHEMA_VERSION = 1
DEFAULT_MAX_DAYS = 20
DEFAULT_LADDER = ("daily_kline", "kaipan", "wencai")
RUNG_SOURCE = {"daily_kline": PRIMARY, "kaipan": "kaipan", "wencai": "wencai"}
RUNG_DEFAULT_STATUS = {"kaipan": "PROVISIONAL", "wencai": "DEGRADED"}
# Limit width depends only on the board (user 2026-10-28: ST 5% no longer exists, no ST distinction).
BOARD_LIMIT_PCT = {"MAIN": 10, "GEM": 20, "STAR": 20, "BSE": 30}
NEW_LISTING_FREE_DAYS = {"MAIN": 5, "GEM": 5, "STAR": 5, "BSE": 1}
BSE_UNVERIFIED_DAYS = 10     # BSE listing days 2..10: exchange text not verified -> excluded and counted, not guessed
EXAMPLES = 5


class NeedInput(Exception):
    """The walk needs ``days[date][rung]`` which the request does not contain yet."""

    def __init__(self, date: str, rung: str):
        super().__init__(f"{date}:{rung}")
        self.date, self.rung = date, rung


def board_of(symbol: str) -> str:
    """Board by code prefix; anything not recognised is UNKNOWN (never defaulted to MAIN)."""
    s = str(symbol)
    if len(s) != 6 or not s.isdigit():
        return "UNKNOWN"
    if s[:3] in ("688", "689"):
        return "STAR"
    if s[:3] in ("300", "301", "302"):
        return "GEM"
    if s[:3] in ("600", "601", "603", "605", "000", "001", "002", "003"):
        return "MAIN"
    if s[:2] in ("43", "83", "87", "88", "92"):
        return "BSE"
    return "UNKNOWN"


def limit_price(pre_close: Any, board: str) -> Decimal | None:
    pct = BOARD_LIMIT_PCT.get(board)
    if pct is None:
        return None
    try:
        pre = Decimal(str(pre_close))
    except (InvalidOperation, ValueError):
        return None
    if pre <= 0:
        return None
    return (pre * (Decimal(100 + pct) / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _sym(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("code") or "")[-6:].zfill(6)


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _sha(symbols: Sequence[str]) -> str:
    return hashlib.sha256(canonical(sorted(symbols))).hexdigest()[:16]


@dataclass
class DaySet:
    date: str
    source: str                       # PRIMARY / kaipan / wencai / UNKNOWN
    rung: str | None
    symbols: frozenset | None         # None = UNKNOWN
    business_status: str
    version: str | None
    attempts: list
    stats: dict


class _Walk:
    def __init__(self, request: Mapping[str, Any]):
        self.req = request
        self.day = str(request.get("trade_date") or "")
        self.as_of = _instant(request.get("as_of"))
        if len(self.day) != 10 or self.as_of is None:
            raise ValueError("ladder_request_invalid:trade_date_and_tz_aware_as_of_required")
        self.fetched_at = _instant(request.get("fetched_at")) or self.as_of
        self.calendar = [str(d) for d in request.get("calendar") or []]
        if self.day not in self.calendar:
            raise UpstreamNotReady("TRADE_DAY_NOT_IN_CALENDAR", self.day)
        self.ladder = [r for r in (request.get("source_ladder") or DEFAULT_LADDER) if r in RUNG_SOURCE]
        self.max_days = int((request.get("window") or {}).get("max_days", DEFAULT_MAX_DAYS))
        if self.max_days < 2:
            raise ValueError("ladder_request_invalid:window_max_days_below_2")
        self.min_ratio = float(request.get("continuity_min_ratio", 0.95))
        self.days = request.get("days") or {}
        self.listings = request.get("listings") or {}
        self.memo: dict[str, DaySet] = {}
        self.cross: dict[str, dict] = {}

    def prev(self, d: str) -> str | None:
        i = self.calendar.index(d)
        return self.calendar[i - 1] if i > 0 else None

    def get(self, d: str, rung: str) -> Mapping[str, Any] | None:
        entry = (self.days.get(d) or {}).get(rung, "__absent__")
        if entry == "__absent__":
            raise NeedInput(d, rung)
        if not isinstance(entry, Mapping) or entry.get("status") != "ok":
            return None
        available = _instant(entry.get("available_at"))
        if available is not None and available > self.as_of:
            return None  # point in time: not visible yet at as_of
        return entry

    # ------------------------------------------------------------------ daily_kline rung
    def _continuous(self, d: str, part: Mapping[str, Any], before: Mapping[str, Any] | None):
        """Bars of ``d`` are usable only when the previous trading day's bars exist and join them: the share of
        symbols whose pre_close equals the previous close must reach ``continuity_min_ratio``."""
        if before is None or not before.get("rows"):
            return False, "previous_day_kline_missing", 0, 0
        closes = {}
        for row in before["rows"]:
            c = _num(row.get("close"))
            if c is not None and c > 0:
                closes[_sym(row)] = c
        both = agree = 0
        for row in part["rows"]:
            pre, s = _num(row.get("pre_close")), _sym(row)
            if pre is not None and pre > 0 and s in closes:
                both += 1
                agree += abs(pre - closes[s]) <= 0.011
        ratio = (agree / both) if both else 0.0
        if both == 0 or ratio < self.min_ratio:
            return False, f"not_continuous:pre_close_matches_previous_close={agree}/{both}", agree, both
        dated = {str(r.get("date"))[:10] for r in part["rows"] if r.get("date")}
        if dated and dated != {d}:
            return False, f"row_dates_mismatch:{sorted(dated)[:2]}", agree, both
        return True, "", agree, both

    def _kline_set(self, d: str):
        part = self.get(d, "daily_kline")
        if part is None or not part.get("rows"):
            return None, "daily_kline_missing"
        p = self.prev(d)
        if p is None:
            return None, "no_previous_trading_day_in_calendar"
        before = self.get(p, "daily_kline")
        ok, reason, agree, both = self._continuous(d, part, before)
        if not ok:
            return None, reason
        present_before = {_sym(r) for r in before["rows"]}
        stats = {"continuity": f"{agree}/{both}", "board_unknown": 0, "exempt_new_listing": 0,
                 "bse_rule_unverified": 0, "above_limit_not_limit_up": 0, "no_trade": 0, "unusable_rows": 0}
        hits, seen = set(), set()
        for row in part["rows"]:
            s = _sym(row)
            if s in seen:
                continue
            seen.add(s)
            board = board_of(s)
            if board == "UNKNOWN":
                stats["board_unknown"] += 1
                continue
            close, pre = _num(row.get("close")), _num(row.get("pre_close"))
            amount = _num(row.get("amount_yuan", row.get("amount")))
            if close is None or pre is None or close <= 0 or pre <= 0:
                stats["unusable_rows"] += 1
                continue
            if row.get("suspended") is True or amount == 0:
                stats["no_trade"] += 1
                continue
            limit = limit_price(pre, board)
            if limit is None:
                stats["unusable_rows"] += 1
                continue
            got = Decimal(str(close)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if got < limit:
                continue
            index = self._listing_index(s, d, present_before)
            if index is not None:
                if index <= NEW_LISTING_FREE_DAYS[board]:
                    stats["exempt_new_listing"] += 1
                    continue
                if board == "BSE" and index <= BSE_UNVERIFIED_DAYS:
                    stats["bse_rule_unverified"] += 1
                    continue
            if got == limit:
                hits.add(s)
            else:
                stats["above_limit_not_limit_up"] += 1
        stats["limit_up_count"] = len(hits)
        return DaySet(d, PRIMARY, "daily_kline", frozenset(hits),
                      normalize_status(part.get("business_status")), part.get("version"), [], stats), None

    def _symbols_on(self, d: str) -> set | None:
        cache = self.__dict__.setdefault("_symcache", {})
        if d not in cache:
            part = self.get(d, "daily_kline")
            cache[d] = {_sym(r) for r in part["rows"]} if part is not None and part.get("rows") else None
        return cache[d]

    def _listing_index(self, symbol: str, d: str, present_before: set) -> int | None:
        """1 = listing day. From the stock_static listing date, else inferred from the first bar in the daily bars
        (symbol absent on the previous trading days that are available); None = cannot tell (treated as ordinary)."""
        listed = self.listings.get(symbol)
        if listed:
            if listed > d or listed not in self.calendar:
                return None
            return len([x for x in self.calendar if listed <= x <= d])
        cur, index = self.prev(d), 1
        while cur is not None and index <= BSE_UNVERIFIED_DAYS:
            symbols = present_before if index == 1 else self._symbols_on(cur)
            if symbols is None:
                return None
            if symbol not in symbols:
                # absent on `cur`: for a first-day inference require absence on up to 2 earlier days as well
                if index == 1:
                    for back in (self.prev(cur), self.prev(self.prev(cur)) if self.prev(cur) else None):
                        if back is None:
                            break
                        earlier = self._symbols_on(back)
                        if earlier is not None and symbol in earlier:
                            return None   # a one-day hole in the bars, not a new listing
                return index
            index += 1
            cur = self.prev(cur)
        return None

    # ------------------------------------------------------------------ pool rungs
    def _pool_set(self, d: str, rung: str):
        entry = self.get(d, rung)
        if entry is None:
            return None, f"{rung}_unavailable"
        rows = entry.get("rows") or []
        evidence = entry.get("date_evidence")
        if rung == "wencai" and evidence != d:
            return None, f"{rung}_date_evidence_mismatch:{evidence}"
        bad = {str(r.get("trade_date"))[:10] for r in rows if r.get("trade_date") and str(r.get("trade_date"))[:10] != d}
        if bad:
            return None, f"{rung}_row_dates_mismatch:{sorted(bad)[:2]}"
        if not rows:
            return None, f"{rung}_empty"
        symbols = frozenset(_sym(r) for r in rows if r.get("symbol") or r.get("code"))
        status = normalize_status(entry.get("business_status") or RUNG_DEFAULT_STATUS[rung])
        return DaySet(d, RUNG_SOURCE[rung], rung, symbols, status, entry.get("version"), [],
                      {"limit_up_count": len(symbols)}), None

    def resolve(self, d: str) -> DaySet:
        if d in self.memo:
            return self.memo[d]
        attempts, chosen = [], None
        for rung in self.ladder:
            ds, reason = self._kline_set(d) if rung == "daily_kline" else self._pool_set(d, rung)
            if ds is None:
                attempts.append({"provider": RUNG_SOURCE[rung], "result": "unusable", "elapsed_ms": 0,
                                 "reason": f"{d}:{reason}", "row_count": 0})
                continue
            attempts.append({"provider": RUNG_SOURCE[rung], "result": "ok", "elapsed_ms": 0, "reason": d,
                             "row_count": len(ds.symbols)})
            chosen = ds
            break
        if chosen is None:
            chosen = DaySet(d, "UNKNOWN", None, None, "DEGRADED", None, attempts, {})
        chosen.attempts = attempts
        # built-in cross validation: daily bars vs Kaipan pool when both exist for the day
        if chosen.rung == "daily_kline" and "kaipan" in self.ladder:
            other = (self.days.get(d) or {}).get("kaipan")
            if isinstance(other, Mapping) and other.get("status") == "ok" and other.get("rows"):
                pool = frozenset(_sym(r) for r in other["rows"])
                self.cross[d] = {"kline_count": len(chosen.symbols), "kaipan_count": len(pool),
                                 "both": len(chosen.symbols & pool),
                                 "kline_only": len(chosen.symbols - pool), "kaipan_only": len(pool - chosen.symbols),
                                 "kline_only_examples": sorted(chosen.symbols - pool)[:EXAMPLES],
                                 "kaipan_only_examples": sorted(pool - chosen.symbols)[:EXAMPLES]}
        self.memo[d] = chosen
        return chosen


def build_ladder_daily(request: Mapping[str, Any]) -> Partition:
    w = _Walk(request)
    top = w.resolve(w.day)
    if top.symbols is None:
        raise UpstreamNotReady("LIMIT_UP_SET_UNKNOWN", w.day + ":" + ";".join(a["reason"] for a in top.attempts))
    streak = {s: 1 for s in top.symbols}
    start = {s: w.day for s in top.symbols}
    chain = {s: [RUNG_SOURCE.get(top.rung or "", top.source)] for s in top.symbols}
    truncated: dict[str, str] = {}
    alive = set(top.symbols)
    visited = [top]
    cursor, depth, capped = w.prev(w.day), 1, False
    while alive:
        if depth >= w.max_days:
            capped = True
            for s in alive:
                truncated[s] = "window_cap"
            break
        if cursor is None:
            for s in alive:
                truncated[s] = "calendar_start"
            break
        ds = w.resolve(cursor)
        visited.append(ds)
        if ds.symbols is None:
            for s in alive:
                truncated[s] = f"unknown_day:{cursor}"
            break
        alive = alive & ds.symbols
        for s in alive:
            streak[s] += 1
            start[s] = cursor
            chain[s].append(ds.source)
        cursor, depth = w.prev(cursor), depth + 1

    rows = [{"symbol": s, "date": w.day, "trade_date": w.day, "limit_up": True, "streak_n": streak[s],
             "streak_start_date": start[s], "day_source_chain": ">".join(chain[s]),
             "quality": "LOWER_BOUND:" + truncated[s] if s in truncated else "OK"}
            for s in sorted(top.symbols)]

    used = [ds for ds in visited if ds.symbols is not None]
    status = lowest_status([ds.business_status for ds in used])
    lower_bound = sorted(truncated)
    unknown_days = [ds.date for ds in visited if ds.symbols is None]
    if any(r.startswith("unknown_day") for r in truncated.values()):
        status = "DEGRADED"
    chain_sources: list[str] = []
    for ds in [top] + [x for x in visited[1:] if x.symbols is not None]:
        if ds.source not in chain_sources:
            chain_sources.append(ds.source)
    provider = top.source
    attempts = [dict(a) for ds in visited for a in ds.attempts]
    refs, day_inputs = [], {}
    for ds in visited:
        d = ds.date
        kl = (w.days.get(d) or {}).get("daily_kline")
        kl_ok = isinstance(kl, Mapping) and kl.get("status") == "ok"
        refs.append({"dataset": "daily_kline", "trade_date": d, "version": kl.get("version") if kl_ok else None,
                     "business_status": normalize_status(kl.get("business_status")) if kl_ok else None})
        kp = (w.days.get(d) or {}).get("kaipan")
        if isinstance(kp, Mapping) and kp.get("status") == "ok" and kp.get("version"):
            refs.append({"dataset": "limit_up_pool", "trade_date": d, "version": kp["version"],
                         "business_status": normalize_status(kp.get("business_status") or "PROVISIONAL")})
        elif ds.rung in ("kaipan", "wencai") or ds.symbols is None:
            refs.append({"dataset": "limit_up_pool", "trade_date": d, "version": None, "business_status": None})
        day_inputs[d] = {"source": ds.source, "version": ds.version, "business_status": ds.business_status,
                         "limit_up_count": None if ds.symbols is None else len(ds.symbols),
                         "set_sha": None if ds.symbols is None else _sha(ds.symbols)}
    first = visited[-1].date
    pre = w.prev(first)       # continuity of the oldest kline day reads its predecessor
    if pre is not None and visited[-1].rung == "daily_kline":
        part = (w.days.get(pre) or {}).get("daily_kline")
        ok = isinstance(part, Mapping) and part.get("status") == "ok"
        refs.append({"dataset": "daily_kline", "trade_date": pre, "version": part.get("version") if ok else None,
                     "business_status": normalize_status(part.get("business_status")) if ok else None})
    seen_ref, uniq = set(), []
    for r in refs:
        key = (r["dataset"], r["trade_date"])
        if key not in seen_ref:
            seen_ref.add(key)
            uniq.append(r)
    refs = uniq
    quality = {"coverage": None, "expected_basis": "none", "expected_count": len(rows), "missing": 0,
               "field_completeness": {c: "COMPLETE" for c in
                                      ("symbol", "date", "trade_date", "limit_up", "streak_n", "streak_start_date",
                                       "day_source_chain", "quality")},
               "partial": bool(lower_bound), "streak_lower_bound_count": len(lower_bound),
               "streak_lower_bound_examples": lower_bound[:EXAMPLES],
               "chain_truncated_days": unknown_days, "window_capped": capped,
               "history_non_formal_days": [ds.date for ds in used if ds.date != w.day and ds.business_status != "FORMAL"],
               "days_visited": len(visited),
               "day_stats": {ds.date: ds.stats for ds in visited if ds.stats},
               "cross_validation": w.cross,
               "max_streak": max(streak.values()) if streak else 0}
    inputs = {"days": day_inputs, "source_ladder": list(w.ladder), "window_max_days": w.max_days}
    manifest = {"schema_version": SCHEMA_VERSION, "dataset": "ladder_daily", "trade_date": w.day,
                "source": provider, "provider": provider, "provider_chain": chain_sources,
                "provider_attempts": attempts, "available_at": w.fetched_at.isoformat(),
                "fetched_at": w.fetched_at.isoformat(), "as_of": w.as_of.isoformat(),
                "business_status": status, "degraded": provider != PRIMARY or status == "DEGRADED",
                "row_count": len(rows), "quality": quality, "field_units": {"streak_n": "trading_days"},
                "algorithm_version": ALGORITHM,
                "dependencies": sorted({r["dataset"] for r in refs}),
                "window": {"max_days": w.max_days, "fixed_days": [ds.date for ds in visited]},
                "inputs": inputs, "input_refs": refs}
    return Partition(rows, manifest)
