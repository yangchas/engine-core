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


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _trimmed_mean(values: list[float], fraction: float = 0.1) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    trim = int(len(ordered) * fraction)
    kept = ordered[trim:len(ordered) - trim] if trim else ordered
    return sum(kept) / len(kept)


def _robust_scale(values: list[float]) -> tuple[float | None, float | None]:
    center = _median(values)
    deviations = [abs(value - center) for value in values] if center is not None else []
    mad = _median(deviations)
    return center, (1.4826 * mad if mad is not None and mad > 0 else None)


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
    min_members: int = 10,
    min_share: float = 0.003,
) -> dict[str, Any]:
    if min_members < 0 or min_share < 0:
        raise ValueError("candidate thresholds must be non-negative")
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
                              "price_source": (raw or {}).get("price_source"),
                              "price_flags": list((raw or {}).get("price_flags", ())),
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
    aggregation_rows = {tag: {row.plate: row for row in aggregation.stats}
                        for tag, aggregation in aggregations.items()}
    market_return_stats = {}
    market_amounts = {}
    for tag, stage_facts in facts_by_stage.items():
        returns = [_pct(fact.price, fact.pre_close) for fact in stage_facts.values()
                   if fact.price is not None and fact.pre_close is not None and fact.pre_close > 0]
        market_return_stats[tag] = _robust_scale([value for value in returns if value is not None])
    from .plate_rules import normalize_plate_name
    prior_days = {}
    for tag, days in (baseline_auction_by_tag or {}).items():
        eligible = [row for row in days if isinstance(row.get("trade_date"), str)
                    and row["trade_date"] < trade_date]
        prior_days[tag] = sorted(eligible, key=lambda row: row["trade_date"])[-5:]

    def same_day(value: Any) -> bool:
        if isinstance(value, (list, tuple)) and value:  # Kaipan getHisPlates returns Day as ["YYYY-MM-DD"]
            value = value[0]
        return isinstance(value, str) and value.replace("-", "")[:8] == trade_date.replace("-", "")

    facts_by_stage_value = {
        tag: {row["symbol"]: row["times"][tag] for row in stocks} for tag in facts_by_stage
    }
    plate_members = {stat.plate: set(membership_index.members_of(stat.plate))
                     for stat in aggregations["0925"].stats}
    member_degree: dict[str, int] = {}
    for members in plate_members.values():
        for symbol in members:
            member_degree[symbol] = member_degree.get(symbol, 0) + 1
    for tag, stage_facts in facts_by_stage.items():
        market_amounts[tag] = sum(float(stage_facts[symbol].amount) for symbol in member_degree
                                  if symbol in stage_facts and stage_facts[symbol].amount is not None
                                  and stage_facts[symbol].amount >= 0)
    cutoff_by_plate_tag: dict[str, dict[str, str]] = {}
    for stock in stocks:
        for membership in memberships.by_symbol.get(stock["symbol"], ()):
            per_tag = cutoff_by_plate_tag.setdefault(membership.plate, {})
            for tag in tags:
                per_tag[tag] = max(per_tag.get(tag, default_cutoffs[tag]),
                                   stock["times"][tag]["input_cutoff"])
    baseline_share_by_tag: dict[str, dict[str, float]] = {}
    baseline_sample_count_by_tag: dict[str, dict[str, int]] = {}
    baseline_manifest: dict[str, Any] = {}
    for tag in tags:
        baseline_tag = "0925" if tag == "0925R" else tag
        days = prior_days.get(tag, prior_days.get(baseline_tag, ()))
        day_shares: dict[str, list[float]] = {}
        for prior in days:
            amounts = prior.get("amount_by_symbol", {})
            allocated_total = sum(float(value) for symbol, value in amounts.items()
                                  if _number(value) is not None and float(value) >= 0
                                  and member_degree.get(str(symbol), 0) > 0)
            if allocated_total <= 0:
                continue
            for plate, members in plate_members.items():
                allocated = sum(float(amounts[str(symbol)]) / member_degree[str(symbol)]
                                for symbol in members if str(symbol) in amounts
                                and _number(amounts[str(symbol)]) is not None
                                and float(amounts[str(symbol)]) >= 0)
                day_shares.setdefault(plate, []).append(allocated / allocated_total)
        baseline_share_by_tag[tag] = {plate: _median(values) for plate, values in day_shares.items()
                                      if values and _median(values) is not None}
        baseline_sample_count_by_tag[tag] = {plate: len(values) for plate, values in day_shares.items()}
        short_count = sum(len(day_shares.get(plate, ())) < 3 for plate in plate_members)
        baseline_manifest[tag] = {"baseline_dates": [row["trade_date"] for row in days],
                                  "baseline_days": len(days),
                                  "baseline_short_plates": short_count}
    plates: list[dict[str, Any]] = []
    for stat in aggregations["0925"].stats:
        plate = stat.plate
        stage_rows = {}
        for tag in tags:
            stage_stat = aggregation_rows[tag].get(plate)
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
                "input_cutoff": cutoff_by_plate_tag.get(plate, {}).get(tag, default_cutoffs[tag]),
            }
            members = plate_members.get(plate, set())
            stage_facts = facts_by_stage[tag]
            valid_returns = [_pct(stage_facts[symbol].price, stage_facts[symbol].pre_close)
                             for symbol in members if symbol in stage_facts
                             and stage_facts[symbol].price is not None
                             and stage_facts[symbol].pre_close is not None
                             and stage_facts[symbol].pre_close > 0]
            valid_returns = [value for value in valid_returns if value is not None]
            market_center, market_scale = market_return_stats[tag]
            trimmed = _trimmed_mean(valid_returns)
            relative = trimmed - market_center if trimmed is not None and market_center is not None else None
            ratio = (relative / market_scale if relative is not None and market_scale else None)
            live_amounts = {symbol: stage_facts[symbol].amount for symbol in members
                            if symbol in stage_facts and stage_facts[symbol].amount is not None
                            and stage_facts[symbol].amount >= 0}
            allocated_amount = sum(value / member_degree[symbol] for symbol, value in live_amounts.items()
                                   if member_degree.get(symbol, 0) > 0)
            market_amount = market_amounts[tag]
            amount_share = allocated_amount / market_amount if market_amount and market_amount > 0 else None
            baseline_share = baseline_share_by_tag.get(tag, {}).get(plate)
            baseline_multiple = (amount_share / baseline_share if amount_share is not None
                                 and baseline_share is not None and baseline_share > 0 else None)
            activity_flags = []
            valid_baseline_days = baseline_sample_count_by_tag.get(tag, {}).get(plate, 0)
            if valid_baseline_days < 3 or baseline_share is None or baseline_share <= 0:
                activity_flags.append("BASELINE_SHORT")
            if stage_rows[tag] is not None:
                stage_rows[tag]["activity"] = {
                    "amount_kind": "MATCH" if tag in _AUCTION_TAGS else "TRADED",
                    "amount_sum": allocated_amount if live_amounts else None,
                    "amount_share_of_market": amount_share,
                    "baseline_share": baseline_share,
                    "baseline_multiple": baseline_multiple,
                    "baseline_days": valid_baseline_days,
                    "flags": activity_flags,
                }
                stage_rows[tag]["performance"] = {
                    "relative_market_z": ratio,
                    "trimmed_mean_pct": trimmed,
                    "relative_market_pp": relative,
                    "breadth_up_share": (sum(value > 0 for value in valid_returns) / len(valid_returns)
                                         if valid_returns else None),
                    "covered_count": len(valid_returns),
                    "touch_limit_price_count": sum(
                        _rounded_limit_price(facts_by_stage[symbol].pre_close, symbol, direction) ==
                        Decimal(str(facts_by_stage[symbol].price)).quantize(Decimal("0.01"))
                        for symbol in members if symbol in facts_by_stage and
                        facts_by_stage[symbol].price is not None and facts_by_stage[symbol].pre_close
                        for direction in (-1, 1)),
                    "sealed_limit_count": (stage_stat.limit_up_count if tag == "0925" else None),
                }
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
            if stage_rows[tag] is not None:
                stage_rows[tag].update({
                "hot_prev_close_rank": hot_prev.get("rank") if hot_prev else None,
                "hot_prev_close_strength": hot_prev.get("strength") if hot_prev else None,
                "hot_prev_close_pct": hot_prev.get("change_pct", hot_prev.get("pct")) if hot_prev else None,
                "hot_live_rank": (hot_row.get("rank") if hot_row else ">50" if capture else None),
                "hot_live_strength": hot_row.get("strength") if hot_row else None,
                "hot_live_pct": hot_row.get("change_pct", hot_row.get("pct")) if hot_row else None,
                "hot_live_captured_at": capture.isoformat() if capture else None,
                "hot_live_staleness_s": (stage_cutoff - capture).total_seconds()
                if capture and stage_cutoff else None,
                })
        core = sorted((symbol for symbol in membership_index.members_of(plate)
                       if facts_by_stage["0925"].get(symbol) is not None
                       and facts_by_stage["0925"][symbol].amount is not None),
                      key=lambda sym: (-(facts_by_stage["0925"].get(sym).amount or 0)
                                       if facts_by_stage["0925"].get(sym) else 0, sym))[:3]
        priced_members = (aggregation_rows["0925"].get(plate).covered_count
                          if aggregation_rows["0925"].get(plate) else 0)
        share = (stage_rows["0925"] or {}).get("amount_share_of_market")
        quality_flags = (["SMALL_OR_THIN"] if priced_members < min_members or share is None or
                         share < min_share else [])
        plates.append({
            "plate": plate, "kind": stat.kind,
            "category": "GENERIC_FACTOR" if stat.kind == "GENERIC" else stat.kind,
            "generic": stat.generic,
            "member_count": stat.member_count, "priced_member_count": priced_members,
            "quality_flags": quality_flags, "times": stage_rows,
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
    plate_by_name = {row["plate"]: row for row in plates}
    for stock in stocks:
        ordered_memberships = sorted(stock["plates"], key=lambda row: (row["tier"], row["rank"], row["plate"]))
        stock["plate_summary"] = [
            {"plate": member["plate"], "tier": member["tier"], "rank": member["rank"],
             "source": member["source"],
             "activity_by_stage": {
                 tag: {
                     "multiple": ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                                  .get(tag, {}).get("activity", {}).get("baseline_multiple")),
                     "z": ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                           .get(tag, {}).get("performance", {}).get("relative_market_z")),
                     "stock_amount_share": (
                         (stock["times"][tag].get("amount") / member_degree.get(stock["symbol"], 1)) /
                         ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                          .get(tag, {}).get("activity", {}).get("amount_sum") or 1)
                         if stock["times"][tag].get("amount") is not None and
                         ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                          .get(tag, {}).get("activity", {}).get("amount_sum") or 0) > 0 else None),
                 }
                 for tag in tags
             },
             "activity_multiple_0925": ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                                        .get("0925", {}).get("activity", {}).get("baseline_multiple")),
             "performance_z_0925": ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                                    .get("0925", {}).get("performance", {}).get("relative_market_z")),
             "stock_amount_share_0925": ((stock["times"]["0925"].get("amount") or 0) /
                                          member_degree.get(stock["symbol"], 1) /
                                          ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                                           .get("0925", {}).get("activity", {}).get("amount_sum") or 1)
                                          if stock["times"]["0925"].get("amount") is not None and
                                          ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                                           .get("0925", {}).get("activity", {}).get("amount_sum") or 0) > 0 else None),
             "hot_live_rank_0925": ((plate_by_name.get(member["plate"]) or {}).get("times", {})
                                    .get("0925", {}).get("hot_live_rank")),
            }
            for member in ordered_memberships[:3]
        ]
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
    result = {"trade_date": trade_date, "stocks": stocks, "plates": plates, "market": market,
              "input_cutoff": input_cutoffs,
              "activity_manifest": baseline_manifest,
              "candidate_thresholds": {"min_members": min_members, "min_share": min_share},
              "degradation": list(degradation), "memberships_policy": memberships.policy}
    result["auction_sections"] = _build_auction_sections(result)
    result["report_md"] = render_report_markdown(result)
    return result


