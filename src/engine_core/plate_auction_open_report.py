"""Strategy-free facts for auction and 09:32 stock/plate reports."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .auction_limit_facts import _rounded_limit_price
from .plate_aggregation import StockFact, aggregate_plates
from .plate_membership import MembershipIndex, PlateHierarchy, StockPlateMemberships


_AUCTION_TAGS = ("0920", "0924", "0925")
_ZONE = ZoneInfo("Asia/Shanghai")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _price(row: Mapping[str, Any] | None) -> float | None:
    if not row:
        return None
    value = _number(row.get("px_milli", row.get("price_milli")))
    return value / 1000 if value is not None else _number(row.get("price"))


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=_ZONE) if value.tzinfo is None else value.astimezone(_ZONE)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.replace(tzinfo=_ZONE) if parsed.tzinfo is None else parsed.astimezone(_ZONE)
        except ValueError:
            return None
    return None


def _cutoff_text(value: Any, fallback: str) -> str:
    parsed = _timestamp(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S") if parsed is not None else fallback


def _pct(price: float | None, pre_close: float | None) -> float | None:
    if price is None or pre_close is None or pre_close <= 0:
        return None
    return price / pre_close - 1.0


def build_session_report(
    *, trade_date: str, snapshot: Mapping[str, Any],
    pre_close_by_symbol: Mapping[str, float | None],
    auction_by_tag: Mapping[str, Mapping[str, Mapping[str, Any]]],
    opening_by_symbol: Mapping[str, Mapping[str, Any]],
    opening_by_tag: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    open_times: tuple[str, ...] | list[str] = ("0932",),
    yest_pool_by_symbol: Mapping[str, Any] | None = None,
    degradation: tuple[str, ...] | list[str] = (),
    memberships: StockPlateMemberships | None = None,
    hierarchy: PlateHierarchy | None = None,
    baseline_auction_by_tag: Mapping[str, list[Mapping[str, Any]]] | None = None,
    hot_prev_by_plate: Mapping[str, Mapping[str, Any]] | None = None,
    hot_captures: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    stock_plate = snapshot.get("stock_plate", {})
    s2p = snapshot.get("s2p", {})
    opening_rows = [symbol for group in (opening_by_tag or {}).values() for symbol in group]
    symbols = sorted(set(map(str, stock_plate)) | set(map(str, s2p)) |
                     set(map(str, pre_close_by_symbol)) | set(map(str, opening_by_symbol)) |
                     set(map(str, opening_rows)) |
                     {str(symbol) for rows in auction_by_tag.values() for symbol in rows})
    if memberships is None:
        from .plate_membership import apply_hierarchy, build_memberships, default_alias_hierarchy
        themes = {
            symbol: list(dict.fromkeys(
                ([stock_plate.get(symbol)] if stock_plate.get(symbol) else [])
                + ([s2p[symbol]] if isinstance(s2p.get(symbol), str) else list(s2p.get(symbol, ())))
            ))
            for symbol in symbols
        }
        memberships = apply_hierarchy(build_memberships(
            symbols=symbols, pool_plate_by_symbol={}, reason_candidates_by_symbol={},
            static_themes_by_symbol=themes,
        ), hierarchy or default_alias_hierarchy())
    hierarchy = hierarchy or PlateHierarchy({}, {})
    membership_index = MembershipIndex(memberships, hierarchy)
    pool = yest_pool_by_symbol or {}
    names = snapshot.get("names", {})
    facts_by_stage: dict[str, dict[str, StockFact]] = {}
    open_times = tuple(dict.fromkeys(map(str, open_times)))
    if any(len(tag) != 4 or not tag.isdigit() for tag in open_times):
        raise ValueError("open_times must contain HHMM tags")
    tags = (*_AUCTION_TAGS, *open_times)
    default_cutoffs = {tag: f"{trade_date}T{tag[:2]}:{tag[2:]}:00" for tag in tags}
    stocks: list[dict[str, Any]] = []
    for symbol in symbols:
        pc = _number(pre_close_by_symbol.get(symbol))
        pool_row = pool.get(symbol)
        if isinstance(pool_row, Mapping):
            lb_days = pool_row.get("lb_days")
        else:
            lb_days = getattr(pool_row, "lb_days", None)
        try:
            lb_days = int(lb_days) if lb_days is not None else None
        except (TypeError, ValueError, OverflowError):
            lb_days = None
        time_rows: dict[str, Any] = {}
        flags: list[str] = []
        if pc is None or pc <= 0:
            flags.append("NO_PRE_CLOSE")
        for tag in _AUCTION_TAGS:
            raw = (auction_by_tag.get(tag) or {}).get(symbol)
            px = _price(raw)
            amt = _number((raw or {}).get("match_amt_yuan", (raw or {}).get("match_amt")))
            rest_bid = _number((raw or {}).get("rest_bid_amt_yuan", (raw or {}).get("rest_bid")))
            rest_ask = _number((raw or {}).get("rest_ask_amt_yuan", (raw or {}).get("rest_ask")))
            state = (raw or {}).get("limit_state", (raw or {}).get("ls"))
            try:
                state = int(state) if state is not None else None
            except (TypeError, ValueError, OverflowError):
                state = None
            time_rows[tag] = {"price": px, "pct_vs_pre_close": _pct(px, pc), "amount": amt,
                              "amount_kind": "MATCH", "rest_bid": rest_bid, "rest_ask": rest_ask,
                              "limit_state": state, "at_limit_price": None,
                              "input_cutoff": _cutoff_text((raw or {}).get("ts"), default_cutoffs[tag])}
            if raw is None:
                flags.append(f"NO_{tag}")
        up_limit = _rounded_limit_price(pc, symbol, 1) if pc is not None else None
        down_limit = _rounded_limit_price(pc, symbol, -1) if pc is not None else None
        for tag in open_times:
            opening = (opening_by_tag or {}).get(tag, {}).get(symbol)
            if tag == "0932" and opening is None:
                opening = opening_by_symbol.get(symbol)
            cutoff = datetime.fromisoformat(
                f"{trade_date} {tag[:2]}:{tag[2:]}:00").replace(tzinfo=_ZONE)
            opening_ts = _timestamp((opening or {}).get("ts")) if opening else None
            opening_valid = bool(opening and opening_ts and opening_ts.date() == date.fromisoformat(trade_date)
                                 and opening_ts <= cutoff)
            px = _price(opening) if opening_valid else None
            amt = _number(opening.get("amt_yuan", opening.get("amount"))) if opening_valid and opening else None
            at_limit = (Decimal(str(px)).quantize(Decimal("0.01")) in (up_limit, down_limit)
                        if px is not None and up_limit is not None and down_limit is not None else None)
            time_rows[tag] = {"price": px, "pct_vs_pre_close": _pct(px, pc), "amount": amt,
                              "amount_kind": "TRADED", "rest_bid": None, "rest_ask": None,
                              "limit_state": None, "at_limit_price": at_limit,
                              "input_cutoff": _cutoff_text((opening or {}).get("ts"), default_cutoffs[tag])}
            if not opening_valid:
                flags.append(f"NO_{tag}_ASOF_ROW")
        for tag in tags:
            facts_by_stage.setdefault(tag, {})[symbol] = StockFact(
                symbol=symbol, pre_close=pc, price=time_rows[tag]["price"], amount=time_rows[tag]["amount"],
                limit_state=time_rows[tag]["limit_state"], lb_days=lb_days,
                in_yest_pool=pool_row is not None,
            )
        membership_rows = memberships.by_symbol.get(symbol, ())
        stocks.append({
            "symbol": symbol, "name": names.get(symbol), "pre_close": pc,
            "primary_plate": next((row.plate for row in membership_rows if row.rank_in_stock == 1), None),
            "plates": [{"plate": row.plate, "rank": row.rank_in_stock, "kind": row.kind,
                        "generic": row.generic, "tier": row.tier, "source": row.source,
                        "ancestors": list(row.ancestors)} for row in membership_rows],
            "times": time_rows, "lb_days": lb_days, "in_yest_pool": pool_row is not None,
            "flags": sorted(set(flags)),
        })

    aggregations = {tag: aggregate_plates(memberships, hierarchy, facts)
                    for tag, facts in facts_by_stage.items()}
    from .plate_strength import build_incidence, compute_strength, stock_relative
    from .plate_rules import normalize_plate_name
    incidence = build_incidence(memberships, hierarchy, coverage_symbols=symbols)
    prior_days = {}
    for tag, days in (baseline_auction_by_tag or {}).items():
        eligible = [row for row in days if isinstance(row.get("trade_date"), str)
                    and row["trade_date"] < trade_date]
        prior_days[tag] = sorted(eligible, key=lambda row: row["trade_date"])[-5:]

    def strength_vectors(tag: str, facts: Mapping[str, StockFact]) -> dict[str, dict[str, Any]]:
        values = {symbol: {"y": _pct(fact.price, fact.pre_close), "amount": fact.amount,
                           "prev_close_ok": 1 if fact.pre_close and fact.pre_close > 0 else 0,
                           "rest_bid": facts_by_stage_value[tag].get(symbol, {}).get("rest_bid"),
                           "rest_ask": facts_by_stage_value[tag].get(symbol, {}).get("rest_ask"),
                           "limit_up": 1 if fact.limit_state == 1 else 0}
                  for symbol, fact in facts.items()}
        return {name: {symbol: row[name] for symbol, row in values.items()}
                for name in ("y", "amount", "prev_close_ok", "rest_bid", "rest_ask", "limit_up")}

    def same_day(value: Any) -> bool:
        if isinstance(value, (list, tuple)) and value:  # Kaipan getHisPlates returns Day as ["YYYY-MM-DD"]
            value = value[0]
        return isinstance(value, str) and value.replace("-", "")[:8] == trade_date.replace("-", "")

    # Preserve book values alongside StockFact for the plate_strength vector.
    facts_by_stage_value = {
        tag: {row["symbol"]: row["times"][tag] for row in stocks} for tag in facts_by_stage
    }
    strength_by_tag: dict[str, dict[str, Any]] = {}
    baseline_by_tag: dict[str, dict[str, float]] = {}
    for tag, facts in facts_by_stage.items():
        baseline_shares = {}
        for prior in prior_days.get(tag, ()):
            daily = {"y": {}, "amount": prior.get("amount_by_symbol", {}),
                     "prev_close_ok": {}, "rest_bid": {}, "rest_ask": {}, "limit_up": {}}
            daily_result = compute_strength(incidence, daily) if incidence.plates else []
            for item in daily_result:
                baseline_shares.setdefault(item.plate, []).append(item.conserved_share)
        baseline_by_tag[tag] = {}
        for plate, values in baseline_shares.items():
            valid_values = [value for value in values if value is not None]
            if valid_values:
                import statistics
                baseline_by_tag[tag][plate] = statistics.median(valid_values)
        strength_by_tag[tag] = ({item.plate: item for item in compute_strength(
            incidence, strength_vectors(tag, facts), baseline_share=baseline_by_tag[tag])}
            if incidence.plates else {})
    market_amounts = {tag: agg.market.amount_sum for tag, agg in aggregations.items()}
    plates: list[dict[str, Any]] = []
    for stat in aggregations["0925"].stats:
        plate = stat.plate
        stage_rows = {}
        for tag in tags:
            stage_stat = next((row for row in aggregations[tag].stats if row.plate == plate), None)
            stage_rows[tag] = None if stage_stat is None else {
                "amount_kind": "TRADED" if tag not in _AUCTION_TAGS else "MATCH",
                "covered_count": stage_stat.covered_count, "amount_sum": stage_stat.amount_sum,
                "amount_share_of_market": stage_stat.amount_share_of_market,
                "pct_equal_weight": stage_stat.pct_equal_weight,
                "pct_amount_weighted": stage_stat.pct_amount_weighted,
                "excess_vs_market": stage_stat.excess_vs_market, "up_count": stage_stat.up_count,
                "down_count": stage_stat.down_count, "flat_count": stage_stat.flat_count,
                "limit_up_count": stage_stat.limit_up_count if tag == "0925" else None,
                "max_lb_days": stage_stat.max_lb_days, "yest_pool_member_count": stage_stat.yest_pool_member_count,
                "input_cutoff": max((row["times"][tag]["input_cutoff"] for row in stocks
                                     if row["symbol"] in membership_index.members_of(plate)),
                                    default=default_cutoffs[tag]),
            }
            strength = strength_by_tag[tag].get(plate)
            strength_row = ({"beta_ridge": strength.beta_ridge, "beta_se": strength.beta_se,
                             "excess_amt_weighted": strength.excess_amt_weighted,
                             "excess_median": strength.excess_median,
                             "excess_trimmed": strength.excess_trimmed,
                             "conserved_share": strength.conserved_share,
                             "baseline_share": baseline_by_tag[tag].get(plate),
                             "share_vs_baseline": strength.share_vs_baseline,
                             "share_ratio_to_baseline": (
                                 strength.conserved_share / baseline_by_tag[tag][plate]
                                 if strength.conserved_share is not None
                                 and baseline_by_tag[tag].get(plate) is not None
                                 and baseline_by_tag[tag][plate] > 0 else None),
                             "imbalance": strength.imbalance,
                             "top1_amount_share": strength.top1_amount_share,
                             "dominant_symbol": strength.dominant_symbol,
                             "flags": list(strength.flags)}) if strength is not None else {
                                 key: None for key in ("beta_ridge", "beta_se", "excess_amt_weighted",
                                                       "excess_median", "excess_trimmed", "conserved_share",
                                                       "baseline_share", "share_vs_baseline",
                                                       "share_ratio_to_baseline", "imbalance",
                                                       "top1_amount_share", "dominant_symbol")}
            if strength is None:
                strength_row["flags"] = ["UNKNOWN"]
            if strength_row.get("baseline_share") is None or strength_row["baseline_share"] <= 0:
                strength_row["flags"].append("NO_BASELINE")
            stage_rows[tag]["strength"] = strength_row
            hot_prev = (hot_prev_by_plate or {}).get(normalize_plate_name(plate))
            stage_cutoff = _timestamp(default_cutoffs[tag])
            eligible_captures = []
            for capture in hot_captures or ():
                captured = _timestamp(capture.get("captured_local"))
                response = capture.get("response", capture)
                if (captured is not None and stage_cutoff is not None and captured <= stage_cutoff
                        and isinstance(response, Mapping) and same_day(response.get("Day"))):
                    eligible_captures.append((captured, response))
            capture, response = (max(eligible_captures, key=lambda item: item[0])
                                 if eligible_captures else (None, {}))
            hot_row = next((row for row in response.get("list", response.get("rows", ()))
                            if normalize_plate_name(row.get("name", row.get("plate", "")))
                            == normalize_plate_name(plate)), None)
            stage_rows[tag].update({
                "hot_prev_close_rank": hot_prev.get("rank") if hot_prev else None,
                "hot_prev_close_strength": hot_prev.get("strength") if hot_prev else None,
                "hot_prev_close_pct": hot_prev.get("change_pct", hot_prev.get("pct")) if hot_prev else None,
                "hot_live_rank": hot_row.get("rank") if hot_row else None,
                "hot_live_strength": hot_row.get("strength") if hot_row else None,
                "hot_live_pct": hot_row.get("change_pct", hot_row.get("pct")) if hot_row else None,
                "hot_live_captured_at": capture.isoformat() if capture else None,
                "hot_live_staleness_s": (stage_cutoff - capture).total_seconds()
                if capture and stage_cutoff else None,
            })
            if not hot_prev:
                stage_rows[tag]["strength"]["flags"].append("HOT_PREV_MISSING")
            if hot_row is None:
                stage_rows[tag]["strength"]["flags"].append("HOT_LIVE_UNAVAILABLE")
        core = sorted((symbol for symbol in membership_index.members_of(plate)
                       if facts_by_stage["0925"].get(symbol) is not None
                       and facts_by_stage["0925"][symbol].amount is not None),
                      key=lambda sym: (-(facts_by_stage["0925"].get(sym).amount or 0)
                                       if facts_by_stage["0925"].get(sym) else 0, sym))[:3]
        plates.append({
            "plate": plate, "kind": stat.kind, "generic": stat.generic,
            "member_count": stat.member_count, "times": stage_rows,
            "match_amount_progress": {
                "0920_to_0924": _delta(stage_rows["0920"], stage_rows["0924"]),
                "0924_to_0925": _delta(stage_rows["0924"], stage_rows["0925"]),
            },
            "pct_equal_weight_change_0925_to_0932": _delta_value(
                (stage_rows["0925"] or {}).get("pct_equal_weight"),
                (stage_rows["0932"] or {}).get("pct_equal_weight")),
            "core_members": [{"symbol": sym, "amount": facts_by_stage["0925"].get(sym).amount}
                             for sym in core],
        })
    kind_order = {"THEME": 0, "EVENT": 1, "GENERIC": 2, "NOISE": 3}
    plates.sort(key=lambda row: (kind_order.get(row["kind"], 4),
                                 -((row["times"]["0925"] or {}).get("amount_share_of_market") or 0), row["plate"]))
    for tag in tags:
        eligible = [row for row in plates if row["times"][tag]
                    and row["times"][tag]["strength"].get("beta_ridge") is not None
                    and not set(row["times"][tag]["strength"]["flags"])
                    .intersection({"UNKNOWN", "SINGLE_STOCK_DOMINATED", "OUTLIER_DRIVEN"})]
        eligible.sort(key=lambda row: (-row["times"][tag]["strength"]["beta_ridge"], row["plate"]))
        for rank, row in enumerate(eligible, start=1):
            row["times"][tag]["strength"]["strength_rank"] = rank
        for row in plates:
            if row["times"][tag]:
                strength_row = row["times"][tag]["strength"]
                if strength_row.get("beta_ridge") is None or set(strength_row["flags"]).intersection(
                        {"UNKNOWN", "SINGLE_STOCK_DOMINATED", "OUTLIER_DRIVEN"}):
                    strength_row["strength_rank"] = "flagged"
                else:
                    strength_row.setdefault("strength_rank", None)
    stock_relatives = {
        tag: stock_relative(incidence, strength_vectors(tag, facts)) if incidence.plates else {}
        for tag, facts in facts_by_stage.items()
    }
    plate_by_name = {row["plate"]: row for row in plates}
    for stock in stocks:
        ordered_memberships = sorted(stock["plates"], key=lambda row: (row["tier"], row["rank"], row["plate"]))
        stock["plate_summary"] = [
            {"plate": member["plate"], "tier": member["tier"], "rank": member["rank"],
             "beta_0925": ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                           .get("0925", {}).get("strength", {}).get("beta_ridge")),
             "hot_live_rank_0925": ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                                    .get("0925", {}).get("hot_live_rank")),
             "rel_leave_one_out_0925": next((item["rel_leave_one_out"] for item in
                                               stock_relatives["0925"].get(stock["symbol"], ())
                                               if item["plate"] == member["plate"]), None),
             "pct_rank_in_plate_0925": next((item["pct_rank_in_plate"] for item in
                                               stock_relatives["0925"].get(stock["symbol"], ())
                                               if item["plate"] == member["plate"]), None)}
            for member in ordered_memberships[:3]
        ]
        stock["stock_relative"] = {
            tag: {row["plate"]: {"rel_leave_one_out": row["rel_leave_one_out"],
                                  "pct_rank_in_plate": row["pct_rank_in_plate"]}
                  for row in stock_relatives[tag].get(stock["symbol"], ())}
        for tag in tags
        }
    market = {tag: {"covered_count": agg.market.covered_count,
                    "amount_covered_count": agg.market.amount_covered_count,
                    "amount_sum": agg.market.amount_sum,
                    "pct_equal_weight": agg.market.pct_equal_weight,
                    "up_count": sum(row.up_count for row in agg.stats if row.plate == "__market__")}
              for tag, agg in aggregations.items()}
    input_cutoffs = {tag: max((row["times"][tag]["input_cutoff"] for row in stocks),
                              default=default_cutoffs[tag]) for tag in tags}
    # Market direction counts are computed once per stock, independent of multi-plate membership.
    for tag, facts in facts_by_stage.items():
        changes = [_pct(fact.price, fact.pre_close) for fact in facts.values()]
        changes = [value for value in changes if value is not None]
        market[tag]["up_count"] = sum(value > 0 for value in changes)
        market[tag]["down_count"] = sum(value < 0 for value in changes)
        market[tag]["flat_count"] = sum(value == 0 for value in changes)
        market[tag]["limit_up_count"] = sum(f.limit_state == 1 for f in facts.values()) if tag == "0925" else None
        market[tag]["limit_down_count"] = sum(f.limit_state == -1 for f in facts.values()) if tag == "0925" else None
    consistency = {}
    for tag in tags:
        ranked = [row for row in plates if row["times"][tag]
                  and row["times"][tag]["strength"].get("beta_ridge") is not None
                  and not set(row["times"][tag]["strength"]["flags"])
                  .intersection({"UNKNOWN", "SINGLE_STOCK_DOMINATED", "OUTLIER_DRIVEN"})]
        ranked.sort(key=lambda row: (-row["times"][tag]["strength"]["beta_ridge"], row["plate"]))
        beta_top = [row["plate"] for row in ranked[:20]]
        hot_rows = sorted((row for row in plates if row["times"][tag]
                           and row["times"][tag].get("hot_live_rank") is not None),
                          key=lambda row: row["times"][tag]["hot_live_rank"])
        hot_top = [row["plate"] for row in hot_rows[:20]]
        common = sorted(set(beta_top) & set(hot_top))
        beta_order = {plate: i for i, plate in enumerate(beta_top)}
        hot_order = {row["plate"]: i for i, row in enumerate(hot_rows[:20])}
        corr = None
        if len(common) >= 2:
            x = [beta_order[plate] for plate in common]
            y = [hot_order[plate] for plate in common]
            mx, my = sum(x) / len(x), sum(y) / len(y)
            den = sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)
            corr = (sum((a - mx) * (b - my) for a, b in zip(x, y)) / den ** 0.5) if den else None
        consistency[tag] = {"intersection_count": len(common), "spearman": corr,
                            "common_plates": common}
    result = {"trade_date": trade_date, "stocks": stocks, "plates": plates, "market": market,
              "input_cutoff": input_cutoffs,
              "strength_hot_consistency": consistency,
              "degradation": list(degradation), "memberships_policy": memberships.policy}
    result["report_md"] = render_report_markdown(result)
    return result


def _delta(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> float | None:
    return _delta_value((right or {}).get("amount_sum"), (left or {}).get("amount_sum"))


def _delta_value(left: float | None, right: float | None) -> float | None:
    return right - left if left is not None and right is not None else None


def render_report_markdown(result: Mapping[str, Any]) -> str:
    trade_date = result["trade_date"]
    market = result["market"]
    stage_tags = tuple(market.keys())
    report_plate_by_name = {row["plate"]: row for row in result["plates"]}
    pool_rows = [row for row in result["stocks"] if row["in_yest_pool"]]
    labels = list(result.get("degradation", ()))
    label_meanings = {
        "NOT_POINT_IN_TIME": "输入快照不是结论时点的历史快照",
        "PARTIAL": "输入覆盖不完整",
        "UNKNOWN": "来源或语义未知",
    }
    prominent_labels = [label for label in labels if label == "NOT_POINT_IN_TIME"]
    lines = ([f"标签: {', '.join(prominent_labels)}", ""] if prominent_labels else [])
    lines.extend([f"# 个股板块竞价开盘情况 {trade_date}", "",
             "## 阅读说明", "",
             "竞价金额是虚拟撮合额；09:20 后不可撤单。09:32 金额是截至该时点的累计真实成交额。",
             "涨幅及超额以百分点（pp）显示，占比以百分比显示；金额以亿元显示；缺失值显示为 —。",
             "NOT_POINT_IN_TIME 表示输入快照并非该历史结论时点可得；降级标签是数据质量说明。", "",
                  "输入时点: " + " / ".join(f"{tag}={value}" for tag, value in result.get("input_cutoff", {}).items()),
                  "", "## 市场摘要", "",
             "| 时点 | 覆盖 | 金额(亿元) | 涨 | 跌 | 平 | 涨停 | 跌停 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for tag in stage_tags:
        row = market[tag]
        lines.append(f"| {tag} | {row['covered_count']} | {_amount_yi(row['amount_sum'])} | {row['up_count']} | "
                     f"{row['down_count']} | {row['flat_count']} | {_fmt(row['limit_up_count'])} | "
                     f"{_fmt(row['limit_down_count'])} |")
    if result.get("independent_by_tag"):
        lines.extend(["", render_independent_plates_markdown(result["independent_by_tag"]).rstrip(), ""])
    lines.extend(["", "## 非泛化板块 0925 匹配额占比前 20", "",
                  "| 板块 | 类型 | 成员 | 0925 匹配额占比 | 0932 涨幅变化(pp) | 涨/跌/平 |",
                  "|---|---|---:|---:|---:|---:|"])
    candidates = [row for row in result["plates"] if row["kind"] == "THEME" and not row["generic"]][:20]
    for row in candidates:
        tag25, tag32 = row["times"]["0925"] or {}, row["times"]["0932"] or {}
        change = row["pct_equal_weight_change_0925_to_0932"]
        lines.append(f"| {row['plate']} | {row['kind']} | {row['member_count']} | "
                     f"{_pctfmt(tag25.get('amount_share_of_market'))} | {_ppfmt(change)} | "
                     f"{_fmt(tag32.get('up_count'))}/{_fmt(tag32.get('down_count'))}/{_fmt(tag32.get('flat_count'))} |")
    strength_heading = ("## 旧口径板块强度（参考）与 Kaipan 热板参考"
                        if result.get("independent_by_tag") else "## 板块强度与 Kaipan 热板参考")
    lines.extend(["", strength_heading, "",
                  "各时点分别计算；0925 竞价金额为虚拟撮合额，0932 金额为真实成交额。可排名板块按 beta 降序，flagged 板块单列且不排名。",
                  "强度值 beta、SE 和超额使用百分点（pp）；份额、不平衡使用百分比。", ""])
    for tag in stage_tags:
        stage_rows = [(row, row["times"].get(tag)) for row in result["plates"]]
        stage_rows = [(row, stage) for row, stage in stage_rows if stage is not None]
        flagged_rows = [(row, stage) for row, stage in stage_rows
                        if set(stage["strength"].get("flags", ()))
                        .intersection({"SINGLE_STOCK_DOMINATED", "OUTLIER_DRIVEN", "UNKNOWN"})]
        ranked_rows = [(row, stage) for row, stage in stage_rows
                       if stage["strength"].get("beta_ridge") is not None
                       and not set(stage["strength"].get("flags", ()))
                       .intersection({"SINGLE_STOCK_DOMINATED", "OUTLIER_DRIVEN", "UNKNOWN"})]
        ranked_rows.sort(key=lambda pair: (-pair[1]["strength"]["beta_ridge"], pair[0]["plate"]))
        if tag in ("0920", "0924"):
            lines.extend([f"### {tag} 板块强度（简表）", "",
                          "| 板块 | 匹配额(亿元) | 匹配额占比(%) | 份额 vs 基线(pp) | 涨幅(pp) |",
                          "|---|---:|---:|---:|---:|"])
            for row, stage in ranked_rows[:20]:
                strength = stage["strength"]
                lines.append(f"| {row['plate']} | {_amount_yi(stage.get('amount_sum'))} | "
                             f"{_pctfmt(stage.get('amount_share_of_market'))} | "
                             f"{_ratiofmt(strength)} | {_ppfmt(stage.get('pct_equal_weight'))} |")
            continue

        lines.extend([f"### {tag} 板块强度（可排名前 20）", "",
                      f"| 板块 | {'匹配额 MATCH（虚拟撮合,亿元）' if tag == '0925' else '成交额 TRADED（真实,亿元）'} | 市场份额(%) | beta/SE(pp) | 加权超额(pp) | 中位超额(pp) | 截尾超额(pp) | Top1占比(%) | 份额/基线 | 不平衡(%) | 热板前收排名 | 热板当日排名 |",
                      "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for row, stage in ranked_rows[:20]:
            strength = stage["strength"]
            lines.append(f"| {row['plate']} | {_amount_yi(stage.get('amount_sum'))} | "
                         f"{_pctfmt(stage.get('amount_share_of_market'))} | "
                         f"{_ppfmt(strength.get('beta_ridge'))} / {_ppfmt(strength.get('beta_se'))} | "
                         f"{_ppfmt(strength.get('excess_amt_weighted'))} | {_ppfmt(strength.get('excess_median'))} | "
                         f"{_ppfmt(strength.get('excess_trimmed'))} | "
                         f"{_pctfmt(strength.get('top1_amount_share'))} | {_ratiofmt(strength)} | "
                         f"{_pctfmt(strength.get('imbalance'))} | {_fmt(stage.get('hot_prev_close_rank'))} | "
                         f"{_fmt(stage.get('hot_live_rank'))} |")
        lines.extend(["", f"### {tag} Flagged 板块", "",
                      "| 板块 | 标记 | 金额(亿元) | 市场份额(%) | 主导个股 | 主导个股涨幅(pp) |",
                      "|---|---|---:|---:|---|---:|"])
        for row, stage in flagged_rows:
            strength = stage["strength"]
            dominant = strength.get("dominant_symbol")
            stock = next((item for item in result["stocks"] if item["symbol"] == dominant), None)
            stock_stage = (stock or {}).get("times", {}).get(tag, {})
            lines.append(f"| {row['plate']} | {','.join(strength.get('flags', ())) or 'UNKNOWN'} | "
                         f"{_amount_yi(stage.get('amount_sum'))} | {_pctfmt(stage.get('amount_share_of_market'))} | "
                         f"{_fmt(dominant)} | {_ppfmt(stock_stage.get('pct_vs_pre_close'))} |")
    lines.extend(["", "### Beta 与当日热板 Top20 一致性", "",
                  "| 时点 | 交集数 | Spearman |", "|---|---:|---:|"])
    for tag, row in result.get("strength_hot_consistency", {}).items():
        lines.append(f"| {tag} | {row['intersection_count']} | {_fmt(row['spearman'])} |")
    def stock_table(title: str, rows: list[Mapping[str, Any]], extra: str = "") -> None:
        headers = ["代码", "名称", "连板数", "主板块"]
        for stage_tag in stage_tags:
            kind = "MATCH" if stage_tag in _AUCTION_TAGS else "TRADED"
            headers.extend((f"{stage_tag}涨幅(pp)", f"{stage_tag} {kind}(亿元)"))
        headers.extend(("0925涨停有封单", "板块前3(tier/beta/热板排名/个股超额/涨幅分位)"))
        if extra:
            headers.extend(extra.strip(" | ").split(" | "))
        lines.extend(["", f"## {title}", "",
                      "| " + " | ".join(headers) + " |",
                      "|" + "|".join("---" for _ in headers) + "|"])
        for row in rows[:40]:
            times = row["times"]
            pieces = [row["symbol"], row.get("name") or "", _fmt(row.get("lb_days")),
                      row.get("primary_plate") or ""]
            for tag in stage_tags:
                fact = times[tag]
                pieces.extend((_ppfmt(fact.get("pct_vs_pre_close")), _amount_yi(fact.get("amount"))))
            a25 = times["0925"]
            seal_status = ("—" if a25.get("limit_state") is None else
                           "是" if a25.get("limit_state") == 1 and (a25.get("rest_bid") or 0) > 0 else "否")
            pieces.extend((seal_status,
                      "; ".join(f"{plate['plate']}({plate['tier']}/{_ppfmt(plate['beta_0925'])}/"
                                     f"{_fmt(plate['hot_live_rank_0925'])}/{_ppfmt(plate['rel_leave_one_out_0925'])}/"
                                     f"{_pctfmt(plate['pct_rank_in_plate_0925'])})"
                                     for plate in row.get("plate_summary", ())) or "—"))
            if extra == " | rest_bid(亿元) | rest_ask(亿元) | 封单额(亿元)":
                seal = a25.get("rest_bid") if a25.get("limit_state") == 1 else (
                    a25.get("rest_ask") if a25.get("limit_state") == -1 else None)
                pieces.extend((_amount_yi(a25.get("rest_bid")), _amount_yi(a25.get("rest_ask")),
                               _amount_yi(seal)))
            elif extra:
                primary = row.get("primary_plate")
                relative = row.get("stock_relative", {}).get("0925", {}).get(primary, {})
                pieces.extend((_ppfmt(((report_plate_by_name.get(primary) or {}).get("times", {})
                                       .get("0925", {}).get("strength", {}).get("beta_ridge"))),
                               _ppfmt(relative.get("rel_leave_one_out"))))
            lines.append("| " + " | ".join(pieces) + " |")

    stock_table("昨日涨停池成员", pool_rows)
    limit_rows = sorted((row for row in result["stocks"]
                         if row["times"]["0925"].get("limit_state") in (-1, 1)),
                        key=lambda row: (0 if row["times"]["0925"].get("limit_state") == 1 else 1,
                                         row["symbol"]))
    stock_table("0925 涨停/跌停个股", limit_rows,
                 " | rest_bid(亿元) | rest_ask(亿元) | 封单额(亿元)")
    amount_rows = sorted((row for row in result["stocks"] if row["times"]["0925"].get("amount") is not None),
                         key=lambda row: (-row["times"]["0925"]["amount"], row["symbol"]))[:30]
    stock_table("0925 匹配额前 30 个股", amount_rows, " | 主板块 beta(pp) | rel_leave_one_out(pp)")
    if result["stocks"] and any(row.get("labels") for row in result["stocks"]):
        tag = stage_tags[-1]
        def label_row(stock: Mapping[str, Any]) -> tuple[Any, ...]:
            label = stock.get("labels", {}).get(tag, {})
            plate = label.get("plate") or {}
            direction = label.get("direction")
            return (stock["symbol"], stock.get("name") or "", label.get("source", "NO_PLATE"),
                    plate.get("name") or "—",
                    "偏强" if direction == "STRONG" else "偏弱" if direction == "WEAK" else "—",
                    ",".join(label.get("flags", ())) or "—",
                    _ppfmt(stock.get("times", {}).get(tag, {}).get("pct_vs_pre_close")),
                    _amount_yi(stock.get("times", {}).get(tag, {}).get("amount")))
        def labeled_section(title: str, rows: list[Mapping[str, Any]]) -> None:
            lines.extend(["", f"### {title}", "", "| 代码 | 名称 | 标签来源 | 板块 | 方向 | flags | 涨幅(pp) | 金额(亿元) |",
                          "|---|---|---|---|---|---|---:|---:|"])
            for stock in rows:
                lines.append("| " + " | ".join(map(str, label_row(stock))) + " |")
        tagged = [row for row in result["stocks"] if tag in row.get("labels", {})]
        count = {source: sum(row["labels"][tag].get("source") == source for row in tagged)
                 for source in ("DYNAMIC", "STATIC_NO_SIGNAL", "NO_PLATE")}
        lines.extend(["", f"## 个股标签（{tag}）", "", f"来源计数: DYNAMIC {count['DYNAMIC']} / "
                      f"STATIC_NO_SIGNAL {count['STATIC_NO_SIGNAL']} / NO_PLATE {count['NO_PLATE']}。"])
        limit_rows = [row for row in tagged if row.get("times", {}).get("0925", {}).get("limit_state") == 1]
        by_return = sorted(tagged, key=lambda row: (row.get("times", {}).get(tag, {}).get("pct_vs_pre_close") is None,
            -(row.get("times", {}).get(tag, {}).get("pct_vs_pre_close") or 0), row["symbol"]))
        labeled_section("涨停个股标签", limit_rows)
        labeled_section("涨幅前 40 标签", by_return[:40])
        labeled_section("跌幅后 20 标签", list(reversed(by_return[-20:])))
        for flag in ("NO_PLATE", "EVENT_ONLY", "COUNTER_TREND"):
            flagged_rows = [row for row in tagged if flag in row["labels"][tag].get("flags", ())]
            lines.extend(["", f"{flag}: " + ("、".join(
                f"{row['symbol']} {row.get('name') or ''} {_ppfmt(row['times'][tag].get('pct_vs_pre_close'))} "
                f"{_amount_yi(row['times'][tag].get('amount'))}" for row in flagged_rows) or "—")])
    lines.extend(["", "## 数据质量与降级标签", "", f"- 竞价金额为虚拟匹配额；09:32 金额为真实累计成交额。",
                  f"- 昨日池成员数: {len(pool_rows)}。", f"- 降级标签: {', '.join(result['degradation']) or 'NONE'}。",
                  *[f"- {label}: {label_meanings.get(label, '数据质量标签，具体含义由对应输入契约定义。')}"
                    for label in labels],
                  "- 缺失值保持 null；多板块统计按成员并集去重。", ""])
    return "\n".join(lines)


def render_independent_plates_markdown(independent_by_tag: Mapping[str, Mapping[str, Any]]) -> str:
    """Render already-computed independent plate selection facts."""
    lines = ["## 独立板块（净效应）", "",
             "方向为相对市场；结论为统计显著性(BH FDR 10%)，非预测。NO_SIGNAL 不出榜。",
             "绝对截尾均值与相对市场差异均以百分点（pp）显示。", ""]
    for tag in independent_by_tag:
        result = independent_by_tag.get(tag)
        if not isinstance(result, Mapping):
            continue
        market = result.get("market", {})
        market_median = market.get("median") if isinstance(market, Mapping) else None
        lines.extend([f"### {tag}", "",
                      (f"撮合收集中, 覆盖 {market.get('n', 0)} 只(仅有有效撮合价者), 低置信度, 仅作事实。"
                       if tag in ("0920", "0924") else ""),
                      "| 排名 | 板块 | 类别 | 方向 | 绝对截尾均值% | 相对市场pp | z | 成员数 | 残差成员 | 涨停数 | 连续入选时点数 | 上一时点方向 | Top1占比 | 本板块新解释数 | Kaipan 热板名次(参考) |",
                      "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|"])
        for row in result.get("selected", ()):
            absolute = row.get("trim_mean")
            delta = absolute - market_median if isinstance(absolute, (int, float)) and isinstance(
                market_median, (int, float)) else None
            lines.append(
                f"| {_fmt(row.get('rank'))} | {row.get('name', row.get('code', ''))} | {row.get('class', '')} | "
                f"{'偏强' if row.get('direction') == 'STRONG' else '偏弱' if row.get('direction') == 'WEAK' else '—'} | {_ppfmt(absolute)} | {_ppfmt(delta)} | {_zfmt(row.get('z'))} | "
                f"{_fmt(row.get('n'))} | {_fmt(row.get('residual_n'))} | {_fmt(row.get('limit_up_count'))} | "
                f"{_fmt(row.get('consecutive_selected'))} | "
                f"{'偏强' if row.get('previous_direction') == 'STRONG' else '偏弱' if row.get('previous_direction') == 'WEAK' else '—'} | "
                f"{_pctfmt(row.get('top1_weight_share'))} | {_fmt(row.get('explained_members'))} | "
                f"{_fmt(row.get('hot_rank'))} |"
            )
        uncovered = result.get("uncovered", {})
        lines.extend([f"| — | 未被覆盖个股 | — | — | {_ppfmt(uncovered.get('trim_mean'))} | — | — | "
                      f"{_fmt(uncovered.get('n'))} | — | — | — | — | — |", "",
                      f"未覆盖分布：中位 {_ppfmt(uncovered.get('median'))}；P10 {_ppfmt(uncovered.get('p10'))}；"
                      f"P90 {_ppfmt(uncovered.get('p90'))}。无有效价格 {_fmt(result.get('missing_count'))} 只(已排除, 非 0)。", ""])
        stability = result.get("stability", {})
        if stability.get("previous_jaccard") is not None:
            lines.extend([f"相邻时点入选集合 Jaccard: {stability['previous_jaccard']:.3f} "
                          f"({stability.get('previous_selected_count')} → {stability.get('selected_count')})", ""])
        controls = result.get("controls_top", ())
        if controls:
            lines.append("控制因子（按 |z| 前 8）: " + "、".join(
                f"{row.get('name', row.get('code', ''))} z={_zfmt(row.get('z')) if row.get('z') is not None else '样本不足'}" for row in controls
            ))
            lines.append("")
        if result.get("event_plates"):
            lines.extend(["事件类, 仅作事实。", "| 事件板块 | 成员有价数 | 截尾均值% | 涨停数 | 成员涨幅前3 |",
                          "|---|---:|---:|---:|---|"])
            for row in result["event_plates"]:
                top = "、".join(f"{item['symbol']}({_ppfmt(item.get('ret'))})" for item in row.get("top3", ()))
                lines.append(f"| {row['name']} | {row['covered_count']} | {_ppfmt(row.get('trim_mean'))} | "
                             f"{row['limit_up_count']} | {top or '—'} |")
            lines.append("")
        if result.get("generic_controls"):
            lines.extend(["控制因子(仅供参考)", "| 板块 | 有价成员 | 相对市场pp | z |", "|---|---:|---:|---:|"])
            for row in result["generic_controls"]:
                lines.append(f"| {row['name']} | {row['n']} | {_ppfmt(row.get('relative_market_pp'))} | "
                             f"{_zfmt(row.get('z')) if row.get('z') is not None else '样本不足'} |")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _fmt(value: Any) -> str:
    return "—" if value is None else str(value)


def _zfmt(value: Any) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def _pctfmt(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _ppfmt(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f}pp"


def _ratiofmt(strength: Mapping[str, Any]) -> str:
    baseline = _number(strength.get("baseline_share"))
    ratio = _number(strength.get("share_ratio_to_baseline"))
    if ratio is None and baseline is not None and baseline > 0:
        share = _number(strength.get("conserved_share"))
        ratio = share / baseline if share is not None else None
    return "—" if ratio is None else f"×{ratio:.2f}"


def _amount_yi(value: float | None) -> str:
    return "—" if value is None else f"{value / 100_000_000:.2f}"


__all__ = ["build_session_report", "render_report_markdown"]
