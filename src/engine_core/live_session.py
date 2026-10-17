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


def load_daily_history_csv(path: str | Path | list[dict[str, Any]], trade_date: str | None = None) -> list[dict[str, Any]]:
    """Load an immutable compact slice when available, otherwise parse CSV once."""
    if isinstance(path, list):
        return [dict(row) for row in path if isinstance(row, Mapping)]
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


def load_auction_amount_baseline_csv(path: str | Path | list[dict[str, Any]] | None,
                                    trade_date: str) -> list[dict[str, Any]] | None:
    """Load valid daily auction totals strictly before trade_date, in billion yuan."""
    if isinstance(path, list):
        by_day: dict[str, float] = {}
        for row in path:
            row_kind = row.get("row_kind")
            if row_kind is not None and row_kind != "market_observation":
                continue
            day = str(row.get("date") or "")[:10]
            if not day or day >= trade_date:
                continue
            try:
                amount = float(row.get("sum_yi", row.get("auction_amount_yuan")))
                if "sum_yi" not in row:
                    amount /= 1e8
            except (TypeError, ValueError):
                continue
            if math.isfinite(amount) and amount >= 0:
                by_day[day] = amount
        return [{"date": day, "sum_yi": by_day[day]} for day in sorted(by_day)]
    if not path:
        return None
    by_day: dict[str, float] = {}
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                row_kind = row.get("row_kind")
                if row_kind and row_kind != "market_observation":
                    continue
                day = str(row.get("date") or "")[:10]
                if not day or day >= trade_date:
                    continue
                try:
                    amount = float(row.get("sum_yi", ""))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(amount) and amount >= 0:
                    by_day[day] = amount
    except OSError:
        return None
    return [{"date": day, "sum_yi": by_day[day]} for day in sorted(by_day)]


