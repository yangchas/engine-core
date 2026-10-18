"""Point-in-time per-stock auction amount and verified buy-one seal facts."""
from __future__ import annotations

import json
import os
from datetime import datetime, time
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .data_pipeline.storage import ArtifactCatalog
from .limit_price import limit_prices

TZ = ZoneInfo("Asia/Shanghai")


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (ValueError, TypeError):
        return None


def build_auction_stock_facts(trade_date: str, asof: str,
                              snapshots: Sequence[Mapping[str, Any]],
                              ticks: Sequence[Mapping[str, Any]], *,
                              tick_volume_unit: str | None = None,
                              st_symbols: set[str] | None = None,
                              unlimited_symbols: set[str] | None = None,
                              now: str | None = None, read_at: str | None = None) -> dict[str, Any]:
    """Build facts from supplied source rows. Rank is competition rank over positive amounts."""
    cutoff = _dt(asof)
    if cutoff is None or cutoff.date().isoformat() != trade_date:
        raise ValueError("invalid_asof_or_trade_date")
    if cutoff.timetz().replace(tzinfo=None) > time(9, 26):
        raise ValueError("auction_asof_after_0926")
    day = trade_date.replace("-", "")
    current = _dt(now) if now is not None else None
    live = current is not None and current.date().isoformat() == trade_date and current >= cutoff
    read_time = _dt(read_at) if read_at is not None else (current if live else None)
    if live and (read_time is None or read_time.date().isoformat() != trade_date or read_time > current):
        raise ValueError("invalid_live_read_at")
    eligible_ticks: dict[str, list[Mapping[str, Any]]] = {}
    for tick in ticks:
        symbol = str(tick.get("symbol") or "").zfill(6)
        source_ts = _dt(tick.get("source_ts") or tick.get("ts"))
        if (source_ts and source_ts.date().strftime("%Y%m%d") == day
                and datetime.combine(source_ts.date(), time(9, 25), TZ) <= source_ts <= cutoff
                ):
            eligible_ticks.setdefault(symbol, []).append(tick)
    latest_ticks = {symbol: max(rows, key=lambda row: _dt(row.get("source_ts") or row.get("ts")))
                    for symbol, rows in eligible_ticks.items()}
    facts = []
    for snap in snapshots:
        symbol = str(snap.get("symbol") or "").zfill(6)
        amount = _number(snap.get("auction_amount_yuan"))
        if amount is not None and amount <= 0:
            amount = None
        px = _number(snap.get("price_milli"))
        change = _number(snap.get("change_bp"))
        preclose_milli = _number(snap.get("pre_close_milli"))
        preclose = preclose_milli / 1000 if preclose_milli is not None else None
        official = limit_prices(preclose, symbol, is_st=symbol in (st_symbols or set()),
                                unlimited=symbol in (unlimited_symbols or set())) if preclose else None
        limit_yuan = official[0] if official else None
        tick = latest_ticks.get(symbol)
        status, reason, seal_amount = "UNKNOWN", "NO_FINAL_TICK", None
        limit_source = "Core.limit_price" if limit_yuan is not None else None
        tick_ts = _dt(tick.get("source_ts") or tick.get("ts")) if tick else None
        received = read_time if tick and live else (_dt(tick.get("received_at")) if tick else None)
        if official is None:
            reason = "OFFICIAL_LIMIT_UNAVAILABLE"
        elif tick:
            px_tick, amt_tick = _number(tick.get("px_milli")), _number(tick.get("amt_yuan"))
            bp1, bv1 = _number(tick.get("bp1_milli")), _number(tick.get("bv1"))
            if px_tick is None or amt_tick is None or bp1 is None or bv1 is None:
                reason = "INCOMPLETE_FINAL_TICK"
            elif amt_tick <= 0:
                reason = "NO_FINAL_TRADE"
            elif round(bp1 / 1000, 2) != round(limit_yuan, 2):
                status, reason = "NOT_LIMIT", "BID1_NOT_AT_LIMIT"
            elif bv1 <= 0:
                status, reason = "NOT_LIMIT", "NO_BID1_AT_LIMIT"
            elif tick_volume_unit != "lots":
                reason = "BID1_UNIT_UNVERIFIED"
            else:
                status, reason = ("VERIFIED", None) if live else ("VERIFIED_REPLAY", None)
                seal_amount = round(limit_yuan * bv1 * 100)
        facts.append({"symbol": symbol, "name": snap.get("name"),
                      "auction_amount_yuan": amount, "amount_rank": None,
                      "amount_top50": False,
                      "pct_change": change / 100 if change is not None else None,
                      "valid_price": px is not None and px > 0,
                      "seal": {"status": status, "reason": reason,
                               "seal_amount_yuan": seal_amount, "seal_rank": None,
                               "basis": ("LIVE_READ" if live else "EVENT_TIME_ONLY") if seal_amount is not None else None,
                               "limit_price_source": limit_source,
                               "tick_source_ts": tick_ts.isoformat() if tick_ts else None,
                               "read_at": read_time.isoformat() if tick and live else None,
                               "received_at": received.isoformat() if received else None},
                      "coverage": {"amount": amount is not None, "price": px is not None and px > 0,
                                   "final_tick": tick is not None, "seal": status in ("VERIFIED", "VERIFIED_REPLAY")},
                      "limit_up_candidate": _number(snap.get("limit_state")) == 1})
    positive = sorted((row["auction_amount_yuan"] for row in facts if row["auction_amount_yuan"] is not None), reverse=True)
    amount_n = len(positive)
    greater: dict[float, int] = {}
    for amount in set(positive):
        greater[amount] = sum(value > amount for value in positive)
    for row in facts:
        amount = row["auction_amount_yuan"]
        if amount is not None:
            row["amount_rank"] = 1 + greater[amount]
            row["amount_top50"] = row["amount_rank"] <= 50
    seals = sorted((row["seal"]["seal_amount_yuan"] for row in facts
                    if row["seal"]["status"] in ("VERIFIED", "VERIFIED_REPLAY")), reverse=True)
    seal_greater = {amount: sum(value > amount for value in seals) for amount in set(seals)}
    for row in facts:
        seal = row["seal"]
        if seal["status"] in ("VERIFIED", "VERIFIED_REPLAY"):
            seal["seal_rank"] = 1 + seal_greater[seal["seal_amount_yuan"]]
    live_n = sum(row["seal"]["status"] == "VERIFIED" for row in facts)
    replay_n = sum(row["seal"]["status"] == "VERIFIED_REPLAY" for row in facts)
    limit_candidates = sum(row["limit_up_candidate"] for row in facts)
    return {"trade_date": trade_date, "asof": cutoff.isoformat(), "rank_method": "competition",
            "seal_label": "已核实" if live else "回放·按事件时间",
            "seal_scope_label": f"已核实子集 {live_n if live else replay_n}/{limit_candidates}",
            "rows": facts, "summary": {"valid_amount_n": amount_n, "universe_n": len(facts),
                                       "verified_seal_n": len(seals), "verified_live_n": live_n,
                                       "verified_replay_n": replay_n, "limit_candidate_n": limit_candidates,
                                       "verified_seal_sum": sum(seals) if seals else None,
                                       "seal_scope": "VERIFIED_SUBSET",
                                       "seal_label": "已核实" if live else "回放·按事件时间"}}


