"""Compact, descriptive first-screen summaries for ladder and plate movement."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from math import ceil
from typing import Any
from .limit_price import is_limit_up


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _eligible(name: str, metadata: Mapping[str, Any]) -> bool:
    row = metadata.get(name, {})
    if not isinstance(row, Mapping):
        return False
    try:
        size = int(row.get("size"))
    except (TypeError, ValueError):
        return False
    return row.get("class", row.get("kind")) in {"THEME", "NICHE"} and 8 <= size <= 150 and not row.get("generic", False)


def _price_label(pct: float | None, symbol: str, price: Any = None, pre_close: Any = None,
                 unlimited: bool = False) -> str:
    if pct is None:
        return "涨幅缺失"
    if price is not None and pre_close is not None and is_limit_up(price, pre_close, symbol, unlimited=unlimited):
        return "封停价代理"
    if pct <= -5:
        return "大低开"
    if abs(pct) < 0.3:
        return "平开"
    return "高开" if pct > 0 else "低开"


def _judgement(rows: Sequence[Mapping[str, Any]], field: str) -> str:
    values = [_number(row.get(field)) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return "信息缺失"
    high = sum(value > 0 for value in values)
    low = sum(value < 0 for value in values)
    big_low = sum(value <= -5 for value in values)
    n = len(values)
    if high / n >= 2 / 3 and big_low == 0:
        return "承接强"
    if low / n >= 2 / 3:
        return "弱"
    return "分化"


def build_ladder_clusters(highboards: Sequence[Mapping[str, Any]], a_to_b: Sequence[Mapping[str, Any]],
                          plate_metadata: Mapping[str, Mapping[str, Any]], *, stage: str,
                          market_amount_yuan: float | None = None,
                          first_board_rows: Sequence[Mapping[str, Any]] = (),
                          plate_leaders: Mapping[str, Mapping[str, Any]] | None = None,
                          plate_share_changes: Mapping[str, tuple[float | None, float | None]] | None = None,
                          theme_assignments: Mapping[str, Mapping[str, Any]] | None = None,
                          theme_basis: str | None = None,
                          hot_plate_names: Iterable[str] = ()) -> dict[str, Any]:
    """Cluster yesterday's highboards using dated pool/reason themes when supplied."""
    plate_by_symbol: dict[str, str] = {}
    name_by_symbol: dict[str, str] = {}
    for row in a_to_b:
        symbol = str(row.get("a_symbol", row.get("symbol", ""))).zfill(6)
        plate = str(row.get("plate_display") or row.get("plate") or "")
        if _eligible(plate, plate_metadata):
            plate_by_symbol[symbol] = plate
            name_by_symbol[symbol] = plate
    assignments = {str(key).zfill(6): value for key, value in (theme_assignments or {}).items()
                   if isinstance(value, Mapping)}
    fallback_used = False
    share_data: dict[str, tuple[float | None, float | None]] = {}
    for row in a_to_b:
        plate = str(row.get("plate_display") or row.get("plate") or "")
        state = row.get("plate_state") if isinstance(row.get("plate_state"), Mapping) else {}
        share = _number(state.get("auction_amount_share_bp"))
        old_share, delta = (plate_share_changes or {}).get(plate, (None, None))
        if share is not None:
            share_data[plate] = (share, delta)
    field = "0925" if stage == "auction" else "0945"
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    group_names: dict[str, str] = {}
    hot_names = set(hot_plate_names)
    for source in highboards:
        if not isinstance(source, Mapping):
            continue
        symbol = str(source.get("symbol", source.get("code", ""))).zfill(6)
        row = dict(source)
        row["symbol"] = symbol
        row["pct"] = _number(source.get(field, source.get("auction_pct" if stage == "auction" else "p0945_pct")))
        row["streak"] = _number(source.get("streak", source.get("prior_streak")))
        theme = assignments.get(symbol)
        if theme and theme.get("theme_source") not in (None, "NONE"):
            key = str(theme.get("theme_key") or theme.get("theme_name") or "NONE")
            name = str(theme.get("theme_name") or "无题材")
            row["theme_source"] = theme.get("theme_source")
            row["reason_text_short"] = theme.get("reason_text_short")
            row["tier_label"] = theme.get("tier_label")
            row["secondary_themes"] = list(theme.get("secondary_themes") or [])
        else:
            name = plate_by_symbol.get(symbol, "无题材")
            key = name
            fallback_used = fallback_used or name != "无题材"
            row["theme_source"] = "STATIC_FALLBACK" if name != "无题材" else "NONE"
            row["reason_text_short"] = ""
            row["tier_label"] = ""
            row["secondary_themes"] = []
        row["plate"] = name
        group_names.setdefault(key, name)
        grouped[key].append(row)
    clusters = []
    for theme_key, rows in grouped.items():
        plate = group_names[theme_key]
        rows.sort(key=lambda row: (row["streak"] is None,
                                   -(row["streak"] if row["streak"] is not None else 0),
                                   row.get("match_amount_yuan") is None,
                                   -(_number(row.get("match_amount_yuan")) or 0), row.get("name", "")))
        known_amounts = [max(0.0, value) for row in rows
                         if (value := _number(row.get("match_amount_yuan"))) is not None]
        amount = sum(known_amounts) if known_amounts else None
        # A cluster's own highboard amount is not the whole plate amount.
        # Keep plate share missing unless the report supplied a plate-level value.
        share_bp = share_data.get(plate, (None, None))[0]
        share_basis = None
        if share_bp is None and amount is not None and market_amount_yuan and market_amount_yuan > 0:
            share_bp = amount * 10000 / market_amount_yuan
            share_basis = "簇内合计"
        if stage == "open":
            for row in rows:
                row["auction_pct"] = _number(row.get("0925", row.get("auction_pct")))
        auction_judgement = _judgement(rows, "auction_pct") if stage == "open" else None
        leader = (plate_leaders or {}).get(plate, {})
        leaders = []
        paths = []
        for row in rows:
            pct = row.get("pct")
            pct_text = "缺失" if pct is None else f"{pct:+.1f}%"
            price = next((row.get(key) for key in ("close", "current_price", "price", "px")
                          if row.get(key) is not None), None)
            pre_close = row.get("pre_close", row.get("preclose"))
            price_label = _price_label(pct, row["symbol"], price, pre_close,
                                       bool(row.get("is_new_listing") or row.get("is_unlimited_price_band")))
            leaders.append(f"{row.get('name') or '名称未知'} 昨{int(row['streak']) if row['streak'] is not None else '?'}板→{pct_text}[{price_label}]")
            paths.append({"symbol": row["symbol"], "name": row.get("name") or "名称未知", "streak": row.get("streak"),
                          "0925": _number(row.get("0925", row.get("auction_pct"))),
                          "0932": _number(row.get("0932")), "0938": _number(row.get("0938")),
                          "0945": _number(row.get("0945", row.get("p0945_pct"))),
                          "status": price_label})
        known_streaks = [r["streak"] for r in rows if r["streak"] is not None]
        clusters.append({"key": theme_key, "name": plate, "count": len(rows), "max_streak": max(known_streaks) if known_streaks else None,
                         "amount_yuan": amount, "share_bp": share_bp, "share_basis": share_basis,
                         "hot_today": plate in hot_names,
                         "theme_sources": sorted({str(row.get("theme_source") or "NONE") for row in rows}),
                         "delta_share_bp": (plate_share_changes or {}).get(plate, (None, None))[1],
                         "equal_weight_pct": (sum(r["pct"] for r in rows if r["pct"] is not None) /
                                               sum(r["pct"] is not None for r in rows)) if any(r["pct"] is not None for r in rows) else None,
                         "leaders": leaders, "paths": paths, "judgement": _judgement(rows, "pct"),
                         "auction_judgement": auction_judgement,
                         "judgement_trend": ("升级" if auction_judgement == "弱" and _judgement(rows, "pct") == "承接强"
                                             else "降级" if auction_judgement == "承接强" and _judgement(rows, "pct") == "弱"
                                             else "不变" if auction_judgement == _judgement(rows, "pct") else "分化"),
                         "leader": dict(leader) if isinstance(leader, Mapping) else {}})
    clusters.sort(key=lambda row: (row["max_streak"] is None,
                                   -(row["max_streak"] if row["max_streak"] is not None else 0),
                                   row["amount_yuan"] is None,
                                   -(_number(row["amount_yuan"]) or 0), row["name"]))
    first = []
    high_symbols = {str(row.get("symbol", "")).zfill(6) for row in highboards if isinstance(row, Mapping)}
    for row in first_board_rows:
        if not isinstance(row, Mapping) or str(row.get("symbol", "")).zfill(6) in high_symbols:
            continue
        first.append(row)
    if not highboards:
        empty_message = "无昨高标簇"
    else:
        empty_message = None
    if not first:
        first_summary = "昨首板0只"
    else:
        def counts(field: str, fallback: str) -> tuple[int, int, int, int, int]:
            values = [_number(row.get(field, row.get(fallback))) for row in first]
            values = [value for value in values if value is not None]
            up = sum(value > 0 for value in values)
            down = sum(value < 0 for value in values)
            flat = sum(value == 0 for value in values)
            locked = sum(_price_label(value, str(row.get("symbol", "")).zfill(6),
                                      next((row.get(key) for key in ("close", "current_price", "price", "px")
                                            if row.get(key) is not None), None),
                                      row.get("pre_close", row.get("preclose")),
                                      bool(row.get("is_new_listing") or row.get("is_unlimited_price_band"))) == "封停价代理"
                         for row in first for value in [_number(row.get(field, row.get(fallback)))] if value is not None)
            return up, down, flat, locked, len(first) - len(values)
        auction_counts = counts("0925", "auction_pct")
        current_counts = counts("0945", "p0945_pct")
        shown_counts = auction_counts if stage == "auction" else current_counts
        up, down, flat, locked, missing = shown_counts
        first_summary = (f"昨首板竞价高{auction_counts[0]}/低{auction_counts[1]}/平{auction_counts[2]}"
                         if stage == "open" else
                         f"昨首板竞价高{up}/低{down}/平{flat}")
        if stage == "open":
            first_summary += f"→0945高{up}/低{down}/平{flat}"
        if locked:
            first_summary += f" 封停价代理{locked}"
        if missing:
            first_summary += f" 缺{missing}只"
        pct_key = "0925" if stage == "auction" else "0945"
        fallback_key = "auction_pct" if stage == "auction" else "p0945_pct"
        strongest = sorted((row for row in first if _number(row.get(pct_key, row.get(fallback_key))) is not None),
                           key=lambda row: -(_number(row.get(pct_key, row.get(fallback_key))) or 0))[:3]
        if strongest:
            details = []
            for row in strongest:
                pct = _number(row.get(pct_key, row.get(fallback_key)))
                topic = row.get("theme") or row.get("theme_name") or row.get("main_plate") or "主题缺失"
                details.append(f"{row.get('name') or row.get('symbol')} {pct:+.1f}% {topic}")
            first_summary += ("；昨首板竞价最强：" if stage == "auction" else "；昨首板0945最强：") + "、".join(details)
    basis = theme_basis or ("登记表回退" if fallback_used else "昨日涨停池题材/历史原因")
    return {"clusters": clusters[:8], "other_cluster_count": max(0, len(clusters) - 8),
            "cluster_count": len(clusters), "empty_message": empty_message,
            "first_board_summary": first_summary, "cutoff_field": field,
            "attribution_basis": basis}


