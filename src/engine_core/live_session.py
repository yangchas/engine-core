"""Read-only, injectable live slices for isolated auction/opening observation."""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Any, Mapping

from .auction_tracking import Tick, derive_state, _at_limit

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
