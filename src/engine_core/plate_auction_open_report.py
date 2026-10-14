"""Strategy-free facts for auction and 09:32 stock/plate reports."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .auction_limit_facts import _rounded_limit_price
from .plate_aggregation import StockFact, aggregate_plates
from .plate_membership import PlateHierarchy, StockPlateMemberships, members_of


_TAGS = ("0920", "0924", "0925")
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
    symbols = sorted(set(map(str, stock_plate)) | set(map(str, s2p)) |
                     set(map(str, pre_close_by_symbol)) | set(map(str, opening_by_symbol)) |
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
    pool = yest_pool_by_symbol or {}
    names = snapshot.get("names", {})
    facts_by_stage: dict[str, dict[str, StockFact]] = {}
    default_cutoffs = {"0920": f"{trade_date}T09:20:00", "0924": f"{trade_date}T09:24:00",
                       "0925": f"{trade_date}T09:25:00", "0932": f"{trade_date}T09:32:00"}
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
        for tag in _TAGS:
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
        opening = opening_by_symbol.get(symbol)
        cutoff = datetime.fromisoformat(f"{trade_date} 09:32:00").replace(tzinfo=_ZONE)
        opening_ts = _timestamp((opening or {}).get("ts")) if opening else None
        opening_valid = bool(opening and opening_ts and opening_ts.date() == date.fromisoformat(trade_date)
                             and opening_ts <= cutoff)
        px = _price(opening) if opening_valid else None
        amt = _number(opening.get("amt_yuan", opening.get("amount"))) if opening_valid and opening else None
        up_limit = _rounded_limit_price(pc, symbol, 1) if pc is not None else None
        down_limit = _rounded_limit_price(pc, symbol, -1) if pc is not None else None
        at_limit = (Decimal(str(px)).quantize(Decimal("0.01")) in (up_limit, down_limit)
                    if px is not None and up_limit is not None and down_limit is not None else None)
        time_rows["0932"] = {"price": px, "pct_vs_pre_close": _pct(px, pc), "amount": amt,
                              "amount_kind": "TRADED", "rest_bid": None, "rest_ask": None,
                              "limit_state": None, "at_limit_price": at_limit,
                              "input_cutoff": _cutoff_text((opening or {}).get("ts"), default_cutoffs["0932"])}
        if not opening_valid:
            flags.append("NO_0932_ASOF_ROW")
        for tag in (*_TAGS, "0932"):
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

    aggregations = {tag: aggregate_plates(memberships, hierarchy, facts) for tag, facts in facts_by_stage.items()}
    from .plate_strength import build_incidence, compute_strength
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
        for tag in (*_TAGS, "0932"):
            stage_stat = next((row for row in aggregations[tag].stats if row.plate == plate), None)
            stage_rows[tag] = None if stage_stat is None else {
                "amount_kind": "TRADED" if tag == "0932" else "MATCH",
                "covered_count": stage_stat.covered_count, "amount_sum": stage_stat.amount_sum,
                "amount_share_of_market": stage_stat.amount_share_of_market,
                "pct_equal_weight": stage_stat.pct_equal_weight,
                "pct_amount_weighted": stage_stat.pct_amount_weighted,
                "excess_vs_market": stage_stat.excess_vs_market, "up_count": stage_stat.up_count,
                "down_count": stage_stat.down_count, "flat_count": stage_stat.flat_count,
                "limit_up_count": stage_stat.limit_up_count if tag == "0925" else None,
                "max_lb_days": stage_stat.max_lb_days, "yest_pool_member_count": stage_stat.yest_pool_member_count,
                "input_cutoff": max((row["times"][tag]["input_cutoff"] for row in stocks
                                     if row["symbol"] in members_of(plate, memberships, hierarchy)),
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
                             "imbalance": strength.imbalance,
                             "top1_amount_share": strength.top1_amount_share,
                             "dominant_symbol": strength.dominant_symbol,
                             "flags": list(strength.flags)}) if strength is not None else {
                                 key: None for key in ("beta_ridge", "beta_se", "excess_amt_weighted",
                                                       "excess_median", "excess_trimmed", "conserved_share",
                                                       "baseline_share", "share_vs_baseline", "imbalance",
                                                       "top1_amount_share", "dominant_symbol")}
            if strength is None:
                strength_row["flags"] = ["UNKNOWN"]
            if strength_row.get("baseline_share") is None:
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
        core = sorted((symbol for symbol in members_of(plate, memberships, hierarchy)
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
    for tag in (*_TAGS, "0932"):
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
    market = {tag: {"covered_count": agg.market.covered_count,
                    "amount_covered_count": agg.market.amount_covered_count,
                    "amount_sum": agg.market.amount_sum,
                    "pct_equal_weight": agg.market.pct_equal_weight,
                    "up_count": sum(row.up_count for row in agg.stats if row.plate == "__market__")}
              for tag, agg in aggregations.items()}
    input_cutoffs = {tag: max((row["times"][tag]["input_cutoff"] for row in stocks),
                              default=default_cutoffs[tag]) for tag in (*_TAGS, "0932")}
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
    for tag in (*_TAGS, "0932"):
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
    pool_rows = [row for row in result["stocks"] if row["in_yest_pool"]]
    labels = [label for label in result.get("degradation", ()) if label == "NOT_POINT_IN_TIME"]
    lines = ([f"标签: {', '.join(labels)}", ""] if labels else [])
    lines.extend([f"# 个股板块竞价开盘情况 {trade_date}", "",
                  "输入时点: " + " / ".join(f"{tag}={value}" for tag, value in result.get("input_cutoff", {}).items()),
                  "", "## 市场摘要", "",
             "| 时点 | 覆盖 | 金额(元) | 涨 | 跌 | 平 | 涨停 | 跌停 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for tag in ("0920", "0924", "0925", "0932"):
        row = market[tag]
        lines.append(f"| {tag} | {row['covered_count']} | {_fmt(row['amount_sum'])} | {row['up_count']} | "
                     f"{row['down_count']} | {row['flat_count']} | {_fmt(row['limit_up_count'])} | "
                     f"{_fmt(row['limit_down_count'])} |")
    lines.extend(["", "## 非泛化板块 0925 匹配额占比前 20", "",
                  "| 板块 | 类型 | 成员 | 0925 匹配额占比 | 0932 涨幅变化 | 涨/跌/平 |",
                  "|---|---|---:|---:|---:|---:|"])
    candidates = [row for row in result["plates"] if row["kind"] == "THEME" and not row["generic"]][:20]
    for row in candidates:
        tag25, tag32 = row["times"]["0925"] or {}, row["times"]["0932"] or {}
        change = row["pct_equal_weight_change_0925_to_0932"]
        lines.append(f"| {row['plate']} | {row['kind']} | {row['member_count']} | "
                     f"{_pctfmt(tag25.get('amount_share_of_market'))} | {_pctfmt(change)} | "
                     f"{_fmt(tag32.get('up_count'))}/{_fmt(tag32.get('down_count'))}/{_fmt(tag32.get('flat_count'))} |")
    lines.extend(["", "## 板块强度与 Kaipan 热板参考", "",
                  "封顶岭回归 beta_ridge 只在无 UNKNOWN、SINGLE_STOCK_DOMINATED、OUTLIER_DRIVEN 的板块中排名；flagged 行显示主导个股。竞价额为 MATCH，0932 额为 TRADED，分别计算。", "",
                  "| 板块 | 时点 | beta | SE | 加权超额 | 中位超额 | 截尾超额 | 守恒份额 | 基线份额 | vs基线 | 不平衡 | Top1占比 | 主导个股 | 排名 | 标记 | 热板前收排名/强度/涨幅 | 热板当日排名/强度/涨幅 | 采集时间 | 延迟秒 |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|---|---|---|---:|"])
    for row in candidates:
        for tag in ("0920", "0924", "0925", "0932"):
            stage = row["times"][tag]
            if stage is None:
                continue
            strength = stage["strength"]
            prev = "/".join(_fmt(stage.get(f"hot_prev_close_{field}")) for field in ("rank", "strength", "pct"))
            live = "/".join(_fmt(stage.get(f"hot_live_{field}")) for field in ("rank", "strength", "pct"))
            lines.append(f"| {row['plate']} | {tag} ({stage.get('amount_kind', 'MATCH')}) | "
                         f"{_fmt(strength.get('beta_ridge'))} | {_fmt(strength.get('beta_se'))} | "
                         f"{_fmt(strength.get('excess_amt_weighted'))} | {_fmt(strength.get('excess_median'))} | "
                         f"{_fmt(strength.get('excess_trimmed'))} | {_fmt(strength.get('conserved_share'))} | "
                         f"{_fmt(strength.get('baseline_share'))} | {_fmt(strength.get('share_vs_baseline'))} | "
                         f"{_fmt(strength.get('imbalance'))} | {_fmt(strength.get('top1_amount_share'))} | "
                         f"{_fmt(strength.get('dominant_symbol'))} | {_fmt(strength.get('strength_rank'))} | "
                         f"{','.join(strength.get('flags', ())) or '—'} | {prev} | {live} | "
                         f"{_fmt(stage.get('hot_live_captured_at'))} | {_fmt(stage.get('hot_live_staleness_s'))} |")
    lines.extend(["", "### Beta 与当日热板 Top20 一致性", "",
                  "| 时点 | 交集数 | Spearman |", "|---|---:|---:|"])
    for tag, row in result.get("strength_hot_consistency", {}).items():
        lines.append(f"| {tag} | {row['intersection_count']} | {_fmt(row['spearman'])} |")
    lines.extend(["", "## 昨日涨停池成员", "", "| 代码 | 名称 | 连板天数 | 0925 匹配额 | 0925 涨幅 | 0932 成交额 | 0932 涨幅 |",
                  "|---|---|---:|---:|---:|---:|---:|"])
    for row in pool_rows:
        a25, o32 = row["times"]["0925"], row["times"]["0932"]
        lines.append(f"| {row['symbol']} | {row['name'] or ''} | {_fmt(row['lb_days'])} | "
                     f"{_fmt(a25['amount'])} | {_pctfmt(a25['pct_vs_pre_close'])} | "
                     f"{_fmt(o32['amount'])} | {_pctfmt(o32['pct_vs_pre_close'])} |")
    lines.extend(["", "## 数据质量与降级标签", "", f"- 竞价金额为虚拟匹配额；09:32 金额为真实累计成交额。",
                  f"- 昨日池成员数: {len(pool_rows)}。", f"- 降级标签: {', '.join(result['degradation']) or 'NONE'}。",
                  "- 缺失值保持 null；多板块统计按成员并集去重。", ""])
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    return "—" if value is None else str(value)


def _pctfmt(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


__all__ = ["build_session_report", "render_report_markdown"]