def select_plate_strength(current: Sequence[Mapping[str, Any]], previous: Sequence[Mapping[str, Any]], *,
                          eligible_names: Iterable[str]) -> dict[str, Any]:
    """Compare market share and equal-weight return, using P80 of |share delta| as a cutoff."""
    allowed = set(eligible_names)
    previous_by_name = {str(row.get("name")): row for row in previous if isinstance(row, Mapping)}
    rows = []
    for row in current:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "")
        old = previous_by_name.get(name)
        now_bp = _number(row.get("market_share_bp"))
        old_bp = _number(old.get("market_share_bp")) if old else None
        if name not in allowed or now_bp is None or old_bp is None:
            continue
        delta = now_bp - old_bp
        rows.append({"name": name, "share_bp": now_bp, "delta_bp": delta,
                     "equal_weight_pct": _number(row.get("equal_weight_pct")),
                     "top_stock_name": row.get("top_stock_name"), "top_stock_pct": _number(row.get("top_stock_pct"))})
    if not rows:
        return {"up": [], "down": [], "threshold_bp": None, "missing": True,
                "message": "0924份额缺失，变化暂不可判" if not previous else "合格板块不足，变化暂不可判"}
    magnitudes = sorted(abs(row["delta_bp"]) for row in rows)
    threshold = max(10.0, magnitudes[min(len(magnitudes) - 1, ceil(0.8 * len(magnitudes)) - 1)])
    changed = [row for row in rows if abs(row["delta_bp"]) >= threshold and row["delta_bp"] != 0]
    up = sorted((row for row in changed if row["delta_bp"] > 0), key=lambda row: (-row["delta_bp"], row["name"]))[:5]
    down = sorted((row for row in changed if row["delta_bp"] < 0), key=lambda row: (row["delta_bp"], row["name"]))[:5]
    return {"up": up, "down": down, "threshold_bp": threshold, "missing": False,
            "message": "" if up or down else "份额变化未达显著阈值"}


