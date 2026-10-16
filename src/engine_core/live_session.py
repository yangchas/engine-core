"""Read-only, injectable live slices for isolated auction/opening observation."""
from __future__ import annotations

import json
import math
import statistics
import csv
import functools
import hashlib
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from .auction_tracking import Tick, derive_state, _at_limit

MARKET_VOLUME_THRESHOLDS = {"high_amount_multiple": 1.25, "low_amount_multiple": 0.85,
                            "down_up_share_pct": 25.0, "up_up_share_pct": 50.0}
TOP100_NORMAL_RANGE_PCT = (40.0, 47.0)
TOP100_NORMAL_MIDPOINT_PCT = sum(TOP100_NORMAL_RANGE_PCT) / 2


def market_price_volume_status(amount_multiple: float | None, weighted_pct: float | None,
                               up_share_pct: float | None) -> str:
    """Descriptive, uncalibrated market state rules; never a combined score."""
    thresholds = MARKET_VOLUME_THRESHOLDS
    if amount_multiple is not None and amount_multiple >= thresholds["high_amount_multiple"]:
        if weighted_pct is not None and weighted_pct < 0 and up_share_pct is not None and up_share_pct < thresholds["down_up_share_pct"]:
            return "放量下杀"
        if weighted_pct is not None and weighted_pct > 0 and up_share_pct is not None and up_share_pct >= thresholds["up_up_share_pct"]:
            return "放量上攻"
    if amount_multiple is not None and amount_multiple <= thresholds["low_amount_multiple"]:
        return "缩量"
    return "量价无明显一致方向"


def load_daily_history_csv(path: str | Path, trade_date: str | None = None) -> list[dict[str, Any]]:
    """Load an immutable compact slice when available, otherwise parse CSV once."""
    source = Path(path)
    if trade_date:
        root = Path(os.environ.get("CORE_SLICE_ROOT", str(Path.home() / "core-data" / "slices"))).expanduser()
        started = time.monotonic()
        try:
            st = source.stat()
            rows = _load_cached_slice(str(root), trade_date, str(source), st.st_mtime_ns, st.st_size)
            if rows is not None:
                return rows
        except OSError:
            pass
        elapsed = time.monotonic() - started
        print(f"SLICE_MISSING_FALLBACK daily_history trade_date={trade_date} elapsed={elapsed:.3f}s",
              file=sys.stderr)
        return _load_daily_history_csv_cached(str(source), source.stat().st_mtime_ns,
                                               source.stat().st_size)
    return _load_daily_history_csv_cached(str(source), source.stat().st_mtime_ns,
                                           source.stat().st_size)


@functools.lru_cache(maxsize=8)
def _load_cached_slice(root: str, trade_date: str, source: str,
                       source_mtime_ns: int, source_size: int):
    from .slice_cache import load_daily_history_slice, sha256_file
    rows, manifest = load_daily_history_slice(root, trade_date)
    if rows is None or manifest is None:
        return None
    source_path = Path(source)
    if source_path.stat().st_size != source_size or sha256_file(source_path) != manifest.get("source_sha256"):
        return None
    return rows


@functools.lru_cache(maxsize=4)
def _load_daily_history_csv_cached(path: str, mtime_ns: int, size: int) -> list[dict[str, Any]]:
    """The no-slice fallback; keying on file identity avoids repeat CSV parses."""
    rows = []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").strip().zfill(6)
            day = str(row.get("date") or row.get("ts") or "")[:10]
            if not symbol or not day:
                continue
            parsed: dict[str, Any] = {"date": day, "symbol": symbol}
            for key in ("open", "high", "low", "close", "pct_chg", "amount_yuan"):
                try: parsed[key] = float(row[key]) if row.get(key) not in (None, "") else None
                except (TypeError, ValueError): parsed[key] = None
            rows.append(parsed)
    return rows


def load_auction_amount_baseline_csv(path: str | Path | None,
                                    trade_date: str) -> list[dict[str, Any]] | None:
    """Load valid daily auction totals strictly before trade_date, in billion yuan."""
    if not path:
        return None
    rows: list[dict[str, Any]] = []
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                day = str(row.get("date") or "")[:10]
                if not day or day >= trade_date:
                    continue
                try:
                    amount = float(row.get("sum_yi", ""))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(amount) and amount >= 0:
                    rows.append({"date": day, "sum_yi": amount})
    except OSError:
        return None
    return rows