def _delta(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> float | None:
    return _delta_value((right or {}).get("amount_sum"), (left or {}).get("amount_sum"))


def _delta_value(left: float | None, right: float | None) -> float | None:
    return right - left if left is not None and right is not None else None


def _build_auction_sections(result: Mapping[str, Any]) -> dict[str, Any]:
    """Build point-in-time auction boards from the already cut stock facts."""
    tags = ("0920", "0924", "0925")
    stocks = list(result.get("stocks", ()))
    plates = list(result.get("plates", ()))
    market_amount_values = {tag: [row["times"][tag]["amount"] for row in stocks
                                  if row.get("times", {}).get(tag, {}).get("amount") is not None
                                  and row["times"][tag]["amount"] >= 0] for tag in tags}
    market_amount = {tag: sum(values) if values else None
                     for tag, values in market_amount_values.items()}
    market_returns: dict[str, list[float]] = {tag: [] for tag in tags}
    for stock in stocks:
        for tag in tags:
            value = stock.get("times", {}).get(tag, {}).get("pct_vs_pre_close")
            if value is not None:
                market_returns[tag].append(value)
    market_centers = {tag: _median(values) for tag, values in market_returns.items()}
    degree: dict[str, int] = {}
    members_by_plate: dict[str, set[str]] = {}
    for stock in stocks:
        symbol = str(stock.get("symbol", ""))
        for membership in stock.get("plates", ()):
            name = str(membership.get("plate", ""))
            if name:
                members_by_plate.setdefault(name, set()).add(symbol)
                degree[symbol] = degree.get(symbol, 0) + 1
    plate_info = {str(row.get("plate")): row for row in plates}
    prior_tag: dict[str, str | None] = {"0920": None, "0924": "0920", "0925": "0924"}
    out: dict[str, Any] = {}
    for tag in tags:
        candidates: list[dict[str, Any]] = []
        for plate_name, members in members_by_plate.items():
            info = plate_info.get(plate_name, {})
            if info.get("category", info.get("kind")) not in ("THEME", "NICHE", "BROAD"):
                continue
            valid_rows = [row for row in stocks if str(row.get("symbol")) in members
                          and row.get("times", {}).get(tag, {}).get("pct_vs_pre_close") is not None]
            if len(valid_rows) < 10:
                continue
            eligible = [row for row in stocks if str(row.get("symbol")) in members]
            amount_values = [row["times"][tag]["amount"] / degree[str(row["symbol"])]
                             for row in eligible if degree.get(str(row["symbol"]), 0)
                             and row.get("times", {}).get(tag, {}).get("amount") is not None
                             and row["times"][tag]["amount"] >= 0]
            amount = sum(amount_values) if amount_values else None
            share = amount / market_amount[tag] if amount is not None and market_amount[tag] else None
            returns = [row["times"][tag]["pct_vs_pre_close"] for row in valid_rows]
            trimmed = _trimmed_mean(returns)
            pressure_rows = [row for row in eligible
                             if row.get("times", {}).get(tag, {}).get("rest_bid") is not None
                             and row["times"][tag].get("rest_ask") is not None]
            rest_bid = sum(row["times"][tag]["rest_bid"] / degree[str(row["symbol"])]
                           for row in pressure_rows if degree.get(str(row["symbol"]), 0))
            rest_ask = sum(row["times"][tag]["rest_ask"] / degree[str(row["symbol"])]
                           for row in pressure_rows if degree.get(str(row["symbol"]), 0))
            touch_count = sum(
                Decimal(str(row["times"][tag]["price"])).quantize(Decimal("0.01")) ==
                _rounded_limit_price(row.get("pre_close"), str(row.get("symbol")), 1)
                for row in valid_rows if row["times"][tag].get("price") is not None
                and row.get("pre_close") is not None and row["pre_close"] > 0)
            previous = None
            old_tag = prior_tag[tag]
            if old_tag:
                previous = next((item for item in out.get(old_tag, {}).get("all", ())
                                 if item["plate"] == plate_name), None)
            previous_priced_count = previous.get("priced_member_count") if previous else None
            previous_usable = previous is not None and previous_priced_count >= 5
            old_amount = previous.get("match_amount_yuan") if previous_usable else None
            old_pct = previous.get("trimmed_mean_pct") if previous_usable else None
            candidates.append({
                "plate": plate_name, "category": info.get("category", info.get("kind")),
                "match_amount_yuan": amount, "match_amount_share": share,
                "previous_amount_change_yuan": (amount - old_amount
                                                 if amount is not None and old_amount is not None else None),
                "previous_amount_change_pct": ((amount / old_amount - 1) * 100
                                                if amount is not None and old_amount is not None
                                                and old_amount > 0 else None),
                "trimmed_mean_pct": trimmed,
                "relative_market_pp": (trimmed - market_centers[tag]
                                        if trimmed is not None and market_centers[tag] is not None else None),
                "previous_pct_change_pp": (trimmed - old_pct
                                           if trimmed is not None and old_pct is not None else None),
                "limit_up_count": touch_count if tag == "0925" else None,
                "unmatched_buy_yuan": rest_bid if pressure_rows else None,
                "unmatched_sell_yuan": rest_ask if pressure_rows else None,
                "unmatched_buy_sell_diff_yuan": rest_bid - rest_ask if pressure_rows else None,
                "price_coverage_pct": len(valid_rows) / len(members) if members else None,
                "priced_member_count": len(valid_rows), "member_count": len(members),
                "previous_priced_member_count": previous_priced_count,
            })
        candidates.sort(key=lambda row: (-(row["match_amount_share"] or 0), row["plate"]))
        main = candidates[:20]
        threshold_rows = [row for row in candidates if row["match_amount_share"] is not None
                          and row["match_amount_share"] >= 0.002]
        gainers = sorted((row for row in threshold_rows if row["trimmed_mean_pct"] is not None),
                         key=lambda row: (-row["trimmed_mean_pct"], row["plate"]))[:10]
        losers = sorted((row for row in threshold_rows if row["trimmed_mean_pct"] is not None),
                        key=lambda row: (row["trimmed_mean_pct"], row["plate"]))[:10]
        stock_rows = []
        all_plate_amounts = {
            plate: sum(row["times"][tag]["amount"] / degree[str(row["symbol"])]
                       for row in stocks if str(row.get("symbol")) in members
                       and degree.get(str(row.get("symbol")), 0)
                       and row.get("times", {}).get(tag, {}).get("amount") is not None
                       and row["times"][tag]["amount"] >= 0)
            for plate, members in members_by_plate.items()
        }
        for stock in stocks:
            value = stock.get("times", {}).get(tag, {})
            amount = value.get("amount")
            if amount is None or amount < 0:
                continue
            memberships = stock.get("plates", ())
            primary = stock.get("primary_plate")
            allocated_plate_amount = all_plate_amounts.get(primary)
            symbol_degree = degree.get(str(stock.get("symbol")), 0)
            contribution = amount / symbol_degree if symbol_degree else None
            stock_share = (contribution / allocated_plate_amount * 100
                           if contribution is not None and allocated_plate_amount else None)
            price_source = value.get("price_source")
            price_flags = value.get("price_flags", value.get("flags", ()))
            price_kind = ("PRE_FINAL_INDICATIVE" if "PRE_FINAL_INDICATIVE" in price_flags else
                          price_source if price_source in ("SNAPSHOT_PX", "TICK_PX_FINAL",
                                                          "TICK_BOOK1_INDICATIVE") else None)
            stock_rows.append({
                "symbol": stock.get("symbol"), "name": stock.get("name"), "price_kind": price_kind,
                "pct": value.get("pct_vs_pre_close"), "match_amount_yuan": amount,
                "plate_amount_share_pct": stock_share, "primary_plate": primary,
                "rest_bid_yuan": value.get("rest_bid"), "rest_ask_yuan": value.get("rest_ask"),
                "touch_limit_price": (Decimal(str(value["price"])).quantize(Decimal("0.01")) ==
                                      _rounded_limit_price(stock.get("pre_close"), str(stock.get("symbol")), 1)
                                      if value.get("price") is not None and stock.get("pre_close") else None),
                "sealed_limit": value.get("limit_state") == 1 if tag == "0925" and value.get("limit_state") is not None else None,
            })
        stock_rows.sort(key=lambda row: (-row["match_amount_yuan"], str(row["symbol"])))
        out[tag] = {"main": main, "gainers": gainers, "losers": losers,
                    "stocks": stock_rows[:30], "all": candidates,
                    "final_price_count": sum(row.get("times", {}).get(tag, {}).get("price_source")
                                              in ("SNAPSHOT_PX", "TICK_PX_FINAL") for row in stocks)
                    if tag == "0925" else None,
                    "indicative_price_count": sum("INDICATIVE" in str(row.get("times", {}).get(tag, {}).get("price_source"))
                                                  or "PRE_FINAL_INDICATIVE" in row.get("times", {}).get(tag, {}).get("price_flags", ())
                                                  for row in stocks) if tag == "0925" else None}
    return out


def render_report_markdown(result: Mapping[str, Any]) -> str:
    auction_sections = _build_auction_sections(result)
    if isinstance(result, dict):
        result["auction_sections"] = auction_sections
    trade_date = result["trade_date"]
    market = result["market"]
    stage_tags = tuple(market.keys())
    report_plate_by_name = {row["plate"]: row for row in result["plates"]}
    pool_rows = [row for row in result["stocks"] if row["in_yest_pool"]]
    labels = list(result.get("degradation", ()))
    label_meanings = {
        "NOT_POINT_IN_TIME": "竞价快照点时性未核验",
        "STATIC_ASOF_UNKNOWN": "静态关系 CSV 为旧快照且时点未知",
        "PARTIAL": "输入覆盖不完整",
        "UNKNOWN": "来源或语义未知",
    }
    prominent_labels = [label for label in label_meanings if label in labels]
    label_text = " / ".join(f"{label}（{label_meanings[label]}）" for label in prominent_labels)
    lines = ([f"标签: {label_text}", ""] if prominent_labels else [])
    lines.extend([f"# 个股板块竞价开盘情况 {trade_date}", "",
             "本报告含义：独立板块=统计显著且去重的相对市场偏强/偏弱（非预测）；规模榜=资金体量；活跃度=相对 5 日基线的放量；事件条=事实。", "",
             "## 表现 × 活跃度摘要", "",
             "放量条件：基线倍数≥1.5、|相对市场稳健 z|≥1.5；只列事实，不计算综合分。"])
    final_match = market.get("0925", {}).get("amount_sum")
    lines.extend(["", "## 竞价金额进度与市场摘要", "",
                  "| 标签 | 总匹配额(亿) | 有价覆盖数 | 涨/跌/平 | 触及涨停价数 | 累计进度(占0925) |",
                  "|---|---:|---:|---:|---:|---:|"])
    for tag in ("0920", "0924", "0925"):
        if tag not in market:
            continue
        row = market[tag]
        prices = [stock for stock in result.get("stocks", ())
                  if stock.get("times", {}).get(tag, {}).get("pct_vs_pre_close") is not None]
        touched = sum(
            stock.get("times", {}).get(tag, {}).get("price") is not None
            and stock.get("pre_close") is not None
            and Decimal(str(stock["times"][tag]["price"])).quantize(Decimal("0.01")) ==
                _rounded_limit_price(stock["pre_close"], str(stock["symbol"]), 1)
            for stock in result.get("stocks", ()))
        progress = (row.get("amount_sum") / final_match * 100
                    if final_match is not None and final_match > 0 and row.get("amount_sum") is not None else None)
        lines.append(f"| {tag} | {_amount_yi(row.get('amount_sum'))} | {len(prices)} | "
                     f"{row.get('up_count', '—')}/{row.get('down_count', '—')}/{row.get('flat_count', '—')} | "
                     f"{touched if tag == '0925' else '—'} | {_pctfmt(progress / 100 if progress is not None else None)} |")
    final_indicator = (f"0925: 终价 {auction_sections['0925']['final_price_count']} / 指示价 "
                       f"{auction_sections['0925']['indicative_price_count']}")
    lines.insert(3, final_indicator)
    lines.extend(["", "额累计进度是当前标签匹配额占 0925 终额（0920→0925）；参考历史 0920/0925 中位约 16%。", "",
                  "## 竞价板块主表", "",
                  "竞价事实: 虚拟撮合阶段无成交量/成交额；09:20 后不可撤单；匹配额=min(买一量,卖一量)×一档价；09:25 后才是真实成交。"])
    full_headers = ("板块", "匹配额(亿)", "占市场比%", "较上一标签额变化(亿,%)", "板块涨幅(截尾均值%)",
                    "相对市场pp", "较上一标签涨幅变化pp", "触及涨停价数", "未匹配买(亿)",
                    "未匹配卖(亿)", "买卖差(亿)", "有价覆盖%")
    for tag in ("0920", "0924", "0925"):
        if tag not in auction_sections:
            continue
        lines.extend(["", f"### {tag}", "", "| " + " | ".join(full_headers) + " |",
                      "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for row in auction_sections[tag]["main"]:
            delta = (f"{_amount_yi(row.get('previous_amount_change_yuan'))}, "
                     f"{_fmt(row.get('previous_amount_change_pct')) + '%' if row.get('previous_amount_change_pct') is not None else '—'}"
                     if row.get("previous_amount_change_yuan") is not None else "—")
            lines.append("| " + " | ".join((
                str(row["plate"]), _amount_yi(row.get("match_amount_yuan")),
                _pctfmt(row.get("match_amount_share")), delta,
                _pctfmt(row.get("trimmed_mean_pct")), _ppfmt(row.get("relative_market_pp")),
                _ppfmt(row.get("previous_pct_change_pp")), _fmt(row.get("limit_up_count")),
                _amount_yi(row.get("unmatched_buy_yuan")),
                _amount_yi(row.get("unmatched_sell_yuan")),
                _amount_yi(row.get("unmatched_buy_sell_diff_yuan")),
                _pctfmt(row.get("price_coverage_pct")),
            )) + " |")
        lines.append("\n较上一标签变化仅在上一标签有价成员不少于 5 时显示；若上一标签有价成员少于 5 只，标为 —。")
        if tag == "0925":
            lines.append(f"\n终价 {auction_sections[tag]['final_price_count']} / 指示价 "
                         f"{auction_sections[tag]['indicative_price_count']}。")
        for title, key in (("涨幅前 10", "gainers"), ("跌幅前 10", "losers")):
            lines.extend(["", f"#### {tag} {title}", "",
                          "| 板块 | 匹配额(亿) | 占市场比% | 涨幅(截尾均值%) | 相对市场pp | 涨停数 | 有价覆盖% |",
                          "|---|---:|---:|---:|---:|---:|---:|"])
            for row in auction_sections[tag][key]:
                lines.append(f"| {row['plate']} | {_amount_yi(row.get('match_amount_yuan'))} | "
                             f"{_pctfmt(row.get('match_amount_share'))} | {_pctfmt(row.get('trimmed_mean_pct'))} | "
                             f"{_ppfmt(row.get('relative_market_pp'))} | {_fmt(row.get('limit_up_count'))} | "
                             f"{_pctfmt(row.get('price_coverage_pct'))} |")
        lines.extend(["", f"#### 竞价个股榜 {tag}", "",
                      "| 代码 | 名称 | 价格口径 | 涨幅% | 匹配额(亿) | 占板块额% | 主板块 | 未匹配买(亿) | 未匹配卖(亿) | 涨停/封单 |",
                      "|---|---|---|---:|---:|---:|---|---:|---:|---|"])
        for row in auction_sections[tag]["stocks"]:
            status = ("触及涨停" if row.get("touch_limit_price") else "—")
            if row.get("sealed_limit") is True:
                status += "/有封单"
            elif tag == "0925" and row.get("touch_limit_price") and row.get("sealed_limit") is False:
                status += "/无封单"
            lines.append(f"| {row.get('symbol')} | {row.get('name') or '—'} | {row.get('price_kind') or '—'} | "
                         f"{_pctfmt(row.get('pct'))} | {_amount_yi(row.get('match_amount_yuan'))} | "
                         f"{_pctfmt((row.get('plate_amount_share_pct') / 100) if row.get('plate_amount_share_pct') is not None else None)} | "
                         f"{row.get('primary_plate') or '—'} | {_amount_yi(row.get('rest_bid_yuan'))} | "
                         f"{_amount_yi(row.get('rest_ask_yuan'))} | {status} |")
    auction_summary_tag = "0925"
    opening_summary_tag = next((tag for tag in reversed(stage_tags)
                                if tag in ("0932", "0935", "0940", "0945")), None)
    summary_tags = [tag for tag in (auction_summary_tag,
                                    "0925R" if "0925R" in stage_tags else None,
                                    opening_summary_tag) if tag in stage_tags]
    for idx, tag in enumerate(summary_tags):
        pairs = [(plate, plate.get("times", {}).get(tag)) for plate in result["plates"]]
        pairs = [(plate, value) for plate, value in pairs if value and value.get("activity") and value.get("performance")]
        thresholds = result.get("candidate_thresholds", {"min_members": 10, "min_share": 0.003})
        independent = [(plate, value) for plate, value in pairs
                       if plate.get("category") in ("THEME", "NICHE", "BROAD")
                       and (value.get("performance", {}).get("covered_count") or 0) >= thresholds["min_members"]
                       and (value["activity"].get("amount_share_of_market") or 0) >= thresholds["min_share"]]
        surged = [(plate, value) for plate, value in independent
                  if "BASELINE_SHORT" not in value["activity"].get("flags", ())
                  if (value["activity"].get("baseline_multiple") or 0) >= 1.5
                  and abs(value["performance"].get("relative_market_z") or 0) >= 1.5]
        strong = sorted((pair for pair in surged if (pair[1]["performance"].get("relative_market_z") or 0) > 0),
                        key=lambda pair: -pair[1]["performance"]["relative_market_z"])[:3]
        weak = sorted((pair for pair in surged if (pair[1]["performance"].get("relative_market_z") or 0) < 0),
                      key=lambda pair: pair[1]["performance"]["relative_market_z"])[:3]
        active = sorted((pair for pair in pairs if pair[0].get("category") in ("THEME", "NICHE", "BROAD")
                         and (pair[0].get("priced_member_count") or 0) >= thresholds["min_members"]
                         and (pair[1]["activity"].get("amount_share_of_market") or 0) >= thresholds["min_share"]
                         and (pair[1].get("performance", {}).get("covered_count") or 0) >= thresholds["min_members"]
                         and pair[1]["activity"].get("baseline_multiple") is not None),
                        key=lambda pair: -pair[1]["activity"]["baseline_multiple"])[:5]
        def summary_names(items):
            return "、".join(f"{plate['plate']}(z={_zfmt(value['performance'].get('relative_market_z'))},×{value['activity']['baseline_multiple']:.2f})"
                            for plate, value in items) or "—"
        active_text = "、".join(f"{plate['plate']}×{value['activity']['baseline_multiple']:.2f}"
                                f"({_pctfmt(value['activity'].get('amount_share_of_market'))})"
                                for plate, value in active) or "—"
        scale_top = sorted(independent, key=lambda pair: (
            -(pair[1]["activity"].get("amount_share_of_market") or 0), pair[0]["plate"]))[:5]
        scale_text = "、".join(
            f"{plate['plate']}({_pctfmt(value['activity'].get('amount_share_of_market'))})"
            for plate, value in scale_top) or "—"
        old_tag = ("0925" if tag == "0925R" else
                   stage_tags[stage_tags.index(tag) - 1] if stage_tags.index(tag) else None)
        prior = {plate["plate"]: plate.get("times", {}).get(old_tag) for plate in result["plates"]} if old_tag else {}
        changes = []
        for plate, value in pairs:
            if (plate.get("category") not in ("THEME", "NICHE", "BROAD")
                    or (plate.get("priced_member_count") or 0) < thresholds["min_members"]
                    or (value.get("performance", {}).get("covered_count") or 0) < thresholds["min_members"]
                    or (value.get("activity", {}).get("amount_share_of_market") or 0) < thresholds["min_share"]):
                continue
            before = (prior.get(plate["plate"]) or {}).get("activity", {}).get("amount_share_of_market")
            after = value["activity"].get("amount_share_of_market")
            if before is not None and after is not None:
                changes.append((abs(after - before), plate["plate"], after - before))
        change_text = "、".join(f"{name}{delta * 100:+.2f}pp" for _, name, delta in sorted(changes, reverse=True)[:3]) or "—"
        event_rows = result.get("independent_by_tag", {}).get(tag, {}).get("event_plates", ())
        event_top = sorted(event_rows, key=lambda row: (
            -(row.get("touch_limit_price_count") or 0), row.get("name", "")))[:3]
        event_text = "、".join(
            f"{row.get('name')}({row.get('touch_limit_price_count', 0)}只,截尾均值{_ppfmt(row.get('trim_mean'))})"
            for row in event_top) or "—"
        summary = (f"{tag}: 放量偏强 Top3 {summary_names(strong)}；放量偏弱 Top3 {summary_names(weak)}；"
                   f"规模前 5 {scale_text}；最活跃 Top5 {active_text}；较{old_tag or '—'}占比变化 Top3 {change_text}；"
                   f"事件板块触及涨停价数最多 Top3 {event_text}（事件类, 仅事实）")
        if tag in ("0925", "0925R"):
            price_tag = tag if tag == "0925R" else "0925"
            sources = [row.get("times", {}).get(price_tag, {}).get("price_source") for row in result["stocks"]]
            final_n = sum(value in ("SNAPSHOT_PX", "TICK_PX_FINAL") for value in sources)
            indicative_n = sum("INDICATIVE" in str(value) for value in sources)
            summary += f"；终价 {final_n} / 指示价 {indicative_n}"
            if tag == "0925" and result.get("no_late_finals"):
                summary += "；无迟到终价, 0925R 与 0925 相同"
        lines.append(summary)
    lines.extend(["", "## 阅读说明", "",
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
    lines.extend(["", "## 成交额占比榜（规模）", ""])
    for tag in stage_tags:
        thresholds = result.get("candidate_thresholds", {"min_members": 10, "min_share": 0.003})
        scale_rows = []
        selected_names = {str(item.get("name")) for item in
                          result.get("independent_by_tag", {}).get(tag, {}).get("selected", ())}
        for plate in result["plates"]:
            stage = plate.get("times", {}).get(tag)
            if not stage or not stage.get("activity") or not stage.get("performance"):
                continue
            activity, performance = stage["activity"], stage["performance"]
            if (plate.get("category") in ("THEME", "NICHE", "BROAD")
                    and (plate.get("priced_member_count") or 0) >= thresholds["min_members"]
                    and (performance.get("covered_count") or 0) >= thresholds["min_members"]
                    and (activity.get("amount_share_of_market") or 0) >= thresholds["min_share"]):
                scale_rows.append((plate, stage))
        scale_rows.sort(key=lambda pair: (-(pair[1]["activity"].get("amount_share_of_market") or 0),
                                          -(pair[1]["activity"].get("amount_sum") or 0), pair[0]["plate"]))
        lines.extend([f"### {tag}", "",
                      "| 板块 | 类别 | 额(亿) | 占比 | 基线倍数 | 表现 z | 绝对均值% | 广度% | 涨停数/触及数 | 独立入选 | Kaipan 名次(参考) |",
                      "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|"])
        for plate, stage in scale_rows[:15]:
            activity, performance = stage["activity"], stage["performance"]
            limit_count = performance.get("sealed_limit_count")
            if limit_count is None:
                limit_count = performance.get("touch_limit_price_count")
            multiple = activity.get("baseline_multiple")
            multiple_text = f"×{multiple:.2f}" if multiple is not None else "—"
            lines.append(f"| {plate['plate']} | {plate['category']} | {_amount_yi(activity.get('amount_sum'))} | "
                         f"{_pctfmt(activity.get('amount_share_of_market'))} | {multiple_text} | "
                         f"{_zfmt(performance.get('relative_market_z'))} | "
                         f"{_ppfmt(performance.get('trimmed_mean_pct'))} | "
                         f"{_pctfmt(performance.get('breadth_up_share'))} | {_fmt(limit_count)} | "
                         f"{'是' if plate['plate'] in selected_names else '否'} | "
                         f"{_fmt(stage.get('hot_live_rank'))} |")
    lines.extend(["", "## 活跃度榜（与表现分开）", ""])
    for tag in stage_tags:
        thresholds = result.get("candidate_thresholds", {"min_members": 10, "min_share": 0.003})
        pairs = [(row, row.get("times", {}).get(tag)) for row in result["plates"]]
        ranked = [(row, value) for row, value in pairs if value and value.get("activity")
                  and row.get("category") in ("THEME", "NICHE", "BROAD")
                  and (row.get("priced_member_count") or 0) >= thresholds["min_members"]
                  and (value["activity"].get("amount_share_of_market") or 0) >= thresholds["min_share"]
                  and (value.get("performance", {}).get("covered_count") or 0) >= thresholds["min_members"]
                  and value["activity"].get("baseline_multiple") is not None]
        ranked.sort(key=lambda pair: -pair[1]["activity"]["baseline_multiple"])
        lines.extend([f"### {tag}", "", "| 板块 | 类别 | 额(亿) | 占比 | 基线倍数 | 表现z | 截尾均值% | 广度% | 涨停/触及 | 独立入选 | Kaipan名次 |",
                      "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|"])
        for row, stage in ranked[:15]:
            activity, performance = stage["activity"], stage["performance"]
            limit_count = performance.get("sealed_limit_count")
            if limit_count is None:
                limit_count = performance.get("touch_limit_price_count")
            selected_names = {str(item.get("name")) for item in
                              result.get("independent_by_tag", {}).get(tag, {}).get("selected", ())}
            selected = row["plate"] in selected_names
            category_text = row["category"] + (" / BASELINE_SHORT" if "BASELINE_SHORT" in activity.get("flags", ()) else "")
            lines.append(f"| {row['plate']} | {category_text} | {_amount_yi(activity.get('amount_sum'))} | "
                         f"{_pctfmt(activity.get('amount_share_of_market'))} | ×{activity['baseline_multiple']:.2f} | "
                         f"{_zfmt(performance.get('relative_market_z'))} | {_ppfmt(performance.get('trimmed_mean_pct'))} | "
                         f"{_pctfmt(performance.get('breadth_up_share'))} | {_fmt(limit_count)} | "
                         f"{'是' if selected else '否'} | {_fmt(stage.get('hot_live_rank'))} |")
        auxiliary = [(row, value) for row, value in pairs if value.get("activity")
                     and row.get("category") in ("EVENT", "GENERIC_FACTOR")
                     and (value["activity"].get("amount_share_of_market") or 0) >= 0.003
                     and (value.get("performance", {}).get("covered_count") or 0) >= 5
                     and value["activity"].get("baseline_multiple") is not None]
        auxiliary.sort(key=lambda pair: -pair[1]["activity"]["baseline_multiple"])
        lines.extend(["", f"#### 事件与泛化因子（{tag}）", "",
                      "| 板块 | 类别 | 额(亿) | 占比 | 基线倍数 | 表现z | 截尾均值% | 广度% | 涨停/触及 |",
                      "|---|---|---:|---:|---:|---:|---:|---:|---:|"])
        for row, stage in auxiliary[:15]:
            activity, performance = stage["activity"], stage["performance"]
            limit_count = performance.get("sealed_limit_count")
            if limit_count is None:
                limit_count = performance.get("touch_limit_price_count")
            lines.append(f"| {row['plate']} | {row['category']} | {_amount_yi(activity.get('amount_sum'))} | "
                         f"{_pctfmt(activity.get('amount_share_of_market'))} | ×{activity['baseline_multiple']:.2f} | "
                         f"{_zfmt(performance.get('relative_market_z'))} | {_ppfmt(performance.get('trimmed_mean_pct'))} | "
                         f"{_pctfmt(performance.get('breadth_up_share'))} | {_fmt(limit_count)} |")
    thin_rows = [row for row in result["plates"] if "SMALL_OR_THIN" in row.get("quality_flags", ())]
    if thin_rows:
        lines.extend(["", "## 小样本/薄额板块（附录，标记 SMALL_OR_THIN）", "",
                      "| 板块 | 类别 | 有价成员 | 市场额占比 |", "|---|---|---:|---:|"])
        for row in thin_rows:
            stage = row.get("times", {}).get("0925") or {}
            lines.append(f"| {row['plate']} | {row.get('category')} | {row.get('priced_member_count', 0)} | "
                         f"{_pctfmt(stage.get('amount_share_of_market'))} |")
    def stock_table(title: str, rows: list[Mapping[str, Any]], extra: str = "") -> None:
        headers = ["代码", "名称", "连板数", "主板块"]
        for stage_tag in stage_tags:
            kind = "MATCH_REVISED" if stage_tag == "0925R" else "MATCH" if stage_tag in _AUCTION_TAGS else "TRADED"
            headers.extend((f"{stage_tag}涨幅(pp)", f"{stage_tag} {kind}(亿元)"))
        headers.extend(("0925涨停有封单", "主板块来源", "主板块活动倍数/z/个股额占比"))
        if extra:
            headers.extend(extra.strip(" | ").split(" | "))
        lines.extend(["", f"## {title}", "",
                      "| " + " | ".join(headers) + " |",
                      "|" + "|".join("---" for _ in headers) + "|"])
        for row in rows[:40]:
            times = row["times"]
            primary_source = next((item.get("source") for item in row.get("plate_summary", ())), None)
            pieces = [row["symbol"], row.get("name") or "", _fmt(row.get("lb_days")),
                      row.get("primary_plate") or ""]
            for tag in stage_tags:
                fact = times[tag]
                pieces.extend((_ppfmt(fact.get("pct_vs_pre_close")), _amount_yi(fact.get("amount"))))
            a25 = times["0925"]
            seal_status = ("—" if a25.get("limit_state") is None else
                           "是" if a25.get("limit_state") == 1 and (a25.get("rest_bid") or 0) > 0 else "否")
            pieces.extend((seal_status, primary_source or "无",
                           "; ".join(f"{plate['plate']}[" + ", ".join(
                                         f"{tag}:×{_fmt(values['multiple'])}/z={_zfmt(values['z'])}/"
                                         f"个股额占比={_pctfmt(values['stock_amount_share'])}"
                                         for tag, values in plate.get("activity_by_stage", {}).items()) + "]"
                                     for plate in row.get("plate_summary", ())) or "—"))
            if extra == " | rest_bid(亿元) | rest_ask(亿元) | 封单额(亿元)":
                seal = a25.get("rest_bid") if a25.get("limit_state") == 1 else (
                    a25.get("rest_ask") if a25.get("limit_state") == -1 else None)
                pieces.extend((_amount_yi(a25.get("rest_bid")), _amount_yi(a25.get("rest_ask")),
                               _amount_yi(seal)))
            elif extra:
                primary = row.get("primary_plate")
                summary = next((item for item in row.get("plate_summary", ())
                                if item["plate"] == primary), {})
                pieces.extend((_fmt(summary.get("activity_multiple_0925")),
                               _zfmt(summary.get("performance_z_0925")),
                               _pctfmt(summary.get("stock_amount_share_0925"))))
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
    stock_table("0925 匹配额前 30 个股", amount_rows, " | 主板块活动倍数 | 主板块表现z | 个股额占板块比")
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
        event_top = sorted(result.get("event_plates", ()), key=lambda row: (
            -(row.get("touch_limit_price_count") or 0), row.get("name", "")))[:3]
        event_summary = "、".join(
            f"{row.get('name')}({_fmt(row.get('touch_limit_price_count'))}只,截尾均值{_ppfmt(row.get('trim_mean'))})"
            for row in event_top) or "—"
        lines.extend([f"### {tag}", "",
                      (f"撮合收集中, 覆盖 {market.get('n', 0)} 只(仅有有效撮合价者), 低置信度, 仅作事实。"
                       if tag in ("0920", "0924") else ""),
                      f"事件板块触及涨停价数最多 Top3: {event_summary}（事件类, 仅事实；触及涨停价不代表存在封单）",
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
            lines.extend(["事件类, 仅作事实。触及涨停价不代表存在封单。", "| 事件板块 | 成员有价数 | 截尾均值% | 触及涨停价数 | 封单涨停数(未知时为—) | 成员涨幅前3 |",
                          "|---|---:|---:|---:|---:|---|"])
            for row in result["event_plates"]:
                top = "、".join(f"{item['symbol']}({_ppfmt(item.get('ret'))})" for item in row.get("top3", ()))
                lines.append(f"| {row['name']} | {row['covered_count']} | {_ppfmt(row.get('trim_mean'))} | "
                             f"{_fmt(row.get('touch_limit_price_count'))} | {_fmt(row.get('sealed_limit_count'))} | {top or '—'} |")
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