def build_plate_strength_from_stocks(stocks: Sequence[Mapping[str, Any]],
                                     plate_metadata: Mapping[str, Mapping[str, Any]], *,
                                     stage: str = "auction") -> dict[str, Any]:
    """Aggregate registered themes from the full point-in-time stock universe."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for stock in stocks:
        if not isinstance(stock, Mapping):
            continue
        names = stock.get("plates")
        if not isinstance(names, list):
            names = [stock.get("main_plate")] if stock.get("main_plate") else []
        for name in set(str(value) for value in names if value):
            if _eligible(name, plate_metadata):
                grouped[name].append(stock)
    current_key = "p0945_amount_yuan" if stage == "open" else "p0925_amount_yuan"
    previous_key = "p0925_amount_yuan" if stage == "open" else "p0924_amount_yuan"
    pct_key = "p0945_pct" if stage == "open" else "p0925_pct"
    def amount(row: Mapping[str, Any], key: str) -> float | None:
        value = _number(row.get(key))
        return max(0.0, value) if value is not None and isfinite(value) else None

    valid_stocks = [row for row in stocks if isinstance(row, Mapping)]
    market_now = sum(value for row in valid_stocks
                     if (value := amount(row, current_key)) is not None)
    comparable_market = [row for row in valid_stocks
                         if amount(row, current_key) is not None and amount(row, previous_key) is not None]
    market_comparable_now = sum(value for row in comparable_market
                                if (value := amount(row, current_key)) is not None)
    market_before = sum(value for row in comparable_market
                        if (value := amount(row, previous_key)) is not None)
    rows = []
    for name, members in grouped.items():
        if not 8 <= len(members) <= 150:
            continue
        with_now = [row for row in members if amount(row, current_key) is not None]
        current_coverage = len(with_now) / len(members)
        if current_coverage < 0.5:  # missing amounts are not zeros: need >=50% coverage
            continue
        now = sum(value for row in with_now if (value := amount(row, current_key)) is not None)
        comparable = [row for row in members
                      if amount(row, current_key) is not None and amount(row, previous_key) is not None]
        comparison_coverage = len(comparable) / len(members)
        comparable_now = sum(value for row in comparable
                             if (value := amount(row, current_key)) is not None)
        before = sum(value for row in comparable
                     if (value := amount(row, previous_key)) is not None)
        if now <= 0 or market_now <= 0:
            continue
        now_bp = now * 10000 / market_now
        comparable_now_bp = comparable_now * 10000 / market_comparable_now if market_comparable_now > 0 else None
        old_bp = before * 10000 / market_before if market_before > 0 else None
        delta = (comparable_now_bp - old_bp if old_bp is not None and comparison_coverage >= 0.5 else None)
        pct_rows = [(row, _number(row.get(pct_key))) for row in members]
        pct_values = [pct for _, pct in pct_rows if pct is not None]
        leader = max(with_now, key=lambda row: amount(row, current_key))
        rows.append({"name": name, "share_bp": now_bp, "previous_share_bp": old_bp,
                     "comparable_share_bp": comparable_now_bp,
                     "delta_bp": delta,
                     "equal_weight_pct": sum(pct_values) / len(pct_values) if pct_values else None,
                     "top_stock_name": leader.get("name") or leader.get("symbol"),
                     "top_stock_pct": _number(leader.get(pct_key)),
                     "member_count": len(members), "member_coverage": current_coverage,
                     "comparison_coverage": comparison_coverage, "amount_yuan": now})
    comparable = [row for row in rows if row["delta_bp"] is not None]
    if not comparable:
        missing_0924 = stage == "auction" and not any(
            _number(row.get("p0924_amount_yuan")) is not None for row in stocks if isinstance(row, Mapping))
        return {"up": [], "down": [], "rows": rows, "threshold_bp": None, "missing": True,
                "eligible_count": len(rows), "message": "缺0924" if missing_0924 else "合格板块不足"}
    magnitudes = sorted(abs(row["delta_bp"]) for row in comparable)
    threshold = max(10.0, magnitudes[min(len(magnitudes) - 1, ceil(0.8 * len(magnitudes)) - 1)])
    changed = [row for row in comparable if abs(row["delta_bp"]) >= threshold and row["delta_bp"] != 0]
    up = sorted((row for row in changed if row["delta_bp"] > 0), key=lambda row: (-row["delta_bp"], row["name"]))[:5]
    down = sorted((row for row in changed if row["delta_bp"] < 0), key=lambda row: (row["delta_bp"], row["name"]))[:5]
    return {"up": up, "down": down, "rows": rows, "threshold_bp": threshold, "missing": False,
            "eligible_count": len(rows), "message": "" if up or down else "份额变化未达显著阈值"}


def build_front_page_data(report: Mapping[str, Any], *, stage: str,
                          plate_metadata: Mapping[str, Mapping[str, Any]] | None = None,
                          stocks: Sequence[Mapping[str, Any]] = (),
                          theme_assignments: Mapping[str, Mapping[str, Any]] | None = None,
                          theme_basis: str | None = None,
                          hot_plate_names: Iterable[str] = (),
                          relation_asof: str | None = None,
                          relation_lag_trading_days: int | None = None) -> dict[str, Any]:
    chapters = report.get("chapters") if isinstance(report.get("chapters"), Mapping) else {}
    highboards = chapters.get("yesterday_highboards", []) if isinstance(chapters.get("yesterday_highboards"), list) else []
    a_to_b = report.get("a_to_b", []) if isinstance(report.get("a_to_b"), list) else []
    market = chapters.get("market_price_volume", {}) if isinstance(chapters.get("market_price_volume"), Mapping) else {}
    current = market.get("top_plates_by_share", []) if isinstance(market.get("top_plates_by_share"), list) else []
    linkage = chapters.get("plate_linkage") if isinstance(chapters.get("plate_linkage"), Mapping) else {}
    linkage_rows = linkage.get("rows", []) if isinstance(linkage.get("rows"), list) else []
    if linkage_rows:
        current = linkage_rows
    if stage == "open":
        handoff = report.get("handoff") if isinstance(report.get("handoff"), Mapping) else {}
        auction = handoff.get("auction_0925") if isinstance(handoff.get("auction_0925"), Mapping) else {}
        terminal = auction.get("final_0925") if isinstance(auction.get("final_0925"), Mapping) else {}
        previous = terminal.get("top_plates_by_share", []) if isinstance(terminal.get("top_plates_by_share"), list) else []
    else:
        snapshots = report.get("snapshots", []) if isinstance(report.get("snapshots"), list) else []
        prior = next((item for item in snapshots if isinstance(item, Mapping) and str(item.get("label", "")).replace(":", "") == "0924"), {})
        previous = prior.get("top_plates_by_share", []) if isinstance(prior, Mapping) else []
    def normalize(rows: Sequence[Mapping[str, Any]], denominator: float | None = None) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            amount = _number(row.get("amount_yuan", row.get("match_amount_yuan")))
            share = row.get("market_share_bp", row.get("amount_share_bp"))
            if share is None and amount is not None and denominator and denominator > 0:
                share = amount * 10000 / denominator
            result.append({"name": row.get("name"), "market_share_bp": share,
                           "equal_weight_pct": row.get("equal_weight_pct", row.get("equal_weight_mean_pct"))})
        return result
    metadata = plate_metadata or {}
    eligible = {name for name in metadata if _eligible(name, metadata)}
    if not eligible:
        eligible = {str(row.get("name")) for row in current if isinstance(row, Mapping)
                    and str(row.get("name", "")) and metadata and _eligible(str(row.get("name")), metadata)}
    leaders: dict[str, dict[str, Any]] = {}
    for stock in stocks:
        if not isinstance(stock, Mapping):
            continue
        amount = _number(stock.get("p0945_amount_yuan", stock.get("amount_yuan", stock.get("auction_amount_yuan"))))
        pct = _number(stock.get("p0945_pct", stock.get("auction_pct", stock.get("p0925_pct"))))
        names = stock.get("plates", [])
        if not isinstance(names, list):
            names = [stock.get("main_plate")] if stock.get("main_plate") else []
        for name in names:
            name = str(name)
            if name in eligible and amount is not None and (name not in leaders or amount > leaders[name]["amount_yuan"]):
                leaders[name] = {"name": stock.get("name") or "名称缺失", "pct": pct, "amount_yuan": amount}
    total = _number(market.get("market_match_amount_yuan"))
    ladder_total = total
    if stage == "open":
        handoff = report.get("handoff") if isinstance(report.get("handoff"), Mapping) else {}
        auction = handoff.get("auction_0925") if isinstance(handoff.get("auction_0925"), Mapping) else {}
        terminal = auction.get("final_0925") if isinstance(auction.get("final_0925"), Mapping) else {}
        ladder_total = _number(terminal.get("market_match_amount_yuan")) or ladder_total
    first = chapters.get("yesterday_limit_pool", [])
    if not isinstance(first, list):
        first = []
    assignments = theme_assignments or report.get("limit_reason_themes") or {}
    if isinstance(assignments, Mapping):
        annotated = []
        for source in first:
            if not isinstance(source, Mapping):
                continue
            symbol = str(source.get("symbol", source.get("code", ""))).zfill(6)
            theme = assignments.get(symbol)
            if isinstance(theme, Mapping) and theme.get("theme_name"):
                annotated.append({**source, "theme": theme.get("theme_name"),
                                  "theme_key": theme.get("theme_key")})
            else:
                annotated.append(dict(source))
        first = annotated
    stock_by_symbol = {str(row.get("symbol", row.get("code", ""))).zfill(6): row
                       for row in stocks if isinstance(row, Mapping)}
    first = [{**row, **({"0925": stock_by_symbol[str(row.get("symbol", "")).zfill(6)].get("p0925_pct")}
                        if stage == "auction" and str(row.get("symbol", "")).zfill(6) in stock_by_symbol else
                        {"0945": stock_by_symbol[str(row.get("symbol", "")).zfill(6)].get("p0945_pct")}
                        if stage == "open" and str(row.get("symbol", "")).zfill(6) in stock_by_symbol else {})}
             for row in first]
    current_norm = normalize(current, total)
    previous_norm = normalize(previous)
    current_shares = {str(row.get("name")): _number(row.get("market_share_bp"))
                      for row in current_norm if isinstance(row, Mapping)}
    prev_shares = {str(row.get("name")): _number(row.get("market_share_bp"))
                   for row in previous_norm if isinstance(row, Mapping)}
    plate_share_changes: dict[str, tuple[float | None, float | None]] = {}
    for row in a_to_b:
        plate = str(row.get("plate_display") or row.get("plate") or "")
        state = row.get("plate_state") if isinstance(row.get("plate_state"), Mapping) else {}
        share = current_shares.get(plate)
        if share is None:
            share = _number(state.get("auction_amount_share_bp"))
        old = prev_shares.get(plate)
        plate_share_changes[plate] = (old, share - old if share is not None and old is not None else None)
    return {"ladder": build_ladder_clusters(highboards, a_to_b, metadata, stage=stage,
                                             market_amount_yuan=ladder_total, first_board_rows=first,
                                             plate_leaders=leaders, plate_share_changes=plate_share_changes,
                                             theme_assignments=theme_assignments or report.get("limit_reason_themes"),
                                             theme_basis=theme_basis or report.get("theme_attribution_basis"),
                                             hot_plate_names=hot_plate_names or report.get("hot_plate_names", ())),
            "plate_strength": build_plate_strength_from_stocks(stocks, metadata, stage=stage),
            "eligible_plate_count": len(eligible),
            "basis": f"PIT {relation_asof} (滞后{relation_lag_trading_days}个交易日)"
            if relation_asof and relation_asof != "UNKNOWN" and relation_lag_trading_days is not None else
            f"PIT {relation_asof}" if relation_asof and relation_asof != "UNKNOWN" else "时点未知"}