def auction_amount_baseline_comparison(rows: list[Mapping[str, Any]] | None,
                                       trade_date: str,
                                       current_sum_yi: float | None) -> dict[str, Any] | None:
    """Compare one auction total with prior-date history using a midrank percentile."""
    if not rows or current_sum_yi is None or not math.isfinite(current_sum_yi):
        return None
    eligible = []
    for row in rows:
        day = str(row.get("date") or "")[:10]
        try:
            amount = float(row.get("sum_yi"))
        except (TypeError, ValueError):
            continue
        if day and day < trade_date and math.isfinite(amount) and amount >= 0:
            eligible.append((day, amount))
    if not eligible:
        return None
    amounts = [amount for _, amount in eligible]
    median = statistics.median(amounts)
    less = sum(amount < current_sum_yi for amount in amounts)
    equal = sum(amount == current_sum_yi for amount in amounts)
    return {"sample_days": len(amounts), "median_yi": median,
            "multiple": current_sum_yi / median if median else None,
            "percentile": 100 * (less + equal / 2) / len(amounts),
            "minimum_yi": min(amounts), "maximum_yi": max(amounts),
            "earliest_date": min(day for day, _ in eligible),
            "latest_date": max(day for day, _ in eligible)}


def _limit_pct(symbol: str) -> float:
    if symbol.startswith(("300", "301", "688", "689")): return 20.0
    if symbol.startswith(("4", "8", "92")): return 30.0
    return 10.0


_DAILY_HISTORY_CONTEXT_CACHE: dict[tuple[int, str, tuple[tuple[str, Any], ...]],
                                   tuple[list[Mapping[str, Any]], dict[str, Any]]] = {}


