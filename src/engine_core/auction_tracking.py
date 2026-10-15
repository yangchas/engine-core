"""Offline, event-time replay of call-auction and opening tick facts.

Thresholds below are provisional review prompts, not trade signals.
"""
from __future__ import annotations

import csv
import hashlib
import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping


TRADE_DATE_REQUIRED = True
PLATE_SHARE_JUMP_BP = 100  # 待校准
PLATE_MIN_AMOUNT_YUAN = 20_000_000  # 待校准
STOCK_AMOUNT_MULTIPLE = 3.0  # 待校准
STOCK_MIN_AMOUNT_YUAN = 20_000_000  # 待校准
STOCK_PRICE_MOVE_PP = 0.5  # 待校准
RELATIVE_MOVE_PP = 1.0  # 待校准
INDEX_AMOUNT_MULTIPLE = 1.8  # 待校准
LATE_CHANGE_PP = 1.0  # 待校准
LATE_SHARE_CHANGE_BP = 100  # 待校准
FOCUS_COUNT = 30
TOP_STOCKS_PER_SLICE = 200


@dataclass(frozen=True)
class Tick:
    trade_date: str
    ts: str
    symbol: str
    px_milli: int = 0
    pc_milli: int = 0
    bp1_milli: int = 0
    ap1_milli: int = 0
    bv1: int = 0
    av1: int = 0
    bp2_milli: int = 0
    ap2_milli: int = 0
    bv2: int = 0
    av2: int = 0
    amt_yuan: int = 0
    vol_units: int = 0


def _int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    try:
        return int(float(value)) if value not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _seconds(ts: str) -> int:
    return sum(int(part) * scale for part, scale in zip(ts.split(":")[:3], (3600, 60, 1)))


def _stamp(trade_date: str, seconds: int) -> str:
    return f"{trade_date}T{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def matching_amount_yuan(tick: Tick) -> int:
    """Auction virtual match amount; no transaction amount is inferred."""
    if tick.bp1_milli <= 0 or tick.bp1_milli != tick.ap1_milli:
        return 0
    return min(max(0, tick.bv1), max(0, tick.av1)) * tick.bp1_milli * 100 // 1000


def derive_state(tick: Tick) -> dict[str, Any]:
    sec = _seconds(tick.ts)
    auction = sec <= _seconds("09:25:00")
    price = tick.bp1_milli if auction and tick.bp1_milli > 0 and tick.bp1_milli == tick.ap1_milli else (
        tick.px_milli if not auction else 0
    )
    amount = matching_amount_yuan(tick) if auction else max(0, tick.amt_yuan)
    pct = ((price / tick.pc_milli - 1.0) * 100.0) if price > 0 and tick.pc_milli > 0 else None
    return {
        "symbol": tick.symbol, "price_milli": price, "prev_close_milli": tick.pc_milli,
        "pct_change": pct, "match_amount_yuan": amount,
        "rest_bid_shares": max(0, tick.bv2), "rest_ask_shares": max(0, tick.av2),
        "rest_bid_yuan": max(0, tick.bv2) * max(0, tick.bp1_milli) // 10,
        "rest_ask_yuan": max(0, tick.av2) * max(0, tick.ap1_milli) // 10,
        "status": "INDICATIVE" if auction and sec < _seconds("09:25:00") else "FINAL",
        "ts": tick.ts, "amt_yuan": max(0, tick.amt_yuan),
    }


