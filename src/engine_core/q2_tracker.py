"""Lightweight read-only Q2 polling snapshots for post-opening follow-up."""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping

from .live_session import publish_isolated

ISOLATED_PREFIX = "core:cache:live_tracking:"


def _float(row: Mapping[str, Any], key: str) -> float | None:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _int(row: Mapping[str, Any], key: str) -> int | None:
    value = _float(row, key)
    return int(value) if value is not None else None


def _trade_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError("trade_date must be YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("trade_date must be YYYY-MM-DD")
    return value


def read_q2_hashes(redis: Any, *, batch_size: int = 2000) -> dict[str, dict[str, Any]]:
    """Read q2 hashes with a single pipelined HGETALL batch per key scan."""
    keys = sorted(key for key in redis.scan_iter(match="q2:[0-9]*", count=batch_size)
                  if len(key) == 9 and key.startswith("q2:") and key[3:].isdigit())
    output: dict[str, dict[str, Any]] = {}
    # Batch to keep memory bounded if an unexpected keyspace expansion occurs.
    for offset in range(0, len(keys), batch_size):
        batch = keys[offset:offset + batch_size]
        pipe = redis.pipeline()
        for key in batch:
            pipe.hgetall(key)
        for key, row in zip(batch, pipe.execute()):
            if isinstance(row, Mapping):
                output[key[3:]] = dict(row)
    return output


class Q2Tracker:
    def __init__(self, relations: Mapping[str, list[str] | set[str]],
                 baseline: Mapping[str, Mapping[str, Any]], *,
                 high_limit_pool: Mapping[str, Any] | list[str] | list[Mapping[str, Any]] = (),
                 plate_names: Mapping[str, str] | None = None, stale_after_seconds: int = 120,
                 emergent_threshold_pct: float = 5.0):
        if isinstance(stale_after_seconds, bool) or not isinstance(stale_after_seconds, int) or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.relations = {str(code): tuple(str(s).zfill(6) for s in members)
                          for code, members in relations.items()}
        self.baseline = baseline
        self.plate_names = plate_names or {}
        self.stale_after_seconds = stale_after_seconds
        self.emergent_threshold_pct = float(emergent_threshold_pct)
        if isinstance(high_limit_pool, Mapping):
            self.high_stocks = {str(symbol).zfill(6): self._streak(value)
                                for symbol, value in high_limit_pool.items()
                                if self._streak(value) >= 2}
        else:
            self.high_stocks = {}
            for item in high_limit_pool:
                if isinstance(item, Mapping):
                    symbol = str(item.get("symbol", "")).zfill(6)
                    streak = self._streak(item)
                else:
                    symbol, streak = str(item).zfill(6), 2
                if symbol.isdigit() and len(symbol) == 6 and streak >= 2:
                    self.high_stocks[symbol] = streak
        self._high_state: dict[str, str] = {}

    @staticmethod
    def _streak(value: Any) -> int:
        if isinstance(value, Mapping):
            value = value.get("streak", value.get("boards", value.get("ban_count", 0)))
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def observe(self, rows: Mapping[str, Mapping[str, Any]], observed_at: datetime) -> dict[str, Any]:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        observed = observed_at.astimezone(timezone.utc)
        iso_time = observed_at.isoformat()
        stocks: dict[str, dict[str, Any]] = {}
        for raw_symbol, raw in rows.items():
            symbol = str(raw_symbol).zfill(6)
            if not symbol.isdigit() or len(symbol) != 6 or not isinstance(raw, Mapping):
                continue
            px, pc, stamp = _float(raw, "px"), _float(raw, "pc"), _int(raw, "ts")
            pct = (px / pc - 1) * 100 if px is not None and pc is not None and px > 0 and pc > 0 else None
            age = max(0.0, observed.timestamp() - stamp / 1000) if stamp is not None and stamp > 0 else None
            stale = age is None or age > self.stale_after_seconds
            stocks[symbol] = {
                "symbol": symbol, "pct_change": pct,
                "amount_yuan": _float(raw, "amt"),
                "spd1m_bp": _float(raw, "spd1m"),
                "limit_state": _int(raw, "ls"), "ts": stamp,
                "age_seconds": round(age, 3) if age is not None else None,
                "status": "STALE" if stale else "FRESH",
            }

        fresh = [row for row in stocks.values() if row["status"] == "FRESH" and row["pct_change"] is not None]
        pcts = [row["pct_change"] for row in fresh]
        amounts = [row["amount_yuan"] for row in fresh if row["amount_yuan"] is not None]
        speeds = [row["spd1m_bp"] for row in fresh if row["spd1m_bp"] is not None]
        market = {
            "symbol_count": len(stocks),
            "fresh_count": sum(row["status"] == "FRESH" for row in stocks.values()),
            "priced_fresh_count": len(fresh),
            "stale_count": sum(row["status"] == "STALE" for row in stocks.values()),
            "median_pct": statistics.median(pcts) if pcts else None,
            "up_count": sum(v > 0 for v in pcts), "down_count": sum(v < 0 for v in pcts),
            "amount_yuan": sum(amounts) if amounts else None,
            "spd1m_bp_mean": statistics.mean(speeds) if speeds else None,
            "limit_up_count": sum(row["limit_state"] == 1 for row in fresh),
            "limit_down_count": sum(row["limit_state"] == -1 for row in fresh),
        }

        plates = []
        for code, members in self.relations.items():
            selected = [stocks[s] for s in members if s in stocks and stocks[s]["status"] == "FRESH"
                        and stocks[s]["pct_change"] is not None]
            if not selected:
                continue
            current_pct = statistics.mean(row["pct_change"] for row in selected)
            current_speeds = [row["spd1m_bp"] for row in selected if row["spd1m_bp"] is not None]
            amounts = [row["amount_yuan"] for row in selected if row["amount_yuan"] is not None]
            base25 = [self.baseline[s].get("p25") for s in members
                      if s in self.baseline and self.baseline[s].get("p25") is not None]
            base45 = [self.baseline[s].get("p45") for s in members
                      if s in self.baseline and self.baseline[s].get("p45") is not None]
            p25 = statistics.mean(base25) if base25 else None
            p45 = statistics.mean(base45) if base45 else None
            plates.append({"code": code, "name": self.plate_names.get(code, code),
                           "member_count": len(selected), "equal_weight_pct": current_pct,
                           "amount_yuan": sum(amounts) if amounts else None,
                           "spd1m_bp_mean": statistics.mean(current_speeds) if current_speeds else None,
                           "vs_0925_pp": current_pct - p25 if p25 is not None else None,
                           "vs_0945_pp": current_pct - p45 if p45 is not None else None})
        plates.sort(key=lambda row: (-(row["amount_yuan"] or 0), row["code"]))

        high = {}
        for symbol, streak in self.high_stocks.items():
            row = stocks.get(symbol)
            if row is None:
                continue
            prev = self._high_state.get(symbol)
            if row["status"] == "STALE":
                high[symbol] = {**row, "streak": streak, "state": prev or "STALE",
                                "transition_at": None, "first_transition_at": None}
                continue
            limit_state = row["limit_state"]
            if limit_state == -1:
                state = "跌停"
            elif limit_state == 1:
                state = "回封" if prev == "开板" else "封死"
            elif prev in ("封死", "回封"):
                state = "开板"
            elif row["pct_change"] is not None and row["pct_change"] < 0:
                state = "走弱"
            else:
                state = "观察"
            changed = state != prev
            high[symbol] = {**row, "streak": streak, "state": state,
                            "transition_at": iso_time if changed else None,
                            "first_transition_at": iso_time if changed and prev is None else None}
            self._high_state[symbol] = state

        emergent = {}
        for symbol, row in stocks.items():
            base = self.baseline.get(symbol, {})
            p25, p45 = base.get("p25"), base.get("p45")
            if p25 is None or p45 is None:
                continue
            try:
                qualified = float(p25) < 3 and float(p45) >= 5
            except (TypeError, ValueError):
                qualified = False
            if qualified:
                current = row["pct_change"]
                alive = (current >= self.emergent_threshold_pct
                         if current is not None and row["status"] == "FRESH" else None)
                emergent[symbol] = {**row, "alive": alive}
        result = {"observed_at": iso_time, "source": "Redis q2 (read-only)", "market": market,
                  "plates": plates, "stocks": stocks, "high_stocks": high, "emergent": emergent,
                  "emergent_summary": {"candidate_count": len(emergent),
                                       "alive_count": sum(row["alive"] is True for row in emergent.values()),
                                       "dropped_count": sum(row["alive"] is False for row in emergent.values()),
                                       "stale_or_unknown_count": sum(row["alive"] is None for row in emergent.values())}}
        return result


def publish_q2_snapshot(redis: Any, trade_date: str, payload: Mapping[str, Any], ttl: int,
                        *, suffix: str | None = None, key: str | None = None) -> str:
    trade_date = _trade_date(trade_date)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise ValueError("ttl must be a positive integer")
    expected_prefix = f"{ISOLATED_PREFIX}{trade_date}:q2:"
    target = key or expected_prefix + (suffix or "latest")
    if not isinstance(target, str) or not target.startswith(expected_prefix) or target == expected_prefix:
        raise ValueError("Redis key must use the dated isolated q2 prefix")
    # Delegate the common TTL validation and isolated prefix publication.
    publish_isolated(redis, trade_date, payload, ttl, key=target)
    return target


def poll_once(redis: Any, tracker: Q2Tracker, trade_date: str, ttl: int,
              *, observed_at: datetime | None = None) -> dict[str, Any]:
    stamp = observed_at or datetime.now().astimezone()
    payload = tracker.observe(read_q2_hashes(redis), stamp)
    minute_key = stamp.strftime("%H%M")
    publish_q2_snapshot(redis, trade_date, payload, ttl, suffix="latest")
    publish_q2_snapshot(redis, trade_date, payload, ttl, suffix=f"minutes:{minute_key}")
    return payload


def run_polling(redis: Any, tracker: Q2Tracker, trade_date: str, ttl: int, *, interval: int = 60,
                once: bool = False, logger=print) -> None:
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError("interval must be positive")
    while True:
        payload = poll_once(redis, tracker, trade_date, ttl)
        logger(json.dumps({"observed_at": payload["observed_at"], "market": payload["market"],
                           "plate_count": len(payload["plates"]),
                           "emergent": payload["emergent_summary"]}, ensure_ascii=False, separators=(",", ":")))
        if once:
            return
        time.sleep(interval)