def append_auction_amount_baseline(rows: list[dict[str, Any]] | None, *, trade_date: str,
                                   amount_yuan: int | float | None,
                                   available_at: str) -> list[dict[str, Any]]:
    """Return prior values plus an observed postmarket total, without filling gaps."""
    updated = [dict(row) for row in (rows or []) if str(row.get("date") or "")[:10] != trade_date]
    if amount_yuan is None:
        return updated
    amount = float(amount_yuan)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("invalid_auction_amount")
    updated.append({"date": trade_date, "sum_yi": amount / 100_000_000,
                    "available_at": available_at, "source": "postmarket_auction_summary"})
    return sorted(updated, key=lambda row: str(row.get("date") or ""))


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
    if rows and rows[0].get("feature_type") == "daily_report_features_v1":
        stock_rows = {str(row.get("symbol", "")).zfill(6): row for row in rows
                      if row.get("symbol") != "__MARKET__"}
        market = next((row for row in rows if row.get("symbol") == "__MARKET__"), {})
        history = list(market.get("opening_history_72") or [])
        current = [float(row["pct_change"]) for row in states.values()
                   if row.get("pct_change") is not None]
        median_now = statistics.median(current) if current else None
        up_now = 100 * sum(value > 0 for value in current) / len(current) if current else None
        med_min = min(history, key=lambda item: item["median_open_pct"]) if history else None
        up_min = min(history, key=lambda item: item["up_share_pct"]) if history else None
        stocks = {symbol: {"streak": row.get("streak"), "position_20d": row.get("position_20d"),
                           "position_close_days": row.get("position_close_days", 0),
                           "last_history_date": row.get("last_history_date"),
                           "prev_day_amount_yuan": row.get("prev_day_amount_yuan")}
                   for symbol, row in stock_rows.items()}
        return {"stocks": stocks, "history": {
            "session_count": len(history), "current_median_open_pct": median_now,
            "current_up_share_pct": up_now,
            "median_is_historical_low": median_now is not None and med_min is not None and median_now <= med_min["median_open_pct"],
            "up_share_is_historical_low": up_now is not None and up_min is not None and up_now <= up_min["up_share_pct"],
            "history_min_median_date": med_min["date"] if med_min else None,
            "history_min_median_open_pct": med_min["median_open_pct"] if med_min else None,
            "history_min_up_share_date": up_min["date"] if up_min else None,
            "history_min_up_share_pct": up_min["up_share_pct"] if up_min else None,
            "daily": history}}
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
        numeric_fields = [key for key in Tick.__dataclass_fields__
                          if key not in {"trade_date", "ts", "symbol", "name"}]
        missing = {key for key in numeric_fields if row.get(key) is None}
        tick = Tick(trade_date=cutoff.date().isoformat(), ts=tick_time, symbol=str(symbol).zfill(6),
                    **{key: int(row[key]) if row.get(key) is not None else 0 for key in numeric_fields})
        state = derive_state(tick)
        # Mirror derive_state: from 09:25:00 a positive amt_yuan is the final print (amount independent of bv1/av1).
        final_print = "09:25:00" <= tick_time < "09:30:00" and tick.amt_yuan > 0
        auction = tick_time <= "09:25:00" and not final_print
        if auction:
            required = {
                "bp1_milli": ("price_milli", "pct_change", "match_amount_yuan", "rest_bid_yuan"),
                "ap1_milli": ("price_milli", "match_amount_yuan", "rest_ask_yuan"),
                "bv1": ("match_amount_yuan",), "av1": ("match_amount_yuan",),
                "pc_milli": ("pct_change",),
                "bv2": ("rest_bid_shares", "rest_bid_yuan"),
                "av2": ("rest_ask_shares", "rest_ask_yuan"),
            }
        else:
            required = {
                "amt_yuan": ("match_amount_yuan", "amt_yuan"),
                "px_milli": ("price_milli", "pct_change"),
                "pc_milli": ("pct_change",),
                "bp1_milli": ("rest_bid_yuan",),
                "ap1_milli": ("rest_ask_yuan",),
                "bv2": ("rest_bid_shares", "rest_bid_yuan"),
                "av2": ("rest_ask_shares", "rest_ask_yuan"),
            }
        missing_outputs = {output for source in missing for output in required.get(source, ())}
        for key in missing_outputs:
            state[key] = None
        state["field_quality"] = {key: "MISSING" for key in missing}
        state["missing_field_count"] = len(missing)
        result[tick.symbol] = state
        result[tick.symbol]["source_ts"] = ts.isoformat(sep=" ") if isinstance(ts, datetime) else str(ts)
    return result


