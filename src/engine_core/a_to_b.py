"""Point-in-time descriptive high-board to same-plate peer lists."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from .plate_kind import classify_plate_kind


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
        kind = str(meta.get("class", meta.get("kind", classify_plate_kind(name)))).upper()
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
    chosen = select_main_plate(eligible, metadata, source_candidates=source_candidates,
                               min_size=min_size, max_size=max_size)
    return [chosen] if chosen else []


def _qualified_plates(row: Mapping[str, Any], relations: Mapping[str, Sequence[str]] | None,
                      metadata: Mapping[str, Mapping[str, Any]] | None,
                      *, min_size: int, max_size: int) -> set[str]:
    candidates = [str(value) for value in (
        row.get("pool_plate"), row.get("reason_plate"), row.get("static_plate"),
        row.get("main_plate"), row.get("hot_plate"))
                  if value]
    candidates.extend(str(value) for value in (row.get("plates") or []) if value)
    if relations:
        candidates.extend(str(value) for value in relations.get(str(row.get("symbol", "")).zfill(6), []))
    return {name for name in candidates if select_main_plate(
        [name], metadata, min_size=min_size, max_size=max_size) is not None}


def build_a_to_b(stocks: Sequence[Mapping[str, Any]], *, trade_date: str,
                 stage: str = "close", asof: str | None = None,
                 relation_asof: str | None = None,
                 relations: Mapping[str, Sequence[str]] | None = None,
                 plate_metadata: Mapping[str, Mapping[str, Any]] | None = None,
                 min_plate_size: int = 8, max_plate_size: int = 150,
                 max_candidates: int = 5) -> list[dict[str, Any]]:
    """Return factual A→B rows using only auction and stage-available values.

    Intraday stages deliberately omit close fields even when the input contains them.
    """
    stage_key = stage.lower()
    live = stage_key not in {"close", "closing", "eod"}
    auction_only = stage_key in {"auction", "auction_0926", "auction_0925"}
    by_plate: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    normalized = []
    for source in stocks:
        row = dict(source)
        row["symbol"] = str(row.get("symbol", row.get("code", ""))).zfill(6)
        normalized.append(row)
        for plate in _plates(row, relations, plate_metadata, min_size=min_plate_size,
                             max_size=max_plate_size):
            by_plate[plate].append(row)
    result = []
    def average(rows, key):
        values = [_number(row, key) for row in rows]
        values = [value for value in values if value is not None]
        return sum(values) / len(values) if values else None
    for anchor in normalized:
        streak, streak_conflict = _prior_streak(anchor)
        is_unlimited = bool(anchor.get("is_unlimited_price_band") or
                            anchor.get("price_band_label") in {"新股/无涨跌幅限制", "新股/无涨跌停价"} or
                            anchor.get("auction_price_status") in {"UNLIMITED", "无涨跌停价", "无涨跌幅限制"})
        state = anchor.get("auction_limit_state")
        state_up = state in (1, "1", "UP", "LIMIT_UP")
        rest_bid = _number(anchor, "rest_bid_yuan", "rest_bid")
        is_sealed = state_up and rest_bid is not None and rest_bid > 0
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
                "plate": anchor_plates[0] if anchor_plates else None,
                "relation_asof": relation_asof or "UNKNOWN", "asof": asof or trade_date,
                "candidates": [], "reason": None}
        if not anchor_plates:
            base["reason"] = "无合格主题板块"
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
                peer_plates = _qualified_plates(peer, relations, plate_metadata,
                    min_size=min_plate_size, max_size=max_plate_size)
                anchor_qualified = _qualified_plates(anchor, relations, plate_metadata,
                    min_size=min_plate_size, max_size=max_plate_size)
                common_count = len(peer_plates.intersection(anchor_qualified))
                eligible.append((amount, common_count, peer))
        eligible.sort(key=lambda pair: (-pair[0], -pair[1], str(pair[2].get("symbol", ""))))
        if not eligible:
            base["reason"] = ("缺少竞价价/额" if has_missing_auction else
                              "同伴全部买不到" if buyable_peers == 0 else "无合格同伴")
        for amount, common_count, peer in eligible[:max_candidates]:
            peer_plate_pct = average(by_plate.get(anchor_plates[0], []), "p0945_pct")
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
    return result