def daily_history_context(rows: list[Mapping[str, Any]], trade_date: str,
                          states: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    state_key = tuple(sorted((str(symbol), row.get("pct_change"))
                              for symbol, row in states.items()))
    key = (id(rows), trade_date, state_key)
    cached = _DAILY_HISTORY_CONTEXT_CACHE.get(key)
    if cached is not None and cached[0] is rows:
        return cached[1]
    result = _compute_daily_history_context(rows, trade_date, states)
    if len(_DAILY_HISTORY_CONTEXT_CACHE) >= 4:
        _DAILY_HISTORY_CONTEXT_CACHE.pop(next(iter(_DAILY_HISTORY_CONTEXT_CACHE)))
    _DAILY_HISTORY_CONTEXT_CACHE[key] = (rows, result)
    return result


def _compute_daily_history_context(rows: list[Mapping[str, Any]], trade_date: str,
                                   states: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Derive as-of streak/position context and 72-session opening benchmarks."""
    by_day: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        day, symbol = str(row.get("date") or "")[:10], str(row.get("symbol") or "").zfill(6)
        if day and day < trade_date and symbol:
            by_day[day][symbol] = row
    days = sorted(by_day)
    day_index = {day: index for index, day in enumerate(days)}
    by_symbol: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    for day in days:
        for symbol, row in by_day[day].items():
            by_symbol[symbol].append((day, row))
    stock_context: dict[str, dict[str, Any]] = {}
    for symbol in states:
        own = by_symbol.get(symbol, [])
        closes = [row.get("close") for _, row in own if row.get("close") is not None]
        position = None
        if len(closes) >= 20:
            window = closes[-20:]
            if max(window) != min(window): position = (window[-1] - min(window)) / (max(window) - min(window))
        streak, last_day = 0, days[-1] if days else None
        if own and own[-1][0] == last_day:
            for index in range(len(own) - 1, 0, -1):
                day, row = own[index]
                previous_day, previous = own[index - 1]
                if day_index[day] != day_index[previous_day] + 1:
                    break
                close, preclose = row.get("close"), previous.get("close")
                if close is None or preclose in (None, 0): break
                limit_price = (Decimal(str(preclose)) * (Decimal(1) + Decimal(str(_limit_pct(symbol))) / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                close_price = Decimal(str(close)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if close_price != limit_price: break
                streak += 1
        stock_context[symbol] = {"streak": streak, "position_20d": position,
                                 "position_close_days": min(20, len(closes)), "last_history_date": last_day,
                                 "prev_day_amount_yuan": by_day[last_day][symbol].get("amount_yuan") if last_day and symbol in by_day[last_day] else None}

    opening_by_day: dict[str, list[float]] = defaultdict(list)
    for index, day in enumerate(days):
        if index == 0: continue
        previous = by_day[days[index - 1]]
        for symbol, row in by_day[day].items():
            prev = previous.get(symbol)
            op, pc = row.get("open"), prev.get("close") if prev else None
            if op is not None and pc not in (None, 0): opening_by_day[day].append((op / pc - 1) * 100)
    history = []
    for day in days[-72:]:
        vals = opening_by_day.get(day, [])
        if vals:
            history.append({"date": day, "median_open_pct": statistics.median(vals),
                            "up_share_pct": 100 * sum(value > 0 for value in vals) / len(vals),
                            "symbol_count": len(vals)})
    current = [float(row["pct_change"]) for row in states.values() if row.get("pct_change") is not None]
    median_now = statistics.median(current) if current else None
    up_now = 100 * sum(value > 0 for value in current) / len(current) if current else None
    med_min = min(history, key=lambda item: item["median_open_pct"]) if history else None
    up_min = min(history, key=lambda item: item["up_share_pct"]) if history else None
    return {"stocks": stock_context, "history": {
        "session_count": len(history), "current_median_open_pct": median_now,
        "current_up_share_pct": up_now, "median_is_historical_low": median_now is not None and med_min is not None and median_now <= med_min["median_open_pct"],
        "up_share_is_historical_low": up_now is not None and up_min is not None and up_now <= up_min["up_share_pct"],
        "history_min_median_date": med_min["date"] if med_min else None,
        "history_min_median_open_pct": med_min["median_open_pct"] if med_min else None,
        "history_min_up_share_date": up_min["date"] if up_min else None,
        "history_min_up_share_pct": up_min["up_share_pct"] if up_min else None,
        "daily": history}}

ISOLATED_PREFIX = "core:cache:live_tracking:"


def _as_datetime(value: datetime | str) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _sql_states(td: Any, cutoff: datetime) -> dict[str, dict[str, Any]]:
    # TD's last() aggregation is bounded to the current session; it avoids exporting tick history.
    cursor = td.cursor() if callable(getattr(td, "cursor", None)) else td
    start = cutoff.replace(hour=9, minute=14, second=0, microsecond=0)
    query = ("select last(ts),last(px_milli),last(pc_milli),last(bp1_milli),last(ap1_milli),"
             "last(bv1),last(av1),last(bp2_milli),last(ap2_milli),last(bv2),last(av2),"
             "last(amt_yuan),last(vol_units),symbol from stock_tick_v2 "
             f"where ts>='{start:%Y-%m-%d %H:%M:%S}' and ts<='{cutoff:%Y-%m-%d %H:%M:%S}' group by symbol")
    cursor.execute(query)
    result = {}
    columns = ("ts", "px_milli", "pc_milli", "bp1_milli", "ap1_milli", "bv1", "av1",
               "bp2_milli", "ap2_milli", "bv2", "av2", "amt_yuan", "vol_units", "symbol")
    for raw in cursor.fetchall():
        row = raw if isinstance(raw, Mapping) else dict(zip(columns, raw))
        symbol, pc, ts = row.get("symbol"), row.get("pc_milli"), row.get("ts")
        if symbol is None or not pc or not ts:
            continue
        tick_time = ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else str(ts)[-8:]
        tick = Tick(trade_date=cutoff.date().isoformat(), ts=tick_time, symbol=str(symbol).zfill(6),
                    **{key: int(row.get(key) or 0) for key in Tick.__dataclass_fields__
                       if key not in {"trade_date", "ts", "symbol", "name"}})
        result[tick.symbol] = derive_state(tick)
        result[tick.symbol]["source_ts"] = ts.isoformat(sep=" ") if isinstance(ts, datetime) else str(ts)
    return result


def states_at(td: Any, cutoff: datetime | str) -> dict[str, dict[str, Any]]:
    """Get per-symbol state at cutoff from an injected fake/provider or bounded TD last() query."""
    cutoff = _as_datetime(cutoff)
    if cutoff < datetime.now() and _state_cache_enabled():
        cached = _read_checkpoint_cache(cutoff)
        if cached is not None:
            return cached
    provider = getattr(td, "states_at", None)
    states = provider(cutoff) if callable(provider) else _sql_states(td, cutoff)
    if cutoff < datetime.now() and _state_cache_enabled():
        try:
            if checkpoint_ready(td, cutoff):
                _write_checkpoint_cache(cutoff, states)
        except Exception:
            # Checkpoint caching is opportunistic; the authoritative TD read already succeeded.
            pass
    return states


def _state_cache_enabled() -> bool:
    return os.environ.get("CORE_STATE_CACHE", "on").lower() != "off"


def _checkpoint_cache_path(cutoff: datetime) -> Path:
    root = Path(os.environ.get("CORE_SLICE_ROOT", str(Path.home() / "core-data" / "slices"))).expanduser()
    label = cutoff.strftime("%H%M%S")
    return root / "states" / cutoff.strftime("%Y-%m-%d") / f"{label}.json"


def _read_checkpoint_cache(cutoff: datetime) -> dict[str, dict[str, Any]] | None:
    path = _checkpoint_cache_path(cutoff)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        states = data.get("states")
        raw = json.dumps(states, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if (data.get("schema") != "CheckpointStatesV1"
                or data.get("cutoff") != cutoff.isoformat(sep=" ") or not isinstance(states, dict)
                or data.get("symbol_count") != len(states)
                or data.get("sha256") != hashlib.sha256(raw).hexdigest()):
            return None
        return states
    except (OSError, ValueError, TypeError):
        return None


def _write_checkpoint_cache(cutoff: datetime, states: Mapping[str, Mapping[str, Any]]) -> None:
    normalized = {str(symbol).zfill(6): dict(row) for symbol, row in states.items()}
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    data = {"schema": "CheckpointStatesV1", "cutoff": cutoff.isoformat(sep=" "),
            "symbol_count": len(normalized),
            "sha256": hashlib.sha256(raw).hexdigest(), "states": normalized}
    path = _checkpoint_cache_path(cutoff)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _latest_tick(td: Any, cutoff: datetime) -> datetime | None:
    getter = getattr(td, "latest_tick_ts", None)
    if callable(getter):
        value = getter()
        if value is None: return None
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    cursor = td.cursor() if callable(getattr(td, "cursor", None)) else td
    start = cutoff.replace(hour=9, minute=14, second=0, microsecond=0)
    cursor.execute("select last(ts) from stock_tick_v2 "
                   f"where ts>='{start:%Y-%m-%d %H:%M:%S}' and ts<='{cutoff:%Y-%m-%d %H:%M:%S}'")
    rows = cursor.fetchall()
    value = rows[0][0] if rows and rows[0] else None
    if value is None: return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def checkpoint_ready(td: Any, ts: datetime | str) -> bool:
    """True only when TD's latest stored tick reaches the requested checkpoint."""
    cutoff = _as_datetime(ts)
    latest = _latest_tick(td, cutoff)
    return latest is not None and latest >= cutoff


def build_live_slice(states: Mapping[str, Mapping[str, Any]], asof: datetime | str, *,
                     previous_day_final_yuan: int | None = None,
                     relations: Mapping[str, set[str]] | None = None,
                     plate_names: Mapping[str, str] | None = None,
                     top_n: int = 15, stale_after_seconds: float = 30) -> dict[str, Any]:
    stamp = _as_datetime(asof)
    priced = [(row, float(row["pct_change"])) for row in states.values()
              if row.get("pct_change") is not None and _finite_number(row.get("pct_change"))]
    prices = [pct for _, pct in priced]
    amounts = [max(0, int(row.get("match_amount_yuan") or 0)) for row, _ in priced]
    latest_values = []
    for row in states.values():
        value = row.get("source_ts")
        if value:
            try: latest_values.append(datetime.fromisoformat(str(value)))
            except ValueError: pass
        elif row.get("ts"):
            try: latest_values.append(datetime.combine(stamp.date(), datetime.strptime(row["ts"], "%H:%M:%S").time()))
            except ValueError: pass
    lag = max(0.0, (stamp - max(latest_values)).total_seconds()) if latest_values else None
    total = sum(amounts)
    weighted_pct = (sum(amount * pct for amount, (_, pct) in zip(amounts, priced)) / total) if total else None
    down_amount = sum(amount for amount, (_, pct) in zip(amounts, priced) if pct < 0)
    down_share = 100 * down_amount / total if total else None
    up_share = 100 * sum(pct > 0 for pct in prices) / len(prices) if prices else None
    ordered_amounts = sorted(amounts, reverse=True)
    top100_concentration = 100 * sum(ordered_amounts[:100]) / total if total else None
    relations, plate_names = relations or {}, plate_names or {}
    plates = []
    for code, members in relations.items():
        selected = [states[s] for s in members if s in states and states[s].get("pct_change") is not None
                    and _finite_number(states[s].get("pct_change"))]
        if len(selected) < 5: continue
        amount = sum(int(s.get("match_amount_yuan") or 0) for s in selected)
        plates.append({"code": code, "name": plate_names.get(code, code), "member_count": len(selected),
                       "match_amount_yuan": amount, "market_share_bp": round(amount * 10000 / total, 2) if total else None,
                       "equal_weight_pct": statistics.mean(s["pct_change"] for s in selected)})
    plates.sort(key=lambda p: (-(p["market_share_bp"] or 0), p["code"]))
    stocks = sorted((dict(row) for row in states.values()),
                    key=lambda row: (-(int(row.get("match_amount_yuan") or 0)), row.get("symbol", "")))[:top_n]
    return {"asof": stamp.isoformat(timespec="seconds"), "phase": "AUCTION" if stamp.time() < datetime.strptime("09:30", "%H:%M").time() else "OPEN",
            "symbol_count": len(states), "data_lag_seconds": lag,
            "status": "STALE" if lag is None or lag > stale_after_seconds else "FRESH",
            "priced_symbol_count": len(priced),
            "market_match_amount_yuan": total,
            "vs_previous_day_final": total / previous_day_final_yuan if previous_day_final_yuan and total else None,
            "amount_weighted_pct_change": weighted_pct,
            "down_amount_share_pct": down_share,
            "median_pct_change": statistics.median(prices) if prices else None,
            "up_share_pct": up_share,
            "top100_concentration_pct": top100_concentration,
            "top100_concentration_vs_normal_pp": (top100_concentration - TOP100_NORMAL_MIDPOINT_PCT) if top100_concentration is not None else None,
            "top100_concentration_in_normal_range": (TOP100_NORMAL_RANGE_PCT[0] <= top100_concentration <= TOP100_NORMAL_RANGE_PCT[1]) if top100_concentration is not None else None,
            "market_price_volume_state": market_price_volume_status(total / previous_day_final_yuan if previous_day_final_yuan and total else None,
                                                                      weighted_pct, up_share),
            "market_price_volume_thresholds": dict(MARKET_VOLUME_THRESHOLDS),
            "limit_up": sum(_at_limit(row, "up") for row in states.values()),
            "limit_down": sum(_at_limit(row, "down") for row in states.values()),
            "top_plates_by_share": plates[:top_n], "top_stocks_by_amount": stocks,
            "source": "TD stock_tick_v2 (read-only)", "isolated": True}


def _finite_number(value: Any) -> bool:
    try: return math.isfinite(float(value))
    except (TypeError, ValueError): return False


def publish_isolated(redis: Any, trade_date: str, payload: Mapping[str, Any], ttl: int, *, key: str | None = None) -> str:
    if not isinstance(trade_date, str) or len(trade_date) != 10:
        raise ValueError("trade_date must be YYYY-MM-DD")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise ValueError("ttl must be a positive integer")
    target = key or f"{ISOLATED_PREFIX}{trade_date}:latest"
    if not target.startswith(f"{ISOLATED_PREFIX}{trade_date}:"):
        raise ValueError("Redis key must use the isolated live_tracking prefix")
    redis.set(target, json.dumps(payload, ensure_ascii=False, default=str), ex=ttl)
    return target
