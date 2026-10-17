"""Point-in-time descriptive high-board to same-plate peer lists."""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from typing import Any, Mapping, Sequence

from .plate_kind import classify_plate_kind
from .limit_price import state_at_close


@lru_cache(maxsize=4096)
def _plate_kind(name: str) -> str:
    return classify_plate_kind(name)


def _number(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    return None


def _plate_size(meta: Mapping[str, Any] | None) -> int | None:
    if not meta:
        return None
    for key in ("size", "members", "member_count"):
        try:
            if meta.get(key) is not None:
                return int(meta[key])
        except (TypeError, ValueError):
            continue
    return None


def _prior_streak(row: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """Prefer the dated yesterday-pool value over defaulted streak fields."""
    pool_value = _number(row, "lb_days")
    values = [_number(row, key) for key in ("prior_streak", "prior_limit_streak", "streak")]
    other_values = [value for value in values if value is not None]
    other = other_values[0] if other_values else None
    chosen = pool_value if pool_value is not None else other
    conflict = ("字段冲突" if pool_value is not None and
                any(value != pool_value for value in other_values) else None)
    if chosen == 0:
        chosen = None
    return (int(chosen) if chosen is not None else None), conflict


def select_main_plate(candidates: Sequence[str], metadata: Mapping[str, Mapping[str, Any]] | None = None,
                      *, source_candidates: Mapping[str, str] | None = None,
                      min_size: int = 8, max_size: int = 150) -> str | None:
    """Choose a bounded, named theme plate with explicit evidence priority."""
    metadata = metadata or {}
    source_candidates = source_candidates or {}
    priority = {"POOL": 0, "REASON": 1, "STATIC": 2, "HOT": 3}
    eligible = []
    for raw in candidates:
        name = str(raw or "").strip()
        if not name:
            continue
        meta = metadata.get(name, {})
        kind = str(meta.get("class", meta.get("kind", _plate_kind(name)))).upper()
        size = _plate_size(meta)
        if kind not in {"THEME", "NICHE"}:
            continue
        # Missing size is tolerated only for legacy callers without registry metadata.
        if metadata and (size is None or not min_size <= size <= max_size):
            continue
        source = source_candidates.get(name, "STATIC").upper()
        eligible.append((priority.get(source, 2), size if size is not None else 10**9, name))
    return min(eligible)[2] if eligible else None


def _plates(row: Mapping[str, Any], relations: Mapping[str, Sequence[str]] | None,
            metadata: Mapping[str, Mapping[str, Any]] | None = None,
            selection_cache: dict[tuple[Any, ...], str | None] | None = None,
            *, min_size: int = 8, max_size: int = 150) -> list[str]:
    ordered = [row.get("pool_plate"), row.get("reason_plate"), row.get("static_plate"),
               row.get("main_plate"), row.get("hot_plate")]
    plates = row.get("plates")
    if isinstance(plates, list):
        ordered.extend(plates)
    if relations:
        ordered.extend(relations.get(str(row.get("symbol", "")).zfill(6), []))
    seen = set()
    source_candidates: dict[str, str] = {}
    for source, name in zip(("POOL", "REASON", "STATIC", "STATIC", "HOT"), ordered[:5]):
        if name:
            source_candidates.setdefault(str(name), source)
    eligible = []
    for name in ordered:
        if not name or str(name) in seen:
            continue
        seen.add(str(name))
        eligible.append(str(name))
    if selection_cache is None:
        chosen = select_main_plate(eligible, metadata, source_candidates=source_candidates,
                                   min_size=min_size, max_size=max_size)
    else:
        cache_key = (tuple(sorted(eligible)), tuple(sorted(source_candidates.items())))
        chosen = selection_cache.get(cache_key, ...)
        if chosen is ...:
            chosen = select_main_plate(eligible, metadata, source_candidates=source_candidates,
                                       min_size=min_size, max_size=max_size)
            selection_cache[cache_key] = chosen
    return [chosen] if chosen else []


def _qualified_plates(row: Mapping[str, Any], relations: Mapping[str, Sequence[str]] | None,
                      metadata: Mapping[str, Mapping[str, Any]] | None,
                      eligibility_cache: dict[str, bool] | None = None,
                      *, min_size: int, max_size: int) -> set[str]:
    candidates = [str(value) for value in (
        row.get("pool_plate"), row.get("reason_plate"), row.get("static_plate"),
        row.get("main_plate"), row.get("hot_plate"))
                  if value]
    candidates.extend(str(value) for value in (row.get("plates") or []) if value)
    if relations:
        candidates.extend(str(value) for value in relations.get(str(row.get("symbol", "")).zfill(6), []))
    if eligibility_cache is None:
        return {name for name in candidates if select_main_plate(
            [name], metadata, min_size=min_size, max_size=max_size) is not None}
    qualified = set()
    for name in candidates:
        eligible = eligibility_cache.get(name)
        if eligible is None:
            eligible = select_main_plate([name], metadata, min_size=min_size,
                                         max_size=max_size) is not None
            eligibility_cache[name] = eligible
        if eligible:
            qualified.add(name)
    return qualified


def _has_membership(row: Mapping[str, Any], relations: Mapping[str, Sequence[str]] | None) -> bool:
    candidates = [row.get(key) for key in ("pool_plate", "reason_plate", "static_plate", "main_plate", "hot_plate")]
    candidates.extend(row.get("plates") or [])
    if relations:
        candidates.extend(relations.get(str(row.get("symbol", "")).zfill(6), []))
    return any(str(value or "").strip() for value in candidates)


def build_a_to_b(stocks: Sequence[Mapping[str, Any]], *, trade_date: str,
                 stage: str = "close", asof: str | None = None,
                 relation_asof: str | None = None,
                 relations: Mapping[str, Sequence[str]] | None = None,
                 plate_metadata: Mapping[str, Mapping[str, Any]] | None = None,
                 min_plate_size: int = 8, max_plate_size: int = 150,
                 max_candidates: int = 3,
                 min_b_auction_amount_yuan: float = 10_000_000,
                 market_state: Mapping[str, Any] | None = None,
                 path_frequencies: Mapping[str, Mapping[str, Any]] | None = None,
                 relation_lag_trading_days: int | None = None) -> list[dict[str, Any]]:
    """Return factual A→B rows using only auction and stage-available values.

    Intraday stages deliberately omit close fields even when the input contains them.
    """
    stage_key = stage.lower()
    live = stage_key not in {"close", "closing", "eod"}
    auction_only = stage_key in {"auction", "auction_0926", "auction_0925"}
    close_visible = not live and not auction_only
    by_plate: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    normalized = []
    qualified_by_symbol: dict[str, set[str]] = {}
    eligibility_cache: dict[str, bool] = {}
    selection_cache: dict[tuple[Any, ...], str | None] = {}
    for source in stocks:
        row = dict(source)
        row["symbol"] = str(row.get("symbol", row.get("code", ""))).zfill(6)
        normalized.append(row)
        qualified_by_symbol[row["symbol"]] = _qualified_plates(
            row, relations, plate_metadata, eligibility_cache,
            min_size=min_plate_size, max_size=max_plate_size)
        for plate in _plates(row, relations, plate_metadata, selection_cache,
                             min_size=min_plate_size,
                             max_size=max_plate_size):
            by_plate[plate].append(row)
    result = []
    def average(rows, key):
        values = [_number(row, key) for row in rows]
        values = [value for value in values if value is not None]
        return sum(values) / len(values) if values else None
    plate_average_0925 = {plate: average(rows, "p0925_pct") for plate, rows in by_plate.items()}
    plate_average_0945 = {plate: average(rows, "p0945_pct") for plate, rows in by_plate.items()}
    plate_average_close = {plate: average(rows, "close_pct") for plate, rows in by_plate.items()}
    visible_key = "p0925_pct" if auction_only else "close_pct" if not live else "p0945_pct"
    plate_current_average = {plate: average(rows, visible_key) for plate, rows in by_plate.items()}
    market_amount = sum(value for row in normalized
                        if (value := _number(row, "auction_amount_yuan")) is not None and value > 0)
    amount_ranked = sorted((row for row in normalized if _number(row, "auction_amount_yuan") is not None),
                           key=lambda r: (-_number(r, "auction_amount_yuan"), r["symbol"]))
    amount_rank = {row["symbol"]: rank for rank, row in enumerate(amount_ranked, 1)}
    plate_amount = {}
    for plate, rows in by_plate.items():
        values = [_number(row, "auction_amount_yuan") for row in rows]
        known = [max(0.0, value) for value in values if value is not None]
        plate_amount[plate] = sum(known) if known else None
    plate_amount_rank = {plate: {row["symbol"]: rank for rank, row in enumerate(sorted(
        (row for row in rows if _number(row, "auction_amount_yuan") is not None),
        key=lambda r: (-_number(r, "auction_amount_yuan"), r["symbol"])), 1)}
        for plate, rows in by_plate.items()}
    plate_up_share = {plate: (sum(1 for row in rows if (_number(row, "p0925_pct") or 0) > 0) /
                              sum(1 for row in rows if _number(row, "p0925_pct") is not None))
                      if any(_number(row, "p0925_pct") is not None for row in rows) else None
                      for plate, rows in by_plate.items()}
    plate_limits = {plate: sum(1 for row in rows if row.get("auction_limit_state") in
                              (1, "1", "UP", "LIMIT_UP")) for plate, rows in by_plate.items()}
    market_amount_ranked = sorted((p for p in by_plate if plate_amount[p] is not None),
                                  key=lambda p: (-plate_amount[p], p))
    plate_strength_ranked = sorted((p for p in by_plate if plate_current_average[p] is not None),
                                   key=lambda p: (-plate_current_average[p], p))
    plate_weak_ranked = sorted((p for p in by_plate if plate_current_average[p] is not None),
                               key=lambda p: (plate_current_average[p], p))
    for anchor in normalized:
        streak, streak_conflict = _prior_streak(anchor)
        # Close-derived flags must not decide which anchors were eligible earlier.
        is_unlimited = bool(anchor.get("is_new_listing") or
                            anchor.get("price_band_label") in {"新股/无涨跌幅限制", "新股/无涨跌停价"} or
                            anchor.get("auction_price_status") in {"UNLIMITED", "无涨跌停价", "无涨跌幅限制"})
        state = anchor.get("auction_limit_state")
        state_up = state in (1, "1", "UP", "LIMIT_UP")
        rest_bid = _number(anchor, "rest_bid_yuan", "rest_bid")
        is_sealed = state_up and rest_bid is not None and rest_bid > 0
        visible_pct = (_number(anchor, "p0925_pct") if auction_only else
                       _number(anchor, "close_pct") if not live else _number(anchor, "p0945_pct"))
        visible_high = _number(anchor, "high_pct") if not live and not auction_only else None
        limit_pct = _number(anchor, "limit_pct")
        exact_close_state = None
        if close_visible and not is_unlimited and all(anchor.get(key) is not None
                                                      for key in ("close", "high", "low", "pre_close")):
            exact_close_state = state_at_close(anchor.get("close"), anchor.get("high"),
                                               anchor.get("low"), anchor.get("pre_close"),
                                               anchor["symbol"],
                                               is_st=bool(anchor.get("is_st")), unlimited=False)
        if is_unlimited:
            today_status = "新"
        elif exact_close_state is not None:
            today_status = exact_close_state
        elif close_visible:
            today_status = "缺原始价格字段"
        elif not close_visible and visible_pct is not None and limit_pct is not None and visible_pct >= limit_pct - .2:
            today_status = "封" if (not live and anchor.get("close_seal_up") is True) or (auction_only and is_sealed) else "触"
        elif not close_visible and visible_high is not None and limit_pct is not None and visible_high >= limit_pct - .2:
            today_status = "触"
        elif streak is not None and visible_pct is not None:
            today_status = "断"
        else:
            today_status = "一字" if is_sealed else "—"
        if is_unlimited or not ((streak is not None and streak >= 2) or is_sealed):
            continue
        anchor_plates = _plates(anchor, relations, plate_metadata, min_size=min_plate_size,
                                max_size=max_plate_size)
        base = {"a_symbol": anchor["symbol"], "a_name": anchor.get("name"),
                "a_prior_streak": int(streak) if streak is not None else None,
                "a_auction_pct": _number(anchor, "p0925_pct", "auction_pct", "0925"),
                "a_close_pct": None if live else _number(anchor, "close_pct"),
                "streak_status": streak_conflict or ("已知" if streak is not None else "—"),
                "a_status": "竞价涨停价且有封单" if is_sealed else "昨日高标",
                "a_today_status": today_status,
                "a_status_is_price_proxy": today_status in {"触", "跌触", "跌停价", "封(价格代理)"} or (today_status == "封" and live),
                "plate": anchor_plates[0] if anchor_plates else None,
                "relation_asof": relation_asof or "UNKNOWN", "asof": asof or trade_date,
                "candidates": [], "reason": None}
        if not anchor_plates:
            base["reason"] = "无合格主题板块" if _has_membership(anchor, relations) else "无板块关系"
            result.append(base)
            continue
        peers = [row for row in by_plate.get(anchor_plates[0], []) if row["symbol"] != anchor["symbol"]]
        if not peers:
            base["reason"] = "无同伴"
            result.append(base)
            continue
        eligible = []
        has_missing_auction = False
        buyable_peers = 0
        excluded_small_amount = 0
        for peer in peers:
            if peer.get("in_yest_pool") is True or peer.get("in_yest_pool") in (1, "1"):
                continue
            p = _number(peer, "p0925_pct", "auction_pct", "0925")
            limit = _number(peer, "limit_pct")
            state = peer.get("auction_limit_state")
            if state in (1, -1, "1", "-1", "UP", "DOWN", "LIMIT_UP", "LIMIT_DOWN"):
                continue
            if p is not None and limit is not None and abs(p) >= limit - 0.2:
                continue
            if peer.get("halted") is True or peer.get("is_halted") is True or peer.get("trading_status") in {"HALTED", "停牌"}:
                continue
            buyable_peers += 1
            amount = _number(peer, "auction_amount_yuan")
            if amount is None or p is None:
                has_missing_auction = True
            else:
                peer_plate_amount = plate_amount.get(anchor_plates[0]) or 0
                share_ok = peer_plate_amount > 0 and amount / peer_plate_amount >= 0.02
                if amount < min_b_auction_amount_yuan and not share_ok:
                    excluded_small_amount += 1
                    continue
                peer_plates = qualified_by_symbol.get(peer["symbol"], set())
                anchor_qualified = qualified_by_symbol.get(anchor["symbol"], set())
                common_count = len(peer_plates.intersection(anchor_qualified))
                eligible.append((amount, common_count, peer))
        eligible.sort(key=lambda pair: (-pair[0], -pair[1], str(pair[2].get("symbol", ""))))
        if not eligible:
            base["reason"] = ("同伴额过小" if excluded_small_amount else
                              "缺少竞价价/额" if has_missing_auction else
                              "同伴全部买不到" if buyable_peers == 0 else "无合格同伴")
        base["excluded_small_amount"] = excluded_small_amount
        for amount, common_count, peer in eligible[:max_candidates]:
            peer_plate_pct = plate_average_0945.get(anchor_plates[0])
            peer_0945 = _number(peer, "p0945_pct", "0945")
            range_position = _number(peer, "prior_range_position20_pct", "p0925_range_position20_pct")
            position_label = ("越界↑" if range_position is not None and range_position > 100 else
                              "越界↓" if range_position is not None and range_position < 0 else
                              "低" if range_position is not None and range_position <= 33 else
                              "中" if range_position is not None and range_position <= 67 else
                              "高" if range_position is not None else "未知")
            item = {"symbol": peer["symbol"], "name": peer.get("name"),
                    "auction_pct": _number(peer, "p0925_pct", "auction_pct", "0925"),
                    "auction_amount_yuan": amount,
                    "common_plate_count": common_count,
                    "auction_amount_bp": _number(peer, "auction_amount_bp"),
                    "prior_range_position20_pct": (range_position if 0 <= range_position <= 100 else position_label)
                        if range_position is not None else None,
                    "position": position_label,
                    "p0925_pct": _number(peer, "p0925_pct", "auction_pct", "0925"),
                    "p0932_pct": None if auction_only else _number(peer, "p0932_pct", "0932"),
                    "p0945_pct": None if auction_only else _number(peer, "p0945_pct", "0945"),
                    "auction_price_status": peer.get("auction_price_status"),
                    "in_yest_pool": peer.get("in_yest_pool"),
                    "prior_streak": _prior_streak(peer)[0],
                    "streak_status": _prior_streak(peer)[1] or
                                    ("已知" if _prior_streak(peer)[0] is not None else "—"),
                    "asof": asof or trade_date, "relation_asof": relation_asof or "UNKNOWN",
                    "relative_to_plate_pp": (None if auction_only else _number(peer, "relative_to_plate_pp") if _number(peer, "relative_to_plate_pp") is not None
                                             else peer_0945 - peer_plate_pct if peer_0945 is not None and peer_plate_pct is not None else None)}
            if not live:
                item["close_pct"] = _number(peer, "close_pct")
            base["candidates"].append(item)
        result.append(base)
    note = "单日描述；14日研究显示A的状态与B表现关系跨零，不构成规则。"
    for row in result:
        row["note"] = note
    plate_counts: dict[str, int] = defaultdict(int)
    for row in result:
        if row.get("plate"):
            plate_counts[str(row["plate"])] += 1
    for row in result:
        plate = row.get("plate")
        row["plate_display"] = (f"同板块 {plate}" if plate and plate_counts[str(plate)] > 1
                                else plate or "无合格主题板块")
    peer_use: dict[str, set[str]] = defaultdict(set)
    for group in result:
        for peer in group.get("candidates", []):
            peer_use[str(peer.get("symbol", ""))].add(str(group.get("a_name") or "名称未知"))
    for group in result:
        for peer in group.get("candidates", []):
            names = sorted(peer_use.get(str(peer.get("symbol", "")), set()))
            peer["shared_anchor_names"] = names if len(names) > 1 else []
    # Add the explainable v2 contract after candidate selection so legacy readers
    # continue to see the original fields and stage filtering remains centralized.
    for group in result:
        symbol = str(group.get("a_symbol", ""))
        anchor = next((row for row in normalized if row["symbol"] == symbol), {})
        plate = group.get("plate")
        peers = by_plate.get(str(plate), []) if plate else []
        streak, _ = _prior_streak(anchor)
        state = anchor.get("auction_limit_state")
        rest_bid = _number(anchor, "rest_bid_yuan", "rest_bid")
        sealed = state in (1, "1", "UP", "LIMIT_UP") and rest_bid is not None and rest_bid > 0
        reasons = []
        if streak is not None and streak >= 2:
            reasons.append({"rule_id": "R1", "text": f"昨日≥2板（{streak}板）"})
        if sealed:
            reasons.append({"rule_id": "R2", "text": "竞价涨停且有买封，当前买不到"})
        highest = max((_prior_streak(row)[0] or 0 for row in peers), default=0)
        if streak is not None and streak > 0 and streak >= highest:
            reasons.append({"rule_id": "R3", "text": "板内最高板" if streak == highest else "空间板"})
        local_rank = plate_amount_rank.get(str(plate), {}).get(symbol)
        if local_rank is not None and local_rank <= 3:
            reasons.append({"rule_id": "R4", "text": f"板块竞价额第{local_rank}"})
        amount = _number(anchor, "auction_amount_yuan")
        a_pct = _number(anchor, "p0925_pct", "auction_pct", "0925")
        a_strength = {
            "auction_pct": a_pct, "auction_amount_yuan": amount,
            "auction_amount_yi": amount / 1e8 if amount is not None else None,
            "auction_amount_bp": (amount / market_amount * 10000 if amount is not None and market_amount > 0 else None),
            "auction_amount_rank": amount_rank.get(symbol), "plate_amount_rank": local_rank,
            "seal_to_match_ratio": (rest_bid / amount if rest_bid is not None and amount is not None and amount > 0 else None),
            "price_track_pct": {key: (None if ((auction_only and key != "0925") or (live and key == "close")) else
                _number(anchor, source)) for key, source in (
                    ("0925", "p0925_pct"), ("0932", "p0932_pct"),
                    ("0945", "p0945_pct"), ("close", "close_pct"))},
            "high_pct": None if live else _number(anchor, "high_pct"),
            "low_pct": None if live else _number(anchor, "low_pct"),
            "path_type": "UNKNOWN" if live else str(anchor.get("path_type") or "UNKNOWN"),
            "path_frequency": None if live or str(anchor.get("path_type") or "UNKNOWN") == "UNKNOWN" else
                (path_frequencies or {}).get(str(anchor.get("path_type"))),
            "same_streak_amount_rank": local_rank,
            "same_streak_count": sum(1 for row in peers if _prior_streak(row)[0] == streak) if streak else None,
            "same_streak_rank": (1 + sum(1 for row in peers if _prior_streak(row)[0] == streak and
                                          (_number(row, "auction_amount_yuan") or -1) > (amount or -1)))
                                 if streak else None,
        }
        p_amount = plate_amount.get(str(plate)) if plate else None
        total_price_count = sum(1 for row in normalized if _number(row, "p0925_pct") is not None)
        up_share = (sum(1 for row in normalized if (_number(row, "p0925_pct") or 0) > 0) / total_price_count
                    if total_price_count else None)
        stage_pct = plate_average_0945.get(str(plate)) if not auction_only else None
        visible_limit_values = [row for row in peers if _number(row, visible_key) is not None and
                                _number(row, "limit_pct") is not None]
        visible_limit_count = (sum(1 for row in visible_limit_values
                                   if _number(row, visible_key) >= _number(row, "limit_pct") - .2)
                               if visible_limit_values else None)
        state_label = str((market_state or {}).get("auction_regime") or "UNKNOWN") if auction_only else "UNKNOWN"
        if not auction_only:
            p25 = [_number(row, "p0925_pct") for row in normalized]
            p45 = [_number(row, "p0945_pct") for row in normalized]
            v25, v45 = [x for x in p25 if x is not None], [x for x in p45 if x is not None]
            if normalized and len(v25) / len(normalized) >= .8 and len(v45) / len(normalized) >= .8:
                med25, med45 = sorted(v25)[len(v25) // 2], sorted(v45)[len(v45) // 2]
                up25, up45 = sum(x > 0 for x in v25) / len(v25), sum(x > 0 for x in v45) / len(v45)
                up_count = sum(1 for row in normalized if _number(row, "p0945_pct") is not None and
                               _number(row, "limit_pct") is not None and
                               _number(row, "p0945_pct") >= _number(row, "limit_pct") - .2)
                down_count = sum(1 for row in normalized if _number(row, "p0945_pct") is not None and
                                 _number(row, "limit_pct") is not None and
                                 _number(row, "p0945_pct") <= -_number(row, "limit_pct") + .2)
                if med45 <= -1 or up45 <= .30 or down_count >= max(20, up_count):
                    state_label = "WEAK"
                elif (med25 <= -.5 or up25 <= .35) and med45 - med25 >= .5 and up45 - up25 >= .10:
                    state_label = "RECOVERING"
                elif med45 >= .5 and up45 >= .60 and up_count >= down_count:
                    state_label = "BROAD_STRONG"
                else:
                    state_label = "MIXED"
        sentiment = {"label": state_label, "asof": group.get("asof"),
                     "history_frequency": (market_state or {}).get("open_regime_frequency") or
                         ({"k": (market_state or {}).get("regime_history", {}).get("same_regime_days"),
                           "n": (market_state or {}).get("regime_history", {}).get("sample_days"),
                           "days": (market_state or {}).get("regime_history", {}).get("sample_days")}
                          if isinstance((market_state or {}).get("regime_history"), Mapping) else None),
                     "history_sample_days": (market_state or {}).get("open_regime_sample_days"),
                     "description": "单日描述，非预测"}
        candidate_rows = []
        for peer in group.get("candidates", []):
            peer_row = next((row for row in peers if row["symbol"] == str(peer.get("symbol", ""))), {})
            p = _number(peer, "p0925_pct", "auction_pct")
            b_reasons = []
            if local_rank_peer := plate_amount_rank.get(str(plate), {}).get(peer.get("symbol")):
                b_reasons.append(f"板块竞价额第{local_rank_peer}")
            plate_pct = plate_average_0925.get(str(plate)) if plate else None
            if p is not None and plate_pct is not None:
                b_reasons.append(f"竞价较板块{p - plate_pct:+.1f}pp")
            a_pct = _number(anchor, "p0925_pct", "auction_pct", "0925")
            if p is not None and a_pct is not None:
                b_reasons.append(f"竞价较A{p - a_pct:+.1f}pp")
            if peer.get("position") in {"低", "越界↓"}:
                b_reasons.append("20日位置低")
            elif peer.get("position") in {"高", "越界↑"}:
                b_reasons.append("20日位置高")
            before = _number(peer_row, "p0924_pct")
            if p is not None and before is not None:
                b_reasons.append(f"竞价内变{p - before:+.1f}pp")
            metrics = {"auction_pct": p, "auction_amount_yuan": peer.get("auction_amount_yuan"),
                       "plate_amount_share": ((peer.get("auction_amount_yuan") or 0) / p_amount
                                               if p_amount and peer.get("auction_amount_yuan") is not None else None),
                       "position20": peer.get("position"), "buyable": True,
                       "buyable_evidence": "竞价未涨停且未接近涨跌停",
                       "relative_to_plate_0945_pp": peer.get("relative_to_plate_pp"),
                       "relative_to_plate_close_pp": ((peer.get("close_pct") - plate_average_close[str(plate)])
                           if not live and peer.get("close_pct") is not None and plate_average_close.get(str(plate)) is not None else None)}
            if auction_only:
                metrics.pop("relative_to_plate_0945_pp", None)
                metrics.pop("relative_to_plate_close_pp", None)
            elif live:
                metrics.pop("relative_to_plate_close_pp", None)
            candidate_rows.append({"name": peer.get("name"), "reasons": b_reasons[:2],
                                   "metrics": metrics, "buyable": True})
        group.update({"schema_version": 2, "a_strength": a_strength,
                      "a_reasons": reasons, "plate_state": {
                          "name": plate, "member_count": len(peers),
                          "class": (((plate_metadata or {}).get(str(plate), {}).get("class") or _plate_kind(str(plate)))
                                    if plate else None),
                          "auction_amount_yuan": p_amount,
                          "auction_amount_share_bp": (p_amount / market_amount * 10000 if p_amount is not None and market_amount > 0 else None),
                          "relative_64d_multiple": None, "relative_prev_multiple": None,
                          "up_share": plate_up_share.get(str(plate)) if plate else None,
                          "equal_weight_pct": plate_average_0925.get(str(plate)) if plate else None,
                          "equal_weight_0945_pct": stage_pct,
                          "equal_weight_close_pct": None if live else plate_average_close.get(str(plate)),
                          "limit_up_count": visible_limit_count,
                          "strength_top": (plate_strength_ranked.index(str(plate)) + 1 <= 10) if plate in plate_strength_ranked else None,
                          "weak_top": (plate_weak_ranked.index(str(plate)) + 1 <= 10) if plate in plate_weak_ranked else None,
                          "relation_asof": group.get("relation_asof"),
                          "relation_lag_trading_days": relation_lag_trading_days},
                      "sentiment_state": sentiment, "b_candidates": candidate_rows,
                      "candidate_sort": "竞价额降序；同额按共同合格板块数降序"})
        group["a_to_b_schema_version"] = 2
    return result