def states_at(td: Any, cutoff: datetime | str) -> dict[str, dict[str, Any]]:
    """Get per-symbol state at cutoff from an injected fake/provider or bounded TD last() query."""
    cutoff = _as_datetime(cutoff)
    cacheable = cutoff < datetime.now() and _state_cache_enabled()
    if cacheable:
        from . import live_state_cache
        cached = live_state_cache.read(cutoff)
        if cached is not None:
            return cached
        # Preserve the explicit legacy test/caller override; production reads use
        # the verified Core cache so old metadata-free blobs cannot shadow it.
        if os.environ.get("CORE_SLICE_ROOT"):
            cached = _read_checkpoint_cache(cutoff)
            if cached is not None:
                return cached
    provider = getattr(td, "states_at", None)
    sql_path = not callable(provider)
    states = provider(cutoff) if callable(provider) else _sql_states(td, cutoff)
    # Only publish a complete market-wide checkpoint. A custom/provider test seam
    # may not expose readiness; in production TD connections do.
    cache_complete = bool(states) and len(states) >= 4_000
    ready = cache_complete
    if cache_complete and (sql_path or callable(getattr(td, "latest_tick_ts", None))):
        try:
            ready = checkpoint_ready(td, cutoff)
        except Exception:
            ready = False
    if cacheable and cache_complete and ready:
        try:
            live_state_cache.write(cutoff, states)
            if os.environ.get("CORE_SLICE_ROOT"):
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
                or data.get("symbol_count") != len(states) or len(states) < 4_000
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
    amount_known = [(row, pct, max(0, int(row["match_amount_yuan"]))) for row, pct in priced
                    if row.get("match_amount_yuan") is not None]
    all_amount_known = [row for row in states.values() if row.get("match_amount_yuan") is not None]
    amounts = [amount for _, _, amount in amount_known]
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
    total_known = sum(amounts)
    amount_complete = bool(states) and len(all_amount_known) == len(states)
    total = total_known if amount_complete else None
    weighted_pct = (sum(amount * pct for _, pct, amount in amount_known) / total_known
                    if total_known and amount_complete else None)
    down_amount = sum(amount for _, pct, amount in amount_known if pct < 0)
    down_share = 100 * down_amount / total_known if total_known and amount_complete else None
    up_share = 100 * sum(pct > 0 for pct in prices) / len(prices) if prices else None
    ordered_amounts = sorted(amounts, reverse=True)
    top100_concentration = 100 * sum(ordered_amounts[:100]) / total_known if total_known and amount_complete else None
    limit_checks = [(_safe_at_limit(row, "up"), _safe_at_limit(row, "down"))
                    for row in states.values()]
    relations, plate_names = relations or {}, plate_names or {}
    plates = []
    for code, members in relations.items():
        selected = [states[s] for s in members if s in states and states[s].get("pct_change") is not None
                    and _finite_number(states[s].get("pct_change"))]
        if len(selected) < 5: continue
        amount_values = [s.get("match_amount_yuan") for s in selected]
        complete = all(value is not None for value in amount_values)
        amount = sum(int(value) for value in amount_values if value is not None) if complete else None
        plates.append({"code": code, "name": plate_names.get(code, code), "member_count": len(selected),
                       "match_amount_yuan": amount, "market_share_bp": round(amount * 10000 / total, 2) if total else None,
                       "equal_weight_pct": statistics.mean(s["pct_change"] for s in selected)})
    plates.sort(key=lambda p: (p["market_share_bp"] is None,
                               -(p["market_share_bp"] if p["market_share_bp"] is not None else 0), p["code"]))
    stocks = sorted((dict(row) for row in states.values()),
                    key=lambda row: (row.get("match_amount_yuan") is None,
                                     -(int(row["match_amount_yuan"]) if row.get("match_amount_yuan") is not None else 0),
                                     row.get("symbol", "")))[:top_n]
    return {"asof": stamp.isoformat(timespec="seconds"), "phase": "AUCTION" if stamp.time() < datetime.strptime("09:30", "%H:%M").time() else "OPEN",
            "symbol_count": len(states), "amount_coverage": len(all_amount_known),
            "amount_missing": len(states) - len(all_amount_known), "data_lag_seconds": lag,
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
            "limit_up": (sum(up is True for up, _down in limit_checks)
                         if not any(up is None for up, _down in limit_checks) else None),
            "limit_down": (sum(down is True for _up, down in limit_checks)
                           if not any(down is None for _up, down in limit_checks) else None),
            "limit_state_missing": sum(up is None or down is None for up, down in limit_checks),
            "top_plates_by_share": plates[:top_n], "top_stocks_by_amount": stocks,
            "source": "TD stock_tick_v2 (read-only)", "isolated": True}


def _finite_number(value: Any) -> bool:
    try: return math.isfinite(float(value))
    except (TypeError, ValueError): return False


def _safe_at_limit(state: Mapping[str, Any], direction: str) -> bool | None:
    rest_key = "rest_bid_shares" if direction == "up" else "rest_ask_shares"
    if any(state.get(key) is None for key in ("price_milli", "prev_close_milli", rest_key)):
        return None
    try:
        return _at_limit(state, direction)
    except (TypeError, ValueError):
        return None


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