def store_auction_stock_facts(facts: Mapping[str, Any], *, artifact_root: str | Path | None = None,
                              redis: Any = None, ttl: int = 3600) -> dict[str, Any]:
    """Write immutable cold version and a bounded Core-only hot projection."""
    root = Path(artifact_root or os.getenv("CORE_DATA_ROOT", str(Path.home() / "core-data")))
    dataset, day = "auction_stock_facts", str(facts["trade_date"])
    catalog = ArtifactCatalog(root)
    summary = dict(facts["summary"])
    rows = list(facts["rows"])
    available_at = datetime.now(TZ).isoformat()
    manifest = catalog.commit(dataset, day, rows,
                              {"schema": "auction_stock_facts_v1", "trade_date": day,
                               "as_of": facts["asof"], "available_at": available_at,
                               "rank_method": facts["rank_method"],
                               "summary": summary, "business_status": "PARTIAL",
                               "quality": {"universe_n": summary["universe_n"],
                                           "valid_amount_n": summary["valid_amount_n"],
                                           "verified_seal_n": summary["verified_seal_n"]}},
                              source="auction_stock_facts")
    if redis is not None:
        view = {"version": manifest["version"], "trade_date": day, "used_date": day,
                "asof": facts["asof"], "available_at": available_at, "summary": summary,
                "top50": [row for row in rows if row["amount_top50"]],
                "verified_seals": [row for row in rows if row["seal"]["status"] == "VERIFIED"]}
        redis.set(f"core:cache:data:auction_stock_facts:{day}",
                  json.dumps(view, ensure_ascii=False, separators=(",", ":")), ex=ttl)
    return manifest