def load_ticks(path: Path, trade_date: str) -> list[Tick]:
    rows: list[tuple[int, int, Tick]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for ordinal, row in enumerate(csv.DictReader(handle)):
            if str(row.get("trade_date", "")).strip() != trade_date:
                continue
            ts = str(row.get("ts", ""))[-8:]
            try:
                _seconds(ts)
            except (ValueError, TypeError):
                continue
            tick = Tick(trade_date=trade_date, ts=ts, symbol=str(row.get("symbol", "")).strip().zfill(6),
                        **{k: _int(row, k) for k in Tick.__dataclass_fields__ if k not in {"trade_date", "ts", "symbol"}})
            rows.append((_seconds(ts), ordinal, tick))
    return [row[2] for row in sorted(rows, key=lambda item: (item[0], item[1])) if row[2].symbol]


def _limit_pct(symbol: str) -> float:
    if symbol.startswith(("300", "301", "688", "689")):
        return 20.0
    if symbol.startswith(("4", "8", "92")):
        return 30.0
    return 10.0


def _at_limit(state: Mapping[str, Any], direction: str) -> bool:
    symbol, price = str(state["symbol"]), int(state["price_milli"] or 0)
    pc = int(state["prev_close_milli"] or 0)
    if not price or not pc or symbol.startswith(("N", "C")):
        return False
    ratio = _limit_pct(symbol) / 100
    target = int((pc * (1 + ratio if direction == "up" else 1 - ratio) + 5) // 10 * 10)
    rest = state["rest_bid_shares"] if direction == "up" else state["rest_ask_shares"]
    return abs(price - target) <= 10 and rest > 0


def _pct_values(states: Mapping[str, Mapping[str, Any]]) -> list[float]:
    return [float(s["pct_change"]) for s in states.values() if s.get("pct_change") is not None]


def _load_json(path: Path | None, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path else default


def _load_relations(relation_dir: Path | None) -> tuple[dict[str, set[str]], dict[str, str], set[str]]:
    if not relation_dir:
        return {}, {}, set()
    raw = json.loads((relation_dir / "registry.json").read_text(encoding="utf-8"))
    plates_raw = raw.get("plates", []) if isinstance(raw, dict) else raw
    names = {str(row.get("code")): str(row.get("name") or row.get("plate") or row.get("code"))
             for row in plates_raw if isinstance(row, dict) and row.get("code") is not None}
    generic_path = Path(__file__).resolve().parents[2] / "examples/data/plate_generic_factor_names.json"
    generic = set()
    if generic_path.exists():
        contents = json.loads(generic_path.read_text(encoding="utf-8"))
        generic = set(contents if isinstance(contents, list) else contents.get("names", []))
    members: dict[str, set[str]] = {}
    with (relation_dir / "relation.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"plateid", "stockid"}.issubset(reader.fieldnames or ()):
            raise ValueError("relation.csv must have plateid and stockid columns")
        for row in reader:
            code, symbol = str(row.get("plateid", "")).strip(), str(row.get("stockid", "")).strip()
            if code and symbol:
                members.setdefault(code, set()).add(symbol.zfill(6))
    return members, names, generic


def _pool_symbols(pool: Any) -> set[str]:
    if isinstance(pool, dict) and isinstance(pool.get("value"), dict):
        rows = pool["value"].values()
    elif isinstance(pool, dict):
        rows = pool.values()
    else:
        rows = pool if isinstance(pool, list) else []
    result = set()
    for row in rows:
        if isinstance(row, str):
            try: row = json.loads(row)
            except ValueError: continue
        if isinstance(row, dict) and row.get("symbol"):
            result.add(str(row["symbol"]).zfill(6))
    return result


def _snapshot(states: Mapping[str, Mapping[str, Any]], ts: str, prior: Mapping[str, Any] | None,
              focus: set[str], relations: Mapping[str, set[str]], names: Mapping[str, str],
              generic: set[str], phase: str, final_amount: int | None = None) -> dict[str, Any]:
    market = sum(int(s["match_amount_yuan"]) for s in states.values())
    pct = _pct_values(states)
    sorted_stocks = sorted(states.values(), key=lambda s: (-int(s["match_amount_yuan"]), s["symbol"]))
    strongest_by_symbol: dict[str, tuple[float, str]] = {}
    for code, members in relations.items():
        name = names.get(code, code)
        selected = [states[s] for s in members if s in states and states[s].get("pct_change") is not None]
        if name in generic or len(selected) < 5:
            continue
        plate_pct = statistics.mean(x["pct_change"] for x in selected)
        for symbol in members:
            if symbol not in strongest_by_symbol or plate_pct > strongest_by_symbol[symbol][0]:
                strongest_by_symbol[symbol] = (plate_pct, name)
    stocks = []
    for src in sorted_stocks[:TOP_STOCKS_PER_SLICE]:
        row = dict(src)
        row["market_share_bp"] = round(10000 * row["match_amount_yuan"] / market, 2) if market else None
        strongest = strongest_by_symbol.get(row["symbol"])
        row["strongest_plate"] = strongest[1] if strongest else None
        row["relative_to_strongest_plate_pp"] = row["pct_change"] - strongest[0] if strongest and row.get("pct_change") is not None else None
        stocks.append(row)
    # Keep focus symbols even when outside the amount top 200.
    present = {x["symbol"] for x in stocks}
    for symbol in sorted(focus - present):
        if symbol in states:
            row = dict(states[symbol]); row["market_share_bp"] = round(10000 * row["match_amount_yuan"] / market, 2) if market else None
            strongest = strongest_by_symbol.get(symbol)
            row["strongest_plate"] = strongest[1] if strongest else None
            row["relative_to_strongest_plate_pp"] = row["pct_change"] - strongest[0] if strongest and row.get("pct_change") is not None else None
            stocks.append(row)
    plate_rows = []
    for code, members in relations.items():
        name = names.get(code, code)
        selected = [states[s] for s in members if s in states and states[s].get("pct_change") is not None]
        if len(selected) < 5 or name in generic:
            continue
        amount = sum(int(row["match_amount_yuan"]) for row in selected)
        plate_rows.append({"code": code, "name": name, "member_count": len(selected),
                           "match_amount_yuan": amount,
                           "market_share_bp": round(amount * 10000 / market, 2) if market else None,
                           "equal_weight_pct": statistics.mean(row["pct_change"] for row in selected)})
    plate_rows.sort(key=lambda row: (-row["match_amount_yuan"], row["code"]))
    return {"timestamp": ts, "phase": phase, "market_match_amount_yuan": market,
            "market_change_yuan": market - int(prior["market_match_amount_yuan"]) if prior else None,
            "share_of_0925_terminal_bp": round(market * 10000 / final_amount, 2) if final_amount else None,
            "median_pct_change": statistics.median(pct) if pct else None,
            "up_share_pct": 100 * sum(x > 0 for x in pct) / len(pct) if pct else None,
            "limit_up_count": sum(_at_limit(s, "up") for s in states.values()),
            "limit_down_count": sum(_at_limit(s, "down") for s in states.values()),
            "stocks": stocks, "plates": plate_rows}


def replay_tracking(*, trade_date: str, ticks: Iterable[Tick], open_ticks: Iterable[Tick] = (),
                    relations: Mapping[str, set[str]] | None = None, plate_names: Mapping[str, str] | None = None,
                    generic_names: set[str] | None = None, pool_symbols: set[str] | None = None,
                    baseline: Mapping[str, Any] | None = None, prev_close: Mapping[str, Any] | None = None,
                    focus_count: int = FOCUS_COUNT) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    relations, plate_names, generic_names = relations or {}, plate_names or {}, generic_names or set()
    rows = sorted((t for t in ticks if t.trade_date == trade_date), key=lambda t: (_seconds(t.ts), t.symbol))
    opens = sorted((t for t in open_ticks if t.trade_date == trade_date), key=lambda t: (_seconds(t.ts), t.symbol))
    marks_auc = list(range(_seconds("09:15:00"), _seconds("09:25:00") + 1, 30))
    marks_open = list(range(_seconds("09:30:00"), _seconds("09:45:00") + 1, 60)) if opens else []
    state: dict[str, dict[str, Any]] = {}
    timeline: list[dict[str, Any]] = []
    state_history: list[dict[str, dict[str, Any]]] = []
    events: list[dict[str, Any]] = []
    i = 0
    for mark in marks_auc:
        while i < len(rows) and _seconds(rows[i].ts) <= mark:
            state[rows[i].symbol] = derive_state(rows[i]); i += 1
        ts = _stamp(trade_date, mark)
        prior = timeline[-1] if timeline else None
        snap = _snapshot(state, ts, prior, set(), relations, plate_names, generic_names, "AUCTION")
        timeline.append(snap)
        state_history.append({s: dict(v) for s, v in state.items()})
    terminal = timeline[-1]["market_match_amount_yuan"] if timeline else 0
    previous_dates = sorted(str(key) for key in (baseline or {}) if str(key) < trade_date)
    baseline_date = previous_dates[-1] if previous_dates else None
    baseline_amount = int((baseline or {}).get(baseline_date, 0) or 0) if baseline_date else 0
    # Retrospective field is attached after all auction states have been computed.
    for snap in timeline:
        snap["share_of_0925_terminal_bp"] = round(snap["market_match_amount_yuan"] * 10000 / terminal, 2) if terminal else None
        snap["market_amount_vs_previous_day_final"] = round(snap["market_match_amount_yuan"] / baseline_amount, 4) if baseline_amount else None
    _add_comparisons(timeline)
    anchor = {s: dict(v) for s, v in state.items()}
    focus = set(pool_symbols or ())
    if anchor:
        focus.update(s for s, _ in sorted(anchor.items(), key=lambda item: -int(item[1]["match_amount_yuan"]))[:focus_count])
    focus_plates = {p["code"] for p in timeline[-1]["plates"][:focus_count]} if timeline else set()
    # Differential events use observed slice states only; no closing/daily fields enter.
    prev_snapshot = None
    for ix, snap in enumerate(timeline):
        now_states = state_history[ix]
        sixty_states = state_history[ix - 2] if ix >= 2 else {}
        immediate_states = state_history[ix - 1] if ix else {}
        _emit_events(events, snap, prev_snapshot, sixty_states, now_states, relations, plate_names,
                     baseline, trade_date, immediate_states=immediate_states, allow_volume_events=ix >= 2,
                     sixty_snapshot=timeline[ix - 2] if ix >= 2 else None, generic_names=generic_names)
        prev_snapshot = snap
    if len(timeline) > 20:
        _emit_late_summary(events, timeline[18], timeline[20], state_history[18], state_history[20])
    if opens:
        oi = 0
        open_state: dict[str, dict[str, Any]] = {s: dict(v) for s, v in anchor.items()}
        prior_open = _snapshot(anchor, f"{trade_date}T09:25:00", None, focus, relations,
                               plate_names, generic_names, "AUCTION")
        opened_positions: dict[str, int] = {}
        for mark in marks_open:
            previous_amounts = {symbol: int(row.get("amt_yuan", 0)) for symbol, row in open_state.items()}
            while oi < len(opens) and _seconds(opens[oi].ts) <= mark:
                tick = opens[oi]
                new = derive_state(tick)
                new["auction_price_milli"] = anchor.get(tick.symbol, {}).get("price_milli")
                open_state[tick.symbol] = new; oi += 1
            for symbol, row in open_state.items():
                row["opening_amount_delta_yuan"] = max(0, int(row.get("amt_yuan", 0)) - previous_amounts.get(symbol, 0))
            ts = _stamp(trade_date, mark)
            snap = _snapshot(open_state, ts, prior_open, focus, relations, plate_names, generic_names, "OPEN")
            for stock in snap["stocks"]:
                auction = anchor.get(stock["symbol"])
                if auction:
                    stock["auction_price_milli"] = auction.get("price_milli")
                    stock["auction_pct_change"] = auction.get("pct_change")
                    stock["below_auction_price"] = bool(stock.get("price_milli") and auction.get("price_milli") and stock["price_milli"] < auction["price_milli"])
            timeline.append(snap)
            for symbol, row in open_state.items():
                pct, auc = row.get("pct_change"), anchor.get(symbol, {})
                if pct is not None and auc.get("pct_change") is not None and auc["pct_change"] < 3 and pct >= 5 and symbol not in opened_positions:
                    opened_positions[symbol] = mark
                    events.append(_event(ts, "stock", symbol, "OPENING_NEW_LEADER", {"pct_change": pct}, {"auction_pct": auc["pct_change"]}, row.get("status")))
            _emit_open_events(events, snap, prior_open, open_state, anchor, relations, plate_names)
            prior_open = snap
    report = {"trade_date": trade_date, "timeline": timeline, "focus_symbols": sorted(focus),
              "focus_plate_codes": sorted(focus_plates), "terminal_market_match_amount_yuan": terminal,
              "previous_day_baseline": {"date": baseline_date, "terminal_amount_yuan": baseline_amount,
                                        "current_terminal_multiple": round(terminal / baseline_amount, 4) if baseline_amount else None},
              "price_status_coverage": {"symbols": len(anchor),
                  "final": sum(row.get("status") == "FINAL" for row in anchor.values()),
                  "indicative": sum(row.get("status") == "INDICATIVE" for row in anchor.values()),
                  "unknown_price": sum(row.get("price_milli", 0) <= 0 for row in anchor.values())}}
    return report, events


def _event(ts: str, object_type: str, obj: str, kind: str, values: Mapping[str, Any], background: Mapping[str, Any], status: str | None = None) -> dict[str, Any]:
    return {"timestamp": ts, "object_type": object_type, "object": obj, "type": kind,
            "values": dict(values), "background": dict(background), "price_status": status,
            "wording": "观察到的数值变化；不推断交易意图"}


def _add_comparisons(timeline: list[dict[str, Any]]) -> None:
    if not timeline:
        return
    by_time = {row["timestamp"][-8:]: row for row in timeline}
    ref20, ref24 = by_time.get("09:20:00"), by_time.get("09:24:00")
    previous = None
    for snap in timeline:
        comparisons = {"previous_slice": previous, "0920": ref20, "0924": ref24}
        for plate in snap["plates"]:
            for label, ref in comparisons.items():
                item = next((p for p in ref["plates"] if p["code"] == plate["code"]), None) if ref else None
                plate[f"share_change_bp_vs_{label}"] = round(plate["market_share_bp"] - item["market_share_bp"], 2) if item and plate["market_share_bp"] is not None and item["market_share_bp"] is not None else None
                plate[f"amount_change_yuan_vs_{label}"] = plate["match_amount_yuan"] - item["match_amount_yuan"] if item else None
                plate[f"equal_weight_change_pp_vs_{label}"] = round(plate["equal_weight_pct"] - item["equal_weight_pct"], 4) if item else None
        for stock in snap["stocks"]:
            stock["market_share_bp"] = stock.get("market_share_bp")
        previous = snap


def _emit_late_summary(out: list[dict[str, Any]], before: Mapping[str, Any], after: Mapping[str, Any],
                       before_states: Mapping[str, Mapping[str, Any]], after_states: Mapping[str, Mapping[str, Any]]) -> None:
    ts = str(after["timestamp"])
    old_plates = {p["code"]: p for p in before["plates"]}
    for plate in after["plates"]:
        old = old_plates.get(plate["code"])
        if old and abs(plate["market_share_bp"] - old["market_share_bp"]) >= LATE_SHARE_CHANGE_BP:
            out.append(_event(ts, "plate", plate["name"], "AFTER_0924_SHARE_CHANGE",
                              {"share_change_bp": plate["market_share_bp"] - old["market_share_bp"]},
                              {"from": "09:24:00", "to": "09:25:00"}))
    for symbol, now in after_states.items():
        old = before_states.get(symbol)
        if old and now.get("pct_change") is not None and old.get("pct_change") is not None:
            change = now["pct_change"] - old["pct_change"]
            if abs(change) >= LATE_CHANGE_PP:
                out.append(_event(ts, "stock", symbol, "AFTER_0924_PRICE_CHANGE", {"change_pp": change},
                                  {"from": "09:24:00", "to": "09:25:00"}, now.get("status")))


def _emit_events(out: list[dict[str, Any]], snap: Mapping[str, Any], prev: Mapping[str, Any] | None,
                 old_states: Mapping[str, Mapping[str, Any]], states: Mapping[str, Mapping[str, Any]],
                 relations: Mapping[str, set[str]], names: Mapping[str, str], baseline: Mapping[str, Any] | None, date: str,
                 *, immediate_states: Mapping[str, Mapping[str, Any]] | None = None,
                 allow_volume_events: bool = True, sixty_snapshot: Mapping[str, Any] | None = None,
                 generic_names: set[str] | None = None) -> None:
    if not prev: return
    ts = str(snap["timestamp"])
    old_plates = {p["code"]: p for p in (sixty_snapshot or prev)["plates"]}; new_plates = {p["code"]: p for p in snap["plates"]}
    for code, now in new_plates.items():
        before = old_plates.get(code)
        if allow_volume_events and before and now["market_share_bp"] - before["market_share_bp"] >= PLATE_SHARE_JUMP_BP and now["match_amount_yuan"] >= PLATE_MIN_AMOUNT_YUAN:
            out.append(_event(ts, "plate", names.get(code, code), "PLATE_SHARE_SURGE", {"share_change_bp": now["market_share_bp"] - before["market_share_bp"], "amount_yuan": now["match_amount_yuan"]}, {"market_amount_yuan": snap["market_match_amount_yuan"], "window_seconds": 60}))
    old_plate_map = _plate_pct_map(old_states, relations, names, generic_names or set())
    new_plate_map = _plate_pct_map(states, relations, names, generic_names or set())
    plates_of = _stock_plates(relations)
    for symbol, now in states.items():
        before = old_states.get(symbol)
        if not before: continue
        d_amount = max(0, int(now["match_amount_yuan"]) - int(before["match_amount_yuan"]))
        prior_amt = int(before["match_amount_yuan"])
        price_delta = (now["pct_change"] - before["pct_change"]) if now.get("pct_change") is not None and before.get("pct_change") is not None else 0
        if allow_volume_events and d_amount >= STOCK_MIN_AMOUNT_YUAN and (d_amount / max(1, prior_amt)) >= STOCK_AMOUNT_MULTIPLE:
            direction = "PRICE_UP" if price_delta >= STOCK_PRICE_MOVE_PP else "PRICE_DOWN" if price_delta <= -STOCK_PRICE_MOVE_PP else "PRICE_FLAT"
            out.append(_event(ts, "stock", symbol, f"STOCK_AMOUNT_SURGE_{direction}", {"amount_increase_yuan": d_amount, "multiple": d_amount / max(1, prior_amt), "price_change_pp": price_delta}, {"market_amount_yuan": snap["market_match_amount_yuan"], "window_seconds": 60}, now.get("status")))
        _limit_transition(out, ts, symbol, (immediate_states or {}).get(symbol, before), now)
        if before.get("pct_change") is not None and now.get("pct_change") is not None:
            old_plate_pct = _strongest_from_map(symbol, old_plate_map, plates_of)
            new_plate_pct = _strongest_from_map(symbol, new_plate_map, plates_of)
            if old_plate_pct is not None and new_plate_pct is not None:
                relative_move = (now["pct_change"] - new_plate_pct) - (before["pct_change"] - old_plate_pct)
                if abs(relative_move) >= RELATIVE_MOVE_PP:
                    kind = "STOCK_RELATIVE_WEAKENING" if relative_move < 0 else "STOCK_RELATIVE_STRENGTHENING"
                    out.append(_event(ts, "stock", symbol, kind,
                                      {"relative_change_pp": relative_move},
                                      {"strongest_plate_pct": new_plate_pct}, now.get("status")))
    if snap["market_match_amount_yuan"] >= int(prev["market_match_amount_yuan"] * INDEX_AMOUNT_MULTIPLE) > 0:
        out.append(_event(ts, "market", "ALL", "MARKET_AMOUNT_JUMP", {"multiple": snap["market_match_amount_yuan"] / prev["market_match_amount_yuan"]}, {}))


_STOCK_PLATES_CACHE: dict[int, dict[str, list[str]]] = {}


def _stock_plates(relations: Mapping[str, set[str]]) -> dict[str, list[str]]:
    key = id(relations)
    cached = _STOCK_PLATES_CACHE.get(key)
    if cached is None or cached.get("__n__") != len(relations):
        cached = {"__n__": len(relations)}  # type: ignore[dict-item]
        for code, members in relations.items():
            for symbol in members:
                cached.setdefault(symbol, []).append(code)
        _STOCK_PLATES_CACHE[key] = cached
    return cached


def _plate_pct_map(states: Mapping[str, Mapping[str, Any]], relations: Mapping[str, set[str]],
                   names: Mapping[str, str], generic_names: set[str]) -> dict[str, float]:
    """plate code -> equal-weight mean pct over members with a price (>=5 priced members), computed once per slice."""
    result: dict[str, float] = {}
    for code, members in relations.items():
        if names.get(code, code) in generic_names:
            continue
        selected = [states[s]["pct_change"] for s in members if s in states and states[s].get("pct_change") is not None]
        if len(selected) >= 5:
            result[code] = statistics.mean(selected)
    return result


def _strongest_from_map(symbol: str, plate_map: Mapping[str, float], plates_of: Mapping[str, list[str]]) -> float | None:
    values = [plate_map[code] for code in plates_of.get(symbol, ()) if code in plate_map]
    return max(values) if values else None


def _strongest_plate_pct(symbol: str, states: Mapping[str, Mapping[str, Any]], relations: Mapping[str, set[str]],
                         names: Mapping[str, str], generic_names: set[str]) -> float | None:
    candidates = []
    for code, members in relations.items():
        if names.get(code, code) in generic_names:
            continue
        selected = [states[s]["pct_change"] for s in members if s in states and states[s].get("pct_change") is not None]
        if symbol in members and len(selected) >= 5:
            candidates.append(statistics.mean(selected))
    return max(candidates) if candidates else None


def _limit_transition(out: list[dict[str, Any]], ts: str, symbol: str, before: Mapping[str, Any], now: Mapping[str, Any]) -> None:
    for direction, label in (("up", "LIMIT_UP"), ("down", "LIMIT_DOWN")):
        was, is_now = _at_limit(before, direction), _at_limit(now, direction)
        if was != is_now:
            side = "bid" if direction == "up" else "ask"
            out.append(_event(ts, "stock", symbol, f"{label}_{'APPEARED' if is_now else 'DISAPPEARED'}",
                              {"rest_shares": now[f"rest_{side}_shares"], "rest_change_shares": now[f"rest_{side}_shares"] - before[f"rest_{side}_shares"]}, {}, now.get("status")))


def _emit_open_events(out: list[dict[str, Any]], snap: Mapping[str, Any], prev: Mapping[str, Any], states: Mapping[str, Mapping[str, Any]], anchor: Mapping[str, Mapping[str, Any]], relations: Mapping[str, set[str]], names: Mapping[str, str]) -> None:
    # Opening focus state changes are factual; cumulative amount deltas come from tick amt_yuan.
    prev_stocks = {x["symbol"]: x for x in prev["stocks"]}
    now_stocks = {x["symbol"]: x for x in snap["stocks"]}
    for symbol, row in states.items():
        if symbol not in anchor:
            continue
        old = prev_stocks.get(symbol)
        shown = now_stocks.get(symbol)
        if old and shown:
            delta = max(0, int(row.get("amt_yuan", 0)) - int(old.get("amt_yuan", 0)))
            if delta > 0:
                out.append(_event(str(snap["timestamp"]), "stock", symbol, "OPENING_TICK_ACTIVITY",
                                  {"amount_delta_yuan": delta, "pct_change": row.get("pct_change")},
                                  {"auction_pct": anchor[symbol].get("pct_change")}, row.get("status")))
            auc_price = int(anchor[symbol].get("price_milli") or 0)
            was_below = bool(old.get("price_milli") and auc_price and old["price_milli"] < auc_price)
            is_below = bool(shown.get("price_milli") and auc_price and shown["price_milli"] < auc_price)
            if was_below != is_below:
                out.append(_event(str(snap["timestamp"]), "stock", symbol,
                                  "OPENING_BELOW_AUCTION" if is_below else "OPENING_RECLAIMED_AUCTION",
                                  {"auction_price_milli": auc_price, "price_milli": shown.get("price_milli")}, {}, row.get("status")))
            _limit_transition(out, str(snap["timestamp"]), symbol, old, shown)


def sha256_file(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path else None


def write_outputs(out_dir: Path, report: Mapping[str, Any], events: list[dict[str, Any]], manifest: Mapping[str, Any]) -> None:
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError("out-dir must be empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "timeline.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps(dict(manifest, slice_count=len(report["timeline"]), event_count=len(events)), ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(out_dir / "tracking_report.md", report, events)


def _write_markdown(path: Path, report: Mapping[str, Any], events: list[dict[str, Any]]) -> None:
    timeline = report["timeline"]
    auction = [s for s in timeline if s["phase"] == "AUCTION"]
    points = " → ".join(f"{s['timestamp'][-8:-3]} {s['market_match_amount_yuan']/1e8:.1f}亿" for s in auction)
    baseline = report.get("previous_day_baseline", {})
    amount = baseline.get("terminal_amount_yuan") or 0
    previous = f"{baseline.get('date')} 09:25={amount/1e8:.1f}亿" if amount else "未提供前日基线"
    curve = ("当前切片(前日09:25终值=20格)：" + " ".join("█" * max(1, round(s["market_match_amount_yuan"] * 20 / amount)) for s in auction)) if amount else "前日对比曲线：不可用"
    path.write_text("\n".join([f"# 竞价与开盘跟踪 {report['trade_date']}", "", f"大盘竞价额：{points}", f"前日对比：{previous}；当前/前日={baseline.get('current_terminal_multiple') or '未知'}", curve, "", "## 关键事件", *([f"- {e['timestamp'][-8:]} {e['object']} {e['type']} {e['values']}" for e in events[:30]] or ["- 无达到当前阈值的事件"]), "", "## 板块跟踪", "timeline.json 保留各切片板块金额、份额、等权涨幅及相对 09:20/09:24 变化。", "", "## 个股跟踪", "timeline.json 每片保留成交额 Top200 及焦点个股、相对所属最强板块涨幅。", "", "## 开盘跟踪", "存在 --open-ticks-csv 时包含 09:30–09:45 焦点股切片、开板/回封、跌破 09:25 价和成交额变化。", "", "## 说明与局限", "竞价为虚拟撮合；09:20–09:25 不可撤单。事件是聚合观察，不推断交易意图；阈值待校准，不构成买卖建议。事后终值占比仅供复盘，不进入实时事件。" ]), encoding="utf-8")


__all__ = ["Tick", "derive_state", "matching_amount_yuan", "load_ticks", "replay_tracking", "write_outputs", "sha256_file", "_load_relations", "_pool_symbols"]
