"""Pure builders for the derived daily datasets (C571, fw/design/24-DATASET-CONTRACT.md).

``build_daily_indicators(request)`` and ``build_daily_report_features(request)`` take already-loaded upstream
partitions inside ``request`` and return a :class:`Partition` (rows + manifest). They never read the environment,
never touch disk or Redis, never import ``engine_core.guardian`` and never look at the wall clock: every instant
that matters (``as_of``, ``fetched_at``) is part of the request. The thin adapters in ``daily_history.py`` /
``examples/build_daily_derived.py`` load the partitions from the catalog and commit the result.

Request (dict)::

    trade_date            "YYYY-MM-DD" partition being derived
    as_of                 tz-aware ISO instant; upstream partitions with available_at > as_of are invisible (PIT)
    fetched_at            tz-aware ISO instant recorded in the manifest (default: as_of)
    window                {"max_days": 120}  number of daily_kline partitions that feed the rolling windows
    min_upstream_status   "PROVISIONAL" (default) | "FORMAL"; trade_date's own daily_kline must reach it
    daily_kline           [{"trade_date","version","business_status","source","available_at","rows"}, ...]
    symbol_universe       {"trade_date","version","rows":[{"symbol","trade_status"}, ...], "basis"?}
                          basis defaults to "universe_active"; the historical-import adapter may pass
                          "daily_kline_symbols" for days older than the first universe partition
    daily_indicators      (report features only) {"trade_date","version","business_status","source"}

Quality inheritance: the partition's ``business_status`` is the lowest status among the *trade_date* inputs
(its own daily_kline, and for report features also its daily_indicators). Older history days are recorded in
``inputs`` / ``input_refs`` (so a changed old day produces a new version and is detected as stale) and summarised
in ``quality.history_non_formal_days``, but do not cap the status: historic gaps that no event can fix would
otherwise pin every later partition at PROVISIONAL forever.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

ALGORITHM = "derived_v1"
SCHEMA_VERSION = 1
DEFAULT_MAX_DAYS = 120
STATUS_RANK = {"DEGRADED": 1, "PROVISIONAL": 2, "FORMAL": 3}
SENTINEL_SYMBOL = "__MARKET__"

INDICATOR_COLUMNS = ("ma5", "ma10", "ma20", "bias_20", "rsi_6", "change_pct_5d",
                     "avg_turnover_5d", "limit_up_days_5", "vol_ratio")
# Units are verified against the kline column semantics (docs/DAILY_INDICATORS.md): close in yuan, pct_chg and
# turnover_pct in percentage points (Baostock ``turn`` / ``pctChg`` are percent), so MA are yuan, bias/change percent.
INDICATOR_UNITS = {"ma5": "yuan", "ma10": "yuan", "ma20": "yuan", "bias_20": "percent",
                   "change_pct_5d": "percent", "avg_turnover_5d": "percent"}
FEATURE_UNITS = {"prev_day_amount_yuan": "yuan", "range20_high": "yuan", "range20_low": "yuan",
                 "range20_last_close": "yuan", "amount_history_64": "yuan"}
FEATURE_COLUMNS = ("last_history_date", "streak", "position_20d", "position_close_days", "range20_low",
                   "range20_high", "range20_last_close", "prev_day_amount_yuan", "amount_history_64")


class UpstreamNotReady(RuntimeError):
    """Raised when an upstream partition is missing / below the configured minimum quality."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code + (":" + detail if detail else ""))
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Partition:
    rows: list[dict[str, Any]]
    manifest: dict[str, Any]


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def normalize_status(value: Any) -> str:
    """Unlabelled (pre-C568) or unknown upstream status is treated as PROVISIONAL, never promoted to FORMAL."""
    text = str(value or "").upper()
    return text if text in STATUS_RANK else "PROVISIONAL"


def lowest_status(statuses: Sequence[str]) -> str:
    return min((normalize_status(s) for s in statuses), key=lambda s: STATUS_RANK[s])


LEGACY_LADDER_SOURCE = "core_price_close_proxy"   # pre-C577 ladder_daily (relay accumulation): never wins


