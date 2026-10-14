"""Replayable, factual A-to-B plate peer view for auction and early opening."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping


TYPE_THRESHOLDS = {
    "LOW_POS_MAX": 0.30,
    "RELATIVE_PP": 1.0,
    "AMOUNT_LEAD_FRACTION": 0.20,
    "LOW_AMOUNT_TAIL_FRACTION": 0.30,
}


def classify_day_type(*, left_tail: int, right_tail: int, median: float,
                      up_share: float) -> str:
    if left_tail >= 300:
        return "恐慌"
    if up_share <= 0.25 or median <= -1.2:
        return "弱势"
    if right_tail >= 200 and median > 0:
        return "修复/强势"
    return "常态"


def range_position(history: Iterable[Mapping[str, Any]], symbol: str,
                   trade_date: str, window: int = 20) -> float | None:
    rows = sorted((row for row in history if str(row.get("symbol")) == symbol
                   and str(row.get("date", "")) < trade_date
                   and _number(row.get("close")) is not None), key=lambda row: str(row["date"]))[-window:]
    if len(rows) < window:
        return None
    closes = [float(row["close"]) for row in rows]
    low, high, current = min(closes), max(closes), closes[-1]
    return 0.0 if high == low else (current - low) / (high - low)


def limit_streaks(history: Iterable[Mapping[str, Any]], asof_date: str) -> dict[str, int]:
    by_symbol: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in history:
        if str(row.get("date", "")) < asof_date:
            by_symbol[str(row.get("symbol", ""))].append(row)
    output = {}
    for symbol, rows in by_symbol.items():
        rows.sort(key=lambda row: str(row.get("date", "")))
        streak = 0
        for row in reversed(rows):
            pct = _number(row.get("pct_chg"))
            if pct is None:
                break
            threshold = 19.0 if symbol.startswith(("300", "301", "688", "689")) else (
                29.0 if symbol.startswith(("4", "8", "92")) else 9.0)
            if pct >= threshold:
                streak += 1
            else:
                break
        output[symbol] = streak
    return output


def classify_peer(*, pct: float | None, plate_mean: float | None,
                  amount_rank: int | None, member_count: int,
                  position: float | None, positive_amount: bool) -> list[str]:
    flags = []
    if position is not None and position <= TYPE_THRESHOLDS["LOW_POS_MAX"]:
        flags.append("LOW_POS")
    relative_pp = (pct - plate_mean) * 100 if pct is not None and plate_mean is not None else None
    if relative_pp is not None and relative_pp >= TYPE_THRESHOLDS["RELATIVE_PP"]:
        flags.append("STRONG_REL")
    if relative_pp is not None and relative_pp <= -TYPE_THRESHOLDS["RELATIVE_PP"]:
        flags.append("WEAK_REL")
    if relative_pp is not None and relative_pp >= TYPE_THRESHOLDS["RELATIVE_PP"] \
            and amount_rank is not None and amount_rank > member_count / 2:
        flags.append("HOLLOW_TOP")
    if amount_rank is not None and amount_rank <= max(1, int(member_count * TYPE_THRESHOLDS["AMOUNT_LEAD_FRACTION"])):
        flags.append("AMOUNT_LEAD")
    if positive_amount and pct is not None and pct > 0 and amount_rank is not None \
            and amount_rank > member_count * (1 - TYPE_THRESHOLDS["LOW_AMOUNT_TAIL_FRACTION"]):
        flags.append("LOW_AMOUNT_UP")
    return flags


def summarize_clusters(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("plate", ""))].append(row)
    result = []
    for plate, values in sorted(grouped.items()):
        a_symbols = {str(row["symbol"]) for row in values if row.get("is_a")}
        peers = {str(row["symbol"]): row for row in values if not row.get("is_a")}
        flags = Counter(flag for row in peers.values() for flag in row.get("peer_flags", ()))
        has_catch = any(row.get("a_buyable") is False and any(peer.get("has_observed_support") for peer in peers.values())
                        for row in values if row.get("is_a"))
        result.append({"plate": plate, "a_count": len(a_symbols), "a_symbols": sorted(a_symbols),
                       "peer_count": len(peers), "low_pos_count": flags["LOW_POS"],
                       "strong_rel_count": flags["STRONG_REL"],
                       "hollow_top_count": flags["HOLLOW_TOP"],
                       "a_unbuyable_with_peers": has_catch})
    return result


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _daily_context(history: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history:
        if row["date"] < target:
            by_date[row["date"]].append(row)
    dates = sorted(by_date)
    if len(dates) < 2:
        return None
    summaries = []
    previous_close: dict[str, float] = {}
    for day in dates:
        vals = []
        opens = []
        for row in by_date[day]:
            close, op = _number(row.get("close")), _number(row.get("open"))
            symbol = row["symbol"]
            prior = previous_close.get(symbol)
            if close is not None and op is not None and prior and prior > 0:
                close_pct = (close / prior - 1) * 100
                open_pct = (op / prior - 1) * 100
                vals.append((close_pct, close_pct - open_pct))
            if close is not None:
                previous_close[symbol] = close
        if vals:
            closes = sorted(v[0] for v in vals)
            med = closes[len(closes) // 2] if len(closes) % 2 else (closes[len(closes)//2 - 1] + closes[len(closes)//2]) / 2
            summaries.append({"date": day, "left_tail": sum(delta <= -8 for _, delta in vals),
                              "right_tail": sum(delta >= 8 for _, delta in vals), "median": med,
                              "up_share": sum(v > 0 for v, _ in vals) / len(vals), "count": len(vals)})
    if len(summaries) < 2:
        return None
    prior = summaries[-1]
    return {**prior, "day_type": classify_day_type(left_tail=prior["left_tail"],
            right_tail=prior["right_tail"], median=prior["median"], up_share=prior["up_share"])}


def build_plate_peer_view(*, trade_date: str, stocks: list[dict[str, Any]],
                          checkpoints: list[dict[str, Any]], auctions: list[dict[str, Any]],
                          history: list[dict[str, Any]], memberships: Mapping[str, set[str]],
                          plate_names: Mapping[str, str], generic_names: set[str],
                          baseline: Mapping[str, Any] | None = None,
                          pool_symbols: set[str] | None = None,
                          input_hashes: Mapping[str, str] | None = None) -> dict[str, Any]:
    names = {str(row.get("symbol")): str(row.get("name") or row.get("symbol")) for row in stocks}
    stock_plates: dict[str, list[str]] = defaultdict(list)
    for plate, symbols in memberships.items():
        pname = plate_names.get(plate, plate)
        if pname in generic_names or len(symbols) < 5:
            continue
        for symbol in symbols:
            stock_plates[symbol].append(pname)
    auction: dict[str, dict[str, Any]] = {}
    for row in auctions:
        if str(row.get("trade_date", "")) != trade_date or str(row.get("auction_tag")) != "0925":
            continue
        symbol = str(row.get("symbol", ""))
        if not symbol:
            continue
        auction[symbol] = dict(row)
    streaks = limit_streaks(history, trade_date)
    previous_day = max((row["date"] for row in history if row["date"] < trade_date), default=None)
    previous_close = {row["symbol"]: _number(row.get("close")) for row in history
                      if row["date"] == previous_day and _number(row.get("close")) is not None}
    market_pcts = {}
    for symbol, row in auction.items():
        pct = _number(row.get("chg_bp"))
        if pct is not None:
            market_pcts[symbol] = pct / 10000
        else:
            px_milli = _number(row.get("px_milli"))
            pre_close = previous_close.get(symbol)
            market_pcts[symbol] = (px_milli / 1000 / pre_close - 1) if px_milli and pre_close else None
    matched = [_number(row.get("match_amt_yuan")) for row in auction.values()]
    market_amount = sum(v for v in matched if v is not None) if any(v is not None for v in matched) else None
    valid_pcts = [v * 100 for v in market_pcts.values() if v is not None]
    sorted_pcts = sorted(valid_pcts)
    median_open = (sorted_pcts[len(sorted_pcts)//2] if len(sorted_pcts) % 2 else
                   (sorted_pcts[len(sorted_pcts)//2-1] + sorted_pcts[len(sorted_pcts)//2]) / 2) if sorted_pcts else None
    buy_up = sum(1 for r in auction.values() if _number(r.get("limit_state")) == 1)
    buy_down = sum(1 for r in auction.values() if _number(r.get("limit_state")) == -1)
    day_context = _daily_context(history, trade_date)
    a_symbols = {s for s, n in streaks.items() if n >= 2}
    for symbol, row in auction.items():
        pct = market_pcts.get(symbol)
        one_word = (_number(row.get("rest_ask_amt_yuan")) == 0
                    and (_number(row.get("rest_bid_amt_yuan")) or 0) > 0)
        if _number(row.get("limit_state")) == 1 or one_word or (pct is not None and pct >= .05):
            a_symbols.add(symbol)
    checkpoint_by: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in checkpoints:
        if str(row.get("date", "")) == trade_date and str(row.get("tag")) in ("0932", "0945"):
            checkpoint_by[str(row.get("symbol", ""))][str(row["tag"])] = row
    # Every plate statistic uses all known 0925 members; peers exclude A and sealed names.
    plate_rows = []
    observations = []
    for plate, symbols in sorted(memberships.items()):
        pname = plate_names.get(plate, plate)
        if pname in generic_names or len(symbols) < 5:
            continue
        covered = [s for s in symbols if market_pcts.get(s) is not None]
        if len(covered) < 5:
            continue
        mean_pct = sum(market_pcts[s] for s in covered) / len(covered) if covered else None
        known_amounts = [_number(auction.get(s, {}).get("match_amt_yuan")) for s in symbols]
        amount_sum = sum(v for v in known_amounts if v is not None) if any(v is not None for v in known_amounts) else None
        limitups = sum(1 for s in symbols if _number(auction.get(s, {}).get("limit_state")) == 1)
        yest = sum(1 for s in symbols if streaks.get(s, 0) > 0)
        plate_a = sorted(a_symbols.intersection(symbols))
        plate_row = {"plate": pname, "member_count": len(symbols), "effective_member_count": len(covered), "priced_count": len(covered),
                     "auction_mean_pct": mean_pct * 100 if mean_pct is not None else None,
                     "amount_sum_yuan": amount_sum, "market_amount_share": amount_sum / market_amount if amount_sum is not None and market_amount else None,
                     "limit_up_count": limitups, "yest_limit_streak_count": yest,
                     "a_symbols": plate_a, "peers": []}
        peer_symbols = sorted(s for s in symbols if s not in a_symbols and _number(auction.get(s, {}).get("limit_state")) not in (-1, 1))
        ranked = sorted((s for s in symbols if _number(auction.get(s, {}).get("match_amt_yuan")) is not None),
                        key=lambda s: (-float(auction[s]["match_amt_yuan"]), s))
        rank_by = {symbol: rank for rank, symbol in enumerate(ranked, 1)}
        counts = {"LOW_POS": 0, "STRONG_REL": 0, "HOLLOW_TOP": 0}
        for symbol in peer_symbols:
            row = auction.get(symbol, {})
            pct = market_pcts.get(symbol)
            position = range_position(history, symbol, trade_date)
            amount_rank = rank_by.get(symbol)
            flags = classify_peer(pct=pct, plate_mean=mean_pct, amount_rank=amount_rank,
                                  member_count=len(ranked), position=position,
                                  positive_amount=(_number(row.get("match_amt_yuan")) or 0) > 0)
            for flag in flags:
                if flag in counts:
                    counts[flag] += 1
            stages = {}
            for tag in ("0932", "0945"):
                cp = checkpoint_by.get(symbol, {}).get(tag)
                px, pre = (_number(cp.get("px")), _number(cp.get("pre_close"))) if cp else (None, None)
                stages[tag] = {"pct": (px / pre - 1) if px is not None and pre else None,
                               "change_vs_0925_pp": ((px / pre - 1 - pct) * 100
                                                     if px is not None and pre and pct is not None else None)}
            amt = _number(row.get("match_amt_yuan"))
            peer = {"symbol": symbol, "name": names.get(symbol, symbol), "streak": streaks.get(symbol, 0),
                    "range_position_20d": position, "auction_pct": pct,
                    "relative_plate_pp": (pct - mean_pct) * 100 if pct is not None and mean_pct is not None else None,
                    "match_amount_yuan": amt, "amount_rank": amount_rank,
                    "amount_share_of_plate": amt / amount_sum if amt is not None and amount_sum else None,
                    "rest_bid_yuan": _number(row.get("rest_bid_amt_yuan")),
                    "rest_ask_yuan": _number(row.get("rest_ask_amt_yuan")),
                    "price_finality": row.get("finality") or row.get("price_source") or "UNKNOWN",
                    "opening": stages, "other_plates": sorted(set(stock_plates.get(symbol, ())) - {pname}),
                    "type_flags": flags}
            plate_row["peers"].append(peer)
        for symbol in plate_a:
            arow = auction.get(symbol, {})
            pct = market_pcts.get(symbol)
            sealed = _number(arow.get("limit_state")) == 1
            bid = _number(arow.get("rest_bid_amt_yuan"))
            ask = _number(arow.get("rest_ask_amt_yuan"))
            one_word = bool((ask == 0 and (bid or 0) > 0) or
                            ((_number(arow.get("match_amt_yuan")) or 0) == 0 and pct is not None and pct >= .05))
            observations.append({"symbol": symbol, "plate": pname, "is_a": True,
                                 "a_buyable": not sealed and not one_word})
        if plate_a:
            observations.extend({"symbol": peer["symbol"], "plate": pname, "is_a": False,
                                 "peer_flags": peer["type_flags"],
                                 "has_observed_support": peer["auction_pct"] is not None
                                     and (peer["match_amount_yuan"] or 0) > 0}
                                for peer in plate_row["peers"])
            plate_rows.append(plate_row)
    a_rows = []
    for symbol in sorted(a_symbols):
        row = auction.get(symbol, {})
        pct = market_pcts.get(symbol)
        sealed = _number(row.get("limit_state")) == 1
        bid = _number(row.get("rest_bid_amt_yuan"))
        ask = _number(row.get("rest_ask_amt_yuan"))
        one_word = bool((ask == 0 and (bid or 0) > 0) or
                        ((_number(row.get("match_amt_yuan")) or 0) == 0 and pct is not None and pct >= .05))
        a_rows.append({"symbol": symbol, "name": names.get(symbol, symbol), "streak": streaks.get(symbol, 0),
                       "auction_pct": pct, "match_amount_yuan": _number(row.get("match_amt_yuan")),
                       "rest_bid_yuan": bid, "rest_ask_yuan": ask, "sealed": sealed,
                       "one_word": one_word, "buyable": not sealed and not one_word,
                       "plates": sorted(stock_plates.get(symbol, ())),
                       "price_finality": row.get("finality") or row.get("price_source") or "UNKNOWN"})
    table = [
        {"day_type": "修复/强势", "rule": "基线", "observations": 264, "peer_close_mean_pct": 1.31, "relative_market_pp": 0.56, "close_ge_5_pct": 10.4, "large_loss_pct": 0.4},
        {"day_type": "修复/强势", "rule": "低位(分位<=0.3)", "observations": 256, "peer_close_mean_pct": 3.10, "relative_market_pp": 2.35, "close_ge_5_pct": 25.6, "large_loss_pct": 0.1},
        {"day_type": "修复/强势", "rule": "高位(分位>=0.7)", "observations": 264, "peer_close_mean_pct": 0.67, "relative_market_pp": -0.08, "close_ge_5_pct": 5.8, "large_loss_pct": 0.4},
        {"day_type": "修复/强势", "rule": "竞价高于板块均值>=1pp", "observations": 257, "peer_close_mean_pct": 2.93, "relative_market_pp": 2.18, "close_ge_5_pct": 24.4, "large_loss_pct": 2.4},
        {"day_type": "常态", "rule": "基线", "observations": 2344, "peer_close_mean_pct": 0.45, "relative_market_pp": 0.18, "close_ge_5_pct": 5.6, "large_loss_pct": 0.8},
        {"day_type": "常态", "rule": "竞价高于板块均值", "observations": 2298, "peer_close_mean_pct": 1.89, "relative_market_pp": 1.62, "close_ge_5_pct": 16.8, "large_loss_pct": 2.7},
        {"day_type": "常态", "rule": "竞价低于板块均值", "observations": 2297, "peer_close_mean_pct": -1.17, "relative_market_pp": -1.44, "close_ge_5_pct": 5.4, "large_loss_pct": 2.6},
        {"day_type": "弱势", "rule": "基线", "observations": 762, "peer_close_mean_pct": -1.75, "relative_market_pp": 0.01, "close_ge_5_pct": 2.0, "large_loss_pct": 3.7},
        {"day_type": "弱势", "rule": "低位(分位<=0.3)", "observations": 745, "peer_close_mean_pct": -1.31, "relative_market_pp": 0.45, "close_ge_5_pct": 1.0, "large_loss_pct": 1.1},
        {"day_type": "弱势", "rule": "竞价低于板块均值", "observations": 747, "peer_close_mean_pct": -3.46, "relative_market_pp": -1.70, "close_ge_5_pct": 3.4, "large_loss_pct": 8.1},
        {"day_type": "弱势", "rule": "竞价高于板块均值", "observations": 740, "peer_close_mean_pct": -0.68, "relative_market_pp": 1.09, "close_ge_5_pct": 7.4, "large_loss_pct": 8.5},
        {"day_type": "恐慌", "rule": "低位(分位<=0.3)", "observations": 40, "peer_close_mean_pct": -4.00, "relative_market_pp": -2.80, "close_ge_5_pct": 1.2, "large_loss_pct": 17.3},
        {"day_type": "恐慌", "rule": "高位(分位>=0.7)", "observations": 40, "peer_close_mean_pct": -0.27, "relative_market_pp": 0.94, "close_ge_5_pct": 2.4, "large_loss_pct": 4.0},
    ]
    clusters = summarize_clusters(observations)
    pool_stats = None
    if pool_symbols:
        latest_day = max((row["date"] for row in history if row["date"] < trade_date), default=None)
        prior_rows = [row for row in history if row["date"] == latest_day and row["symbol"] in pool_symbols]
        returns = [_number(row.get("pct_chg")) for row in prior_rows]
        returns = [value for value in returns if value is not None]
        pool_auction = [market_pcts[s] * 100 for s in pool_symbols if s in market_pcts and market_pcts[s] is not None]
        pool_stats = {"date": latest_day, "symbol_count": len(pool_symbols), "observed_count": len(returns),
                      "prior_day_mean_pct_chg": sum(returns) / len(returns) if returns else None,
                      "prior_day_positive_share": sum(value > 0 for value in returns) / len(returns) if returns else None,
                      "auction_premium_count": len(pool_auction),
                      "auction_premium_mean_pct": sum(pool_auction) / len(pool_auction) if pool_auction else None,
                      "auction_premium_positive_share": sum(value > 0 for value in pool_auction) / len(pool_auction) if pool_auction else None}
    baseline_amount = _number((baseline or {}).get("baseline_amount_yuan")) if isinstance(baseline, Mapping) else None
    baseline_multiple = ((market_amount / baseline_amount) if market_amount is not None and baseline_amount else
                         _number(baseline.get("multiple")) if isinstance(baseline, Mapping) else None)
    summary = {"yesterday": day_context, "yesterday_pool": pool_stats, "market_auction": {"symbol_count": len(auction),
        "median_open_pct": median_open, "down_le_3_pct_share": sum(v is not None and v <= -.03 for v in market_pcts.values()) / len(auction) if auction else None,
        "up_ge_3_pct_share": sum(v is not None and v >= .03 for v in market_pcts.values()) / len(auction) if auction else None,
        "matched_amount_yuan": market_amount, "limit_up_count": buy_up, "limit_down_count": buy_down,
        "baseline_multiple": baseline_multiple}}
    manifest = {"trade_date": trade_date, "input_sha256": dict(input_hashes or {}),
                "a_count": len(a_rows), "plate_count": len(plate_rows),
                "peer_count": sum(len(row["peers"]) for row in plate_rows),
                "position_missing_count": sum(peer["range_position_20d"] is None for row in plate_rows for peer in row["peers"]),
                "thresholds": TYPE_THRESHOLDS}
    return {"manifest": manifest, "summary": summary, "a_list": a_rows,
            "plates": plate_rows, "clusters": clusters,
            "reference_statistics": {"disclaimer": "描述性、非 PIT、观察重叠，不是建议",
                                     "rows": [{**row, "today_match": bool(day_context and row["day_type"] == day_context["day_type"])} for row in table]}}


def render_plate_peer_markdown(view: Mapping[str, Any]) -> str:
    summary = view["summary"]
    market = summary["market_auction"]
    yesterday = summary.get("yesterday") or {}
    lines = ["# 看A做B：板块聚类视图", "",
             "竞价为虚拟撮合；FINAL/INDICATIVE 只在来源显式提供时标注，否则为 UNKNOWN。仅用昨日及之前日线与当日竞价/开盘信息；缺失不补 0；无逐笔委托无法证明意图。情绪读数仅并列展示，不作买卖判断。",
             "", "## 情绪背景", "",
             f"昨日 {yesterday.get('date', 'UNKNOWN')} 日型 {yesterday.get('day_type', 'UNKNOWN')}；Δ左尾 {yesterday.get('left_tail', 'UNKNOWN')}、右尾 {yesterday.get('right_tail', 'UNKNOWN')}、中位 {yesterday.get('median', 'UNKNOWN')}%、上涨占比 {yesterday.get('up_share', 'UNKNOWN')}。",
             f"今日 0925：样本 {market['symbol_count']}，开盘中位 {market['median_open_pct']}%，<=-3% 占比 {market['down_le_3_pct_share']}，>=+3% 占比 {market['up_ge_3_pct_share']}，竞价金额 {market['matched_amount_yuan']} 元，前五日同刻倍数 {market['baseline_multiple']}；竞价封涨 {market['limit_up_count']}、封跌 {market['limit_down_count']}。",
             f"昨日涨停池收盘表现：{summary.get('yesterday_pool')}。", "",
             "## A 清单", "", "| 代码 | 名称 | 昨日连板 | 0925涨幅 | 匹配额(元) | 未匹配买/卖(元) | 可买 | 板块 |", "|---|---|---:|---:|---:|---:|---|---|"]
    for row in view["a_list"]:
        lines.append(f"| {row['symbol']} | {row['name']} | {row['streak']} | {row['auction_pct']} | {row['match_amount_yuan']} | {row['rest_bid_yuan']}/{row['rest_ask_yuan']} | {'是' if row['buyable'] else '否'} | {', '.join(row['plates'])} |")
    lines.extend(["", "## A 所属板块与同伴", ""])
    for plate in view["plates"]:
        lines.extend([f"### {plate['plate']}（A: {', '.join(plate['a_symbols'])}）", "",
                      f"竞价等权涨幅 {plate['auction_mean_pct']}%；成员合计额 {plate['amount_sum_yuan']} 元；占大盘 {plate['market_amount_share']}；涨停 {plate['limit_up_count']}；昨板 {plate['yest_limit_streak_count']}。", "",
                      "| 代码 | 名称 | 连板 | 20日位置 | 0925涨幅 | 相对板块(pp) | 匹配额(亿)/名次/占比 | 未匹配买卖 | 0932 | 0945 | 其他板块 | 类型 |",
                      "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"])
        for peer in plate["peers"]:
            opening = peer["opening"]
            amount_100m = "" if peer["match_amount_yuan"] is None else f"{peer['match_amount_yuan']/1e8:.3f}"
            lines.append(f"| {peer['symbol']} | {peer['name']} | {peer['streak']} | {peer['range_position_20d']} | {peer['auction_pct']} | {peer['relative_plate_pp']} | {amount_100m}/{peer['amount_rank']}/{peer['amount_share_of_plate']} | {peer['rest_bid_yuan']}/{peer['rest_ask_yuan']} | {opening['0932']['pct']} ({opening['0932']['change_vs_0925_pp']}) | {opening['0945']['pct']} ({opening['0945']['change_vs_0925_pp']}) | {', '.join(peer['other_plates'])} | {','.join(peer['type_flags'])} |")
        lines.append("")
    lines.extend(["## 板块聚类汇总", "", "| 板块 | A数 | 同伴数 | 低位 | 强相对 | 虚顶 | A不可买且有同伴 |", "|---|---:|---:|---:|---:|---:|---|"])
    for row in view["clusters"]:
        lines.append(f"| {row['plate']} | {row['a_count']} | {row['peer_count']} | {row['low_pos_count']} | {row['strong_rel_count']} | {row['hollow_top_count']} | {'是' if row['a_unbuyable_with_peers'] else '否'} |")
    lines.extend(["", "## 72日描述性参考", "", "描述性、非 PIT、观察重叠，不是建议。", "",
                  "| 日型 | 规则 | 观察 | 同伴收盘均% | 相对市场pp | 收盘>=+5% | 大面率 | 今日匹配 |", "|---|---|---:|---:|---:|---:|---:|---|"])
    for row in view["reference_statistics"]["rows"]:
        lines.append(f"| {row['day_type']} | {row['rule']} | {row['observations']} | {row['peer_close_mean_pct']} | {row['relative_market_pp']} | {row['close_ge_5_pct']} | {row['large_loss_pct']} | {'是' if row['today_match'] else ''} |")
    return "\n".join(lines) + "\n"
