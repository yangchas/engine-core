"""Read-only, injectable live slices for isolated auction/opening observation."""
from __future__ import annotations

import json
import statistics
import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from .auction_tracking import Tick, derive_state, _at_limit


def load_daily_history_csv(path: str | Path) -> list[dict[str, Any]]:
    """Load the supplied daily-kline file without filling absent values."""
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


def _limit_pct(symbol: str) -> float:
    if symbol.startswith(("300", "301", "688", "689")): return 20.0
    if symbol.startswith(("4", "8", "92")): return 30.0
    return 10.0


def daily_history_context(rows: list[Mapping[str, Any]], trade_date: str,
                          states: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Derive as-of streak/position context and 72-session opening benchmarks."""
    by_day: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        day, symbol = str(row.get("date") or "")[:10], str(row.get("symbol") or "").zfill(6)
        if day and day < trade_date and symbol:
            by_day[day][symbol] = row
    days = sorted(by_day)
    stock_context: dict[str, dict[str, Any]] = {}
    for symbol in states:
        own = [(day, by_day[day][symbol]) for day in days if symbol in by_day[day]]
        closes = [row.get("close") for _, row in own if row.get("close") is not None]
        position = None
        if len(closes) >= 20:
            window = closes[-20:]
            if max(window) != min(window): position = (window[-1] - min(window)) / (max(window) - min(window))
        streak, last_day = 0, days[-1] if days else None
        reverse_days = list(reversed(days))
        for index, day in enumerate(reverse_days):
            row = by_day[day].get(symbol)
            previous = by_day[reverse_days[index + 1]].get(symbol) if index + 1 < len(reverse_days) else None
            close, preclose = row.get("close") if row else None, previous.get("close") if previous else None
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
    provider = getattr(td, "states_at", None)
    return provider(cutoff) if callable(provider) else _sql_states(td, cutoff)


def _latest_tick(td: Any, cutoff: datetime) -> datetime | None:
    getter = getattr(td, "latest_tick_ts", None)
    if callable(getter):
        value = getter()
        if value is None: return None
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    cursor = td.cursor() if callable(getattr(td, "cursor", None)) else td
    start = cutoff.replace(hour=9, minute=14, second=0, microsecond=0)
    cursor.execute("select max(ts) from stock_tick_v2 "
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
    prices = [float(row["pct_change"]) for row in states.values() if row.get("pct_change") is not None]
    amounts = [int(row.get("match_amount_yuan") or 0) for row in states.values()]
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
    relations, plate_names = relations or {}, plate_names or {}
    plates = []
    for code, members in relations.items():
        selected = [states[s] for s in members if s in states and states[s].get("pct_change") is not None]
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
            "market_match_amount_yuan": total,
            "vs_previous_day_final": total / previous_day_final_yuan if previous_day_final_yuan and total else None,
            "median_pct_change": statistics.median(prices) if prices else None,
            "up_share_pct": 100 * sum(value > 0 for value in prices) / len(prices) if prices else None,
            "limit_up": sum(_at_limit(row, "up") for row in states.values()),
            "limit_down": sum(_at_limit(row, "down") for row in states.values()),
            "top_plates_by_share": plates[:top_n], "top_stocks_by_amount": stocks,
            "source": "TD stock_tick_v2 (read-only)", "isolated": True}


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