def version_rank_key(status: Any, source: Any = None) -> int:
    return 0 if source == LEGACY_LADDER_SOURCE else STATUS_RANK[normalize_status(status)]


def select_best(entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Highest-quality catalog entry of one partition; ties go to the most recently appended."""
    best = None
    for entry in entries:
        if best is None or version_rank_key(entry.get("business_status"), entry.get("source")) >= version_rank_key(
                best.get("business_status"), best.get("source")):
            best = entry
    return best


def _parse_instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _no_trade_zero(row: Mapping[str, Any]) -> bool:
    amount = row.get("amount_yuan", row.get("amount"))
    volume = row.get("volume")
    return (isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount == 0
            and (volume is None or (isinstance(volume, (int, float)) and volume == 0)))


def _symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("code") or "").zfill(6)


def _prepare(request: Mapping[str, Any]) -> dict[str, Any]:
    day = str(request.get("trade_date") or "")
    as_of = _parse_instant(request.get("as_of"))
    if len(day) != 10 or as_of is None:
        raise ValueError("derived_request_invalid:trade_date_and_tz_aware_as_of_required")
    fetched_at = _parse_instant(request.get("fetched_at")) or as_of
    max_days = int((request.get("window") or {}).get("max_days", DEFAULT_MAX_DAYS))
    if max_days < 20:
        raise ValueError("derived_request_invalid:window_max_days_below_20")
    minimum = normalize_status(request.get("min_upstream_status") or "PROVISIONAL")
    visible: list[Mapping[str, Any]] = []
    for part in request.get("daily_kline") or []:
        available = _parse_instant(part.get("available_at"))
        if str(part.get("trade_date", "")) <= day and available is not None and available <= as_of:
            visible.append(part)
    visible.sort(key=lambda part: str(part["trade_date"]))
    visible = visible[-max_days:]
    own = next((part for part in visible if part["trade_date"] == day), None)
    if own is None:
        raise UpstreamNotReady("DAILY_KLINE_MISSING", day)
    own_status = normalize_status(own.get("business_status"))
    if STATUS_RANK[own_status] < STATUS_RANK[minimum]:
        raise UpstreamNotReady("UPSTREAM_BELOW_MIN_QUALITY", f"daily_kline {day} is {own_status}, need {minimum}")
    universe = request.get("symbol_universe") or {}
    urows = universe.get("rows") or []
    if universe.get("trade_date") != day or not urows:
        raise UpstreamNotReady("SYMBOL_UNIVERSE_MISSING", day)
    by_symbol = {str(r.get("symbol", "")).strip(): r for r in urows}
    by_symbol.pop("", None)
    suspended = {s for s, r in by_symbol.items() if str(r.get("trade_status", "")).strip() == "0"}
    # one row per (symbol, date): the last one wins, duplicates are counted
    history: list[dict[str, Any]] = []
    duplicates = 0
    zero_bars = 0
    for part in visible:
        seen: dict[str, dict[str, Any]] = {}
        for raw in part.get("rows") or []:
            row = dict(raw)
            symbol = _symbol(row)
            event_date = str(row.get("date") or row.get("time") or part["trade_date"])[:10]
            if not symbol.isdigit() or len(symbol) != 6 or event_date > day:
                continue
            row["symbol"], row["date"] = symbol, event_date
            if _no_trade_zero(row):
                # contract §3: null must not be impersonated by 0. A bar with amount 0 and volume 0 is a
                # suspended / no-trade bar (legacy_q2_final writes 0.0 for them), not a day with zero turnover.
                row["amount_yuan"] = None
                row.pop("amount", None)
                zero_bars += 1
            if symbol in seen:
                duplicates += 1
            seen[symbol] = row
        history.extend(seen.values())
    history.sort(key=lambda row: (row["symbol"], row["date"]))
    return {"day": day, "as_of": as_of, "fetched_at": fetched_at, "max_days": max_days, "minimum": minimum,
            "visible": visible, "own": own, "own_status": own_status, "universe": universe,
            "universe_symbols": set(by_symbol), "active": set(by_symbol) - suspended,
            "suspended": suspended, "history": history, "duplicates": duplicates,
            "zero_bars": zero_bars}


def _history_digest(visible: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical([[p["trade_date"], p.get("version")] for p in visible])).hexdigest()[:16]


def _refs(ctx: Mapping[str, Any], extra: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    refs = [{"dataset": "daily_kline", "trade_date": p["trade_date"], "version": p.get("version"),
             "business_status": normalize_status(p.get("business_status"))} for p in ctx["visible"]]
    uni = ctx["universe"]
    if uni.get("version"):
        refs.append({"dataset": "symbol_universe", "trade_date": uni["trade_date"], "version": uni.get("version"),
                     "business_status": normalize_status(uni.get("business_status") or "FORMAL")})
    refs.extend(dict(item) for item in extra)
    return refs


def _completeness(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> dict[str, str]:
    out = {}
    for column in columns:
        nulls = sum(1 for row in rows if row.get(column) is None)
        out[column] = "COMPLETE" if nulls == 0 else "MISSING" if nulls == len(rows) else "PARTIAL"
    return out


def _manifest(dataset: str, ctx: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *, status: str,
              inputs: dict[str, Any], refs: list[dict[str, Any]], units: dict[str, str],
              completeness_columns: Sequence[str], dropped_outside: int) -> dict[str, Any]:
    data_rows = [r for r in rows if r.get("symbol") != SENTINEL_SYMBOL]
    present = {r["symbol"] for r in data_rows}
    active = ctx["active"]
    inside = len(active & present)
    history_days = [p["trade_date"] for p in ctx["visible"]]
    non_formal = [p["trade_date"] for p in ctx["visible"]
                  if normalize_status(p.get("business_status")) != "FORMAL" and p["trade_date"] != ctx["day"]]
    quality = {"coverage": (inside / len(active)) if active else None,
               "expected_basis": ctx["universe"].get("basis", "universe_active"), "expected_count": len(active),
               "missing": len(active - present), "suspended_in_universe": len(ctx["suspended"]),
               "field_completeness": _completeness(data_rows, completeness_columns),
               "partial": bool(active - present), "dropped_outside_universe": dropped_outside,
               "dropped_duplicates": ctx["duplicates"], "no_trade_amounts_nullified": ctx["zero_bars"],
               "history_trade_dates": len(history_days),
               "history_non_formal_days": non_formal}
    return {"schema_version": SCHEMA_VERSION, "dataset": dataset, "trade_date": ctx["day"],
            "source": ALGORITHM, "provider": ALGORITHM, "provider_chain": [],
            "provider_attempts": [{"provider": ALGORITHM, "result": "ok", "elapsed_ms": 0, "reason": "",
                                   "row_count": len(rows)}],
            "available_at": ctx["fetched_at"].isoformat(), "fetched_at": ctx["fetched_at"].isoformat(),
            "as_of": ctx["as_of"].isoformat(), "business_status": status, "degraded": status == "DEGRADED",
            "row_count": len(rows), "quality": quality, "field_units": units, "algorithm_version": ALGORITHM,
            "dependencies": sorted({ref["dataset"] for ref in refs}),
            "window": {"max_days": ctx["max_days"], "min_upstream_status": ctx["minimum"]},
            "inputs": inputs, "input_refs": refs}


def _kline_input(ctx: Mapping[str, Any]) -> dict[str, Any]:
    own = ctx["own"]
    return {"trade_date": ctx["day"], "version": own.get("version"), "business_status": ctx["own_status"],
            "source": own.get("source"), "history_days": len(ctx["visible"]),
            "history_digest": _history_digest(ctx["visible"])}


def build_daily_indicators(request: Mapping[str, Any]) -> Partition:
    from ..daily_history import build_indicator_rows
    ctx = _prepare(request)
    raw = build_indicator_rows(ctx["history"], trade_date=ctx["day"])
    rows, dropped = [], 0
    for row in raw:
        if row["symbol"] not in ctx["universe_symbols"]:
            dropped += 1
            continue
        # per-row ``source`` / ``field_units`` are replaced by manifest.provider / manifest.field_units
        rows.append({k: v for k, v in row.items() if k not in ("source", "field_units")})
    rows.sort(key=lambda r: r["symbol"])
    inputs = {"daily_kline": _kline_input(ctx),
              "symbol_universe": {"trade_date": ctx["day"], "version": ctx["universe"].get("version")}}
    manifest = _manifest("daily_indicators", ctx, rows, status=ctx["own_status"], inputs=inputs,
                         refs=_refs(ctx), units=dict(INDICATOR_UNITS), completeness_columns=INDICATOR_COLUMNS,
                         dropped_outside=dropped)
    return Partition(rows, manifest)


def build_daily_report_features(request: Mapping[str, Any]) -> Partition:
    from ..daily_history import build_daily_report_features as feature_rows
    ctx = _prepare(request)
    indicators = request.get("daily_indicators") or {}
    if indicators.get("trade_date") != ctx["day"] or not indicators.get("version"):
        raise UpstreamNotReady("DAILY_INDICATORS_MISSING", ctx["day"])
    ind_status = normalize_status(indicators.get("business_status"))
    status = lowest_status([ctx["own_status"], ind_status])
    raw = feature_rows(ctx["history"], trade_date=ctx["day"])
    rows, dropped = [], 0
    for row in raw:
        if row["symbol"] != SENTINEL_SYMBOL and row["symbol"] not in ctx["universe_symbols"]:
            dropped += 1
            continue
        rows.append(row)
    rows.sort(key=lambda r: r["symbol"])
    inputs = {"daily_kline": _kline_input(ctx),
              "daily_indicators": {"trade_date": ctx["day"], "version": indicators.get("version"),
                                   "business_status": ind_status, "source": indicators.get("source")},
              "symbol_universe": {"trade_date": ctx["day"], "version": ctx["universe"].get("version")}}
    extra = [{"dataset": "daily_indicators", "trade_date": ctx["day"], "version": indicators.get("version"),
              "business_status": ind_status}]
    manifest = _manifest("daily_report_features", ctx, rows, status=status, inputs=inputs,
                         refs=_refs(ctx, extra), units=dict(FEATURE_UNITS), completeness_columns=FEATURE_COLUMNS,
                         dropped_outside=dropped)
    return Partition(rows, manifest)


def inputs_stale(catalog: Mapping[str, Any], dataset: str, day: str, manifest: Mapping[str, Any]) -> bool:
    """True when a recorded input is no longer the best catalogued version (read-only, catalog dict only).

    Pre-C571 manifests with only ``inputs.daily_kline.version`` are compared on that single input.
    """
    datasets = catalog.get("datasets", {})

    def best(name: str, when: str) -> str | None:
        entries = datasets.get(name, {}).get("dates", {}).get(when) or []
        if isinstance(entries, Mapping):
            entries = [entries]
        chosen = select_best([e for e in entries if isinstance(e, Mapping)])
        return str(chosen.get("version")) if chosen and chosen.get("version") else None

    refs = manifest.get("input_refs")
    if isinstance(refs, list) and refs:
        recorded_kline = {r["trade_date"] for r in refs if r.get("dataset") == "daily_kline"}
        for ref in refs:
            current = best(str(ref.get("dataset")), str(ref.get("trade_date")))
            if current is not None and current != ref.get("version"):
                return True
        if (manifest.get("window") or {}).get("fixed_days"):
            return False   # ladder_daily (C577): the visited days are recorded exactly; every ref was compared above
        max_days = int((manifest.get("window") or {}).get("max_days", DEFAULT_MAX_DAYS))
        current_days = sorted(d for d in datasets.get("daily_kline", {}).get("dates", {}) if d <= day)[-max_days:]
        return set(current_days) != recorded_kline and bool(current_days)
    recorded = (manifest.get("inputs") or {}).get("daily_kline")
    if isinstance(recorded, Mapping) and recorded.get("version"):
        current = best("daily_kline", day)
        return current is not None and current != recorded["version"]
    return False
