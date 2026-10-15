"""Point-in-time descriptive high-board to same-plate peer lists."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence


def _number(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    return None


def _plates(row: Mapping[str, Any], relations: Mapping[str, Sequence[str]] | None) -> list[str]:
    try:
        from .plate_kind import classify_plate_kind
    except ImportError:
        classify_plate_kind = lambda _name: "UNKNOWN"
    ordered = [row.get("pool_plate"), row.get("reason_plate"), row.get("static_plate"),
               row.get("main_plate"), row.get("hot_plate")]
    plates = row.get("plates")
    if isinstance(plates, list):
        ordered.extend(plates)
    if relations:
        ordered.extend(relations.get(str(row.get("symbol", "")).zfill(6), []))
    seen = set()
    eligible = []
    for name in ordered:
        if not name or str(name) in seen:
            continue
        seen.add(str(name))
        if classify_plate_kind(str(name)) not in {"GENERIC", "EVENT", "NOISE"}:
            eligible.append(str(name))
    return eligible[:1]


def build_a_to_b(stocks: Sequence[Mapping[str, Any]], *, trade_date: str,
                 stage: str = "close", asof: str | None = None,
                 relation_asof: str | None = None,
                 relations: Mapping[str, Sequence[str]] | None = None,
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
        for plate in _plates(row, relations):
            by_plate[plate].append(row)
    result = []
    def average(rows, key):
        values = [_number(row, key) for row in rows]
        values = [value for value in values if value is not None]
        return sum(values) / len(values) if values else None
    for anchor in normalized:
        streak = _number(anchor, "prior_streak", "prior_limit_streak", "lb_days", "streak")
        pct = _number(anchor, "p0925_pct", "auction_pct", "0925")
        band = _number(anchor, "limit_pct")
        is_sealed = bool(anchor.get("auction_limit_state") in (1, "1", "UP", "LIMIT_UP"))
        is_sealed = is_sealed or (pct is not None and band is not None and pct >= band - 0.2)
        if not ((streak is not None and streak >= 2) or is_sealed):
            continue
        anchor_plates = _plates(anchor, relations)
        base = {"a_symbol": anchor["symbol"], "a_name": anchor.get("name"),
                "a_prior_streak": int(streak) if streak is not None else None,
                "a_status": "竞价涨停价代理" if is_sealed else "昨日高标",
                "plate": anchor_plates[0] if anchor_plates else None,
                "relation_asof": relation_asof or "UNKNOWN", "asof": asof or trade_date,
                "candidates": [], "reason": None}
        if not anchor_plates:
            base["reason"] = "缺关系数据"
            result.append(base)
            continue
        peers = [row for row in by_plate.get(anchor_plates[0], []) if row["symbol"] != anchor["symbol"]]
        if not peers:
            base["reason"] = "无同伴"
            result.append(base)
            continue
        eligible = []
        for peer in peers:
            if peer.get("in_yest_pool") is True or peer.get("in_yest_pool") in (1, "1"):
                continue
            p = (_number(peer, "p0925_pct", "auction_pct", "0925") if auction_only
                 else _number(peer, "open_pct", "p0925_pct", "auction_pct", "0925"))
            limit = _number(peer, "limit_pct")
            state = peer.get("auction_limit_state")
            if state in (1, -1, "1", "-1", "UP", "DOWN", "LIMIT_UP", "LIMIT_DOWN"):
                continue
            if p is not None and limit is not None and abs(p) >= limit - 0.2:
                continue
            amount = _number(peer, "auction_amount_yuan", "match_amount_yuan", "amount_yuan")
            if amount is not None:
                eligible.append((amount, peer))
        eligible.sort(key=lambda pair: (-pair[0], str(pair[1].get("symbol", ""))))
        if not eligible:
            base["reason"] = "全部为买不到"
        for amount, peer in eligible[:max_candidates]:
            peer_plate_pct = average(by_plate.get(anchor_plates[0], []), "p0945_pct")
            peer_0945 = _number(peer, "p0945_pct", "0945")
            item = {"symbol": peer["symbol"], "name": peer.get("name"),
                    "auction_pct": _number(peer, "p0925_pct", "auction_pct", "0925"),
                    "auction_amount_yuan": amount,
                    "auction_amount_bp": _number(peer, "auction_amount_bp"),
                    "prior_range_position20_pct": _number(peer, "prior_range_position20_pct", "p0925_range_position20_pct"),
                    "position": "已知" if _number(peer, "prior_range_position20_pct", "p0925_range_position20_pct") is not None else "UNKNOWN",
                    "p0925_pct": _number(peer, "p0925_pct", "auction_pct", "0925"),
                    "p0932_pct": None if auction_only else _number(peer, "p0932_pct", "0932"),
                    "p0945_pct": None if auction_only else _number(peer, "p0945_pct", "0945"),
                    "auction_price_status": peer.get("auction_price_status"),
                    "in_yest_pool": peer.get("in_yest_pool"),
                    "prior_streak": _number(peer, "prior_streak", "prior_limit_streak", "lb_days"),
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
    return result
