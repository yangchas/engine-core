"""Fact-only plate-to-stock views for auction and opening reports."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

CONTINUATION_TOLERANCE_PP = 0.3
OPENING_FOLD_PCT_CHANGE_PP = 1.0
OPENING_FOLD_RANK_CHANGE = 10
FOCUS_LIMIT_AUCTION = 30
FOCUS_LIMIT_OPEN = 20
_OPEN_TAGS = ("0932", "0935", "0940", "0945")


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _ranked(plates: Sequence[Mapping[str, Any]], stage: str, key: str) -> list[Mapping[str, Any]]:
    rows = []
    for plate in plates:
        value = _num((plate.get("times", {}).get(stage) or {}).get(key))
        if value is not None:
            rows.append((plate, value))
    return [row for row, _ in sorted(rows, key=lambda item: (-item[1], str(item[0].get("plate", ""))))]


def select_focus_plates(
    plates: Sequence[Mapping[str, Any]], stage: str, *,
    independent: Sequence[Mapping[str, Any]] = (), limit: int = FOCUS_LIMIT_AUCTION,
) -> list[dict[str, Any]]:
    """Select a stage-specific focus set, preserving every reason a plate qualified."""
    eligible = []
    for row in plates:
        values = row.get("times", {}).get(stage) or {}
        count = _num(values.get("effective_member_count"))
        if count is None or count < 5 or row.get("generic") or row.get("kind") == "GENERIC":
            continue
        eligible.append(row)
    by_name = {str(row.get("plate")): row for row in eligible}
    sources: dict[str, set[str]] = {}
    for item in independent:
        name = str(item.get("plate", item.get("name", "")))
        if name in by_name and str(item.get("direction", item.get("label", ""))).upper() in ("STRONG", "WEAK"):
            sources.setdefault(name, set()).add("独立")
    for key, label in (("pct_equal_weight", "涨幅"), ("amount_sum", "额"),
                       ("yest_pool_member_count", "昨板")):
        for row in _ranked(eligible, stage, key)[:10]:
            sources.setdefault(str(row["plate"]), set()).add(label)
    selected = [(by_name[name], tags) for name, tags in sources.items()]
    selected.sort(key=lambda item: (
        -(_num((item[0].get("times", {}).get(stage) or {}).get("pct_equal_weight"))
          if _num((item[0].get("times", {}).get(stage) or {}).get("pct_equal_weight")) is not None else float("-inf")),
        str(item[0].get("plate", "")),
    ))
    return [{"plate": str(row.get("plate")), "category": row.get("category", row.get("kind")),
             "sources": [tag for tag in ("独立", "涨幅", "额", "昨板") if tag in tags],
             "effective_member_count": (row.get("times", {}).get(stage) or {}).get("effective_member_count")}
            for row, tags in selected[:max(0, limit)]]


def continuation_state(auction_pp: float | None, final_pp: float | None) -> str | None:
    if auction_pp is None or final_pp is None:
        return None
    if auction_pp > 0 and final_pp <= 0 or auction_pp < 0 and final_pp >= 0:
        return "反转"
    delta = final_pp - auction_pp
    if delta >= -CONTINUATION_TOLERANCE_PP - 1e-12 and final_pp > 0:
        return "延续"
    if delta < -CONTINUATION_TOLERANCE_PP and final_pp > 0:
        return "转弱"
    return "反转"


def build_plate_stock_view(
    stocks: Sequence[Mapping[str, Any]], plates: Sequence[Mapping[str, Any]], *,
    independent_by_tag: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    plate_by_name = {str(row.get("plate")): row for row in plates}
    independent_by_tag = independent_by_tag or {}

    def focuses(stage: str, limit: int) -> list[dict[str, Any]]:
        return select_focus_plates(plates, stage,
                                   independent=(independent_by_tag.get(stage) or {}).get("selected", ()),
                                   limit=limit)

    def members_for(plate_name: str, stage: str) -> list[dict[str, Any]]:
        members = [stock for stock in stocks if any(str(item.get("plate")) == plate_name
                                                    for item in stock.get("plates", ()))]
        by_amount = sorted((row for row in members
                            if _num((row.get("times", {}).get(stage) or {}).get("amount")) is not None),
                           key=lambda row: (-_num(row["times"][stage].get("amount")), str(row.get("symbol"))))
        by_pct = sorted((row for row in members
                         if _num((row.get("times", {}).get(stage) or {}).get("pct_vs_pre_close")) is not None),
                        key=lambda row: (-_num(row["times"][stage].get("pct_vs_pre_close")),
                                         str(row.get("symbol"))))
        display_symbols = {str(row["symbol"]) for row in by_amount[:8]}
        display_symbols.update(str(row["symbol"]) for row in by_pct[:3])
        ordered = sorted(members, key=lambda row: (
            _num((row.get("times", {}).get(stage) or {}).get("amount")) is None,
            -(_num((row.get("times", {}).get(stage) or {}).get("amount")) or 0), str(row.get("symbol"))))
        out = []
        for stock in ordered:
            current = stock.get("times", {}).get(stage) or {}
            a25 = stock.get("times", {}).get("0925") or {}
            scenario = stock.get("scenario") or {}
            final_flag = (scenario.get("auction") or {}).get("final_flag") or "NO_PRICE"
            pct = _num(current.get("pct_vs_pre_close"))
            pct_a25 = _num(a25.get("pct_vs_pre_close"))
            memberships = [str(item.get("plate")) for item in stock.get("plates", ())
                           if str(item.get("plate")) != plate_name]
            out.append({
                "symbol": str(stock.get("symbol")), "name": stock.get("name"), "pct": pct,
                "amount_yuan": _num(current.get("amount")), "matched_amount_yuan": _num(a25.get("amount")),
                "rest_bid_yuan": _num(a25.get("rest_bid")), "rest_ask_yuan": _num(a25.get("rest_ask")),
                "in_yest_pool": bool(stock.get("in_yest_pool")), "lb_days": stock.get("lb_days"),
                "sealed_0925": a25.get("limit_state") == 1 and (_num(a25.get("rest_bid")) or 0) > 0,
                "price_flag": final_flag if stage == "0925" else
                              ("FINAL" if current.get("price") is not None else "NO_PRICE"),
                "also_in_plates": memberships[:3], "primary_plate": stock.get("primary_plate"),
                "change_vs_0925_pp": (pct - pct_a25) * 100 if pct is not None and pct_a25 is not None else None,
                "opened_limit": (a25.get("limit_state") == 1 and current.get("at_limit_price") is False)
                                if stage != "0925" else None,
                "newly_emerged": (pct_a25 is not None and pct is not None
                                   and pct_a25 * 100 < 3 and pct * 100 >= 5) if stage != "0925" else False,
                "display_member": str(stock.get("symbol")) in display_symbols,
            })
        return out

    def plate_rows(stage: str, limit: int) -> list[dict[str, Any]]:
        selected = focuses(stage, limit)
        pct_rank = {row["plate"]: i + 1 for i, row in enumerate(_ranked(plates, stage, "pct_equal_weight"))}
        auction_rank = {row["plate"]: i + 1 for i, row in enumerate(_ranked(plates, "0925", "pct_equal_weight"))}
        output = []
        for focus in selected:
            name = focus["plate"]
            plate = plate_by_name[name]
            metrics = plate.get("times", {}).get(stage) or {}
            auction = plate.get("times", {}).get("0925") or {}
            top1 = metrics.get("top1_concentration")
            member_amounts = [
                _num((stock.get("times", {}).get(stage) or {}).get("amount"))
                for stock in stocks
                if any(str(item.get("plate")) == name for item in stock.get("plates", ()))
            ]
            member_amounts = [value for value in member_amounts if value is not None and value >= 0]
            if _num(top1) is None and member_amounts and sum(member_amounts) > 0:
                top1 = max(member_amounts) / sum(member_amounts)
            output.append({**focus, "pct": metrics.get("pct_equal_weight"),
                           "pct_0920": (plate.get("times", {}).get("0920") or {}).get("pct_equal_weight"),
                           "pct_0924": (plate.get("times", {}).get("0924") or {}).get("pct_equal_weight"),
                           "amount_sum_yuan": metrics.get("amount_sum"),
                           "effective_member_count": metrics.get("effective_member_count"),
                           "limit_up_count": _opening_limit_up_count(
                               metrics.get("limit_up_count"), stocks, name, stage),
                           "yest_pool_member_count": metrics.get("yest_pool_member_count"),
                           "top1_concentration": top1,
                           "concentrated": _num(top1) is not None and top1 > 0.5,
                           "rank": pct_rank.get(name),
                           "change_vs_0925_pp": (
                               (_num(metrics.get("pct_equal_weight")) - _num(auction.get("pct_equal_weight"))) * 100
                               if _num(metrics.get("pct_equal_weight")) is not None
                               and _num(auction.get("pct_equal_weight")) is not None else None),
                           "rank_change": (auction_rank.get(name) - pct_rank.get(name)
                                           if name in auction_rank and name in pct_rank else None),
                           "members": members_for(name, stage)})
        return output

    auction_rows = plate_rows("0925", FOCUS_LIMIT_AUCTION)
    auction_focus_names = {row["plate"] for row in auction_rows}
    opening = {}
    emerging = {}
    manifest = {"focus_count_by_stage": {}, "source_count_by_stage": {}}
    for tag in ("0925", "0932", "0935", "0940", "0945"):
        limit = FOCUS_LIMIT_AUCTION if tag == "0925" else FOCUS_LIMIT_OPEN
        rows = auction_rows if tag == "0925" else plate_rows(tag, limit)
        if tag == "0925":
            continue
        opening[tag] = {"stage": tag, "plates": rows}
        manifest["focus_count_by_stage"][tag] = len(rows)
        manifest["source_count_by_stage"][tag] = dict(Counter(
            source for row in rows for source in row["sources"]))
        newly_focused = [row for row in rows if row["plate"] not in auction_focus_names]
        new_stocks = []
        independently_selected = {
            str(item.get("plate", item.get("name", "")))
            for item in (independent_by_tag.get(tag) or {}).get("selected", ())
        }
        for stock in stocks:
            name = str(stock.get("name") or "")
            if "ST" in name.upper():
                continue
            a25 = _num((stock.get("times", {}).get("0925") or {}).get("pct_vs_pre_close"))
            now = _num((stock.get("times", {}).get(tag) or {}).get("pct_vs_pre_close"))
            if a25 is None or now is None or a25 * 100 >= 3 or now * 100 < 5:
                continue
            new_stocks.append({"symbol": stock.get("symbol"), "name": stock.get("name"),
                               "auction_pct": a25,
                               "current_pct": now,
                               "current_amount_yuan": _num((stock.get("times", {}).get(tag) or {}).get("amount")),
                               "primary_plate": stock.get("primary_plate"),
                               "in_yest_pool": bool(stock.get("in_yest_pool")),
                               "in_independent_plate": any(
                                   str(item.get("plate")) in independently_selected
                                   for item in stock.get("plates", ()))})
        new_stocks.sort(key=lambda row: (row["current_amount_yuan"] is None,
                                        -(row["current_amount_yuan"] or 0), str(row["symbol"])))
        emerging[tag] = {"plates": newly_focused[:10], "stocks": new_stocks[:15]}
    manifest["focus_count_by_stage"]["0925"] = len(auction_rows)
    manifest["source_count_by_stage"]["0925"] = dict(Counter(
        source for row in auction_rows for source in row["sources"]))
    continuation = []
    for row in auction_rows[:15]:
        name = row["plate"]
        by_stage = {tag: _num((plate_by_name[name].get("times", {}).get(tag) or {}).get("pct_equal_weight"))
                    for tag in ("0925", "0932", "0945")}
        a25 = by_stage["0925"] * 100 if by_stage["0925"] is not None else None
        a45 = by_stage["0945"] * 100 if by_stage["0945"] is not None else None
        continuation.append({"plate": name, "pct_0925": by_stage["0925"],
                             "pct_0932": by_stage["0932"], "pct_0945": by_stage["0945"],
                             "state": continuation_state(a25, a45)})
    return {"auction": {"stage": "0925", "plates": auction_rows},
            "opening": opening, "emerging": emerging,
            "continuation": {"tolerance_pp": CONTINUATION_TOLERANCE_PP, "plates": continuation},
            "manifest": manifest,
            "notes": ["涨跌幅为成员等权；成员合计额按原始个股金额求和。",
                      "FINAL/INDICATIVE 按竞价来源标记；缺失不补 0。",
                      "昨日涨停池标记沿用既有 ST 排除规则；CONCENTRATED 与涨幅分档只用于显示。"]}


def _opening_limit_up_count(existing: Any, stocks: Sequence[Mapping[str, Any]],
                            plate_name: str, stage: str) -> Any:
    if _num(existing) is not None:
        return existing
    valid_flags = []
    key = f"at_limit_{stage}"
    for stock in stocks:
        if not any(str(item.get("plate")) == plate_name for item in stock.get("plates", ())):
            continue
        current = stock.get("times", {}).get(stage) or {}
        flag = (stock.get("scenario", {}).get("open") or {}).get(key)
        if current.get("pct_vs_pre_close") is not None and isinstance(flag, bool):
            valid_flags.append(flag)
    return sum(valid_flags) if valid_flags else None


def _significant_opening_plate(row: Mapping[str, Any], previous: Mapping[str, Any] | None) -> bool:
    if previous is None:
        return True
    current_pct = _num(row.get("pct"))
    previous_pct = _num(previous.get("pct"))
    if current_pct is not None and previous_pct is not None and abs(current_pct - previous_pct) * 100 >= OPENING_FOLD_PCT_CHANGE_PP:
        return True
    current_rank = _num(row.get("rank"))
    previous_rank = _num(previous.get("rank"))
    if current_rank is not None and previous_rank is not None and abs(current_rank - previous_rank) >= OPENING_FOLD_RANK_CHANGE:
        return True
    return any(member.get("opened_limit") or member.get("newly_emerged")
               for member in row.get("members", ()))


def render_plate_stock_view(view: Mapping[str, Any]) -> str:
    def pct(value: Any) -> str:
        return "—" if _num(value) is None else f"{float(value) * 100:+.2f}%"

    def pp(value: Any) -> str:
        return "—" if _num(value) is None else f"{float(value):+.2f}pp"

    def share(value: Any) -> str:
        return "—" if _num(value) is None else f"{float(value) * 100:.1f}%"

    def yi(value: Any, scale: float = 1e8) -> str:
        return "—" if _num(value) is None else f"{float(value) / scale:.2f}"

    lines = ["## 竞价分析", "", *[f"口径：{note}" for note in view.get("notes", ())], "",
             "板块变化列按 0920→0924→0925 等权涨幅；成员表按 0925 匹配额前 8 并补涨幅前 3。", ""]
    for row in view.get("auction", {}).get("plates", ()):
        lines.extend([f"### {row['plate']}（{row.get('category') or '—'}；来源：{'、'.join(row['sources'])}）", "",
                      f"涨幅 {pct(row.get('pct'))}；额 {yi(row.get('amount_sum_yuan'))} 亿；有效成员 {row.get('effective_member_count', '—')}；"
                      f"涨停 {row.get('limit_up_count', '—')}；昨板 {row.get('yest_pool_member_count', '—')}；"
                      f"0920→0924→0925 {pct(row.get('pct_0920'))}→{pct(row.get('pct_0924'))}→{pct(row.get('pct'))}；"
                      f"Top1 {share(row.get('top1_concentration'))}{' CONCENTRATED' if row.get('concentrated') else ''}。", "",
                      "| 个股 | 0925涨幅 | 匹配额(亿) | 未匹配买(万) | 未匹配卖(万) | 昨板 | 封停 | 价格 | 其他板块 |",
                      "|---|---:|---:|---:|---:|---|---|---|---|"])
        for member in (item for item in row["members"] if item.get("display_member")):
            lines.append(f"| {member['symbol']} {member.get('name') or ''} | {pct(member.get('pct'))} | "
                         f"{yi(member.get('matched_amount_yuan'))} | {yi(member.get('rest_bid_yuan'), 1e4)} | "
                         f"{yi(member.get('rest_ask_yuan'), 1e4)} | "
                         f"{'是' if member.get('in_yest_pool') else '否'}({member.get('lb_days') if member.get('lb_days') is not None else '—'}) | "
                         f"{'是' if member.get('sealed_0925') else '否'} | {member.get('price_flag')} | "
                         f"{'、'.join(member.get('also_in_plates', ())) or '—'} |")
        lines.append("")
    lines.extend(["## 开盘分析", "",
                  f"分档阈值仅用于显示；涨幅变化以百分点计。0935/0940 仅展示相对上一展示时点变化≥{OPENING_FOLD_PCT_CHANGE_PP:.1f}pp、名次变化≥{OPENING_FOLD_RANK_CHANGE}或含开板/新进个股的板块。", ""])
    previous_stage: dict[str, Mapping[str, Any]] = {}
    for tag, section in view.get("opening", {}).items():
        lines.extend([f"### {tag}", ""])
        new = view.get("emerging", {}).get(tag, {})
        lines.extend([f"#### {tag} 竞价后新冒头", "",
                      "新入焦点板块：" + ("、".join(row["plate"] for row in new.get("plates", ())) or "—"), "",
                      "| 代码 | 名称 | 主板块 | 0925 涨幅 | 当前涨幅 | 成交额(亿) | 昨板 | 独立入选板块 |",
                      "|---|---|---|---:|---:|---:|---|---|"])
        for row in new.get("stocks", ()):
            lines.append(f"| {row.get('symbol')} | {row.get('name') or '—'} | {row.get('primary_plate') or '—'} | "
                         f"{pct(row.get('auction_pct'))} | {pct(row.get('current_pct'))} | "
                         f"{yi(row.get('current_amount_yuan'))} | {'是' if row.get('in_yest_pool') else '否'} | "
                         f"{'是' if row.get('in_independent_plate') else '否'} |")
        lines.append("")
        candidates = list(section.get("plates", ()))
        if tag in ("0935", "0940"):
            shown = [row for row in candidates
                     if _significant_opening_plate(row, previous_stage.get(str(row["plate"]))) ]
            folded = len(candidates) - len(shown)
        else:
            shown, folded = candidates, 0
        for row in shown:
            lines.extend([f"#### {row['plate']}（来源：{'、'.join(row['sources'])}）", "",
                          f"涨幅 {pct(row.get('pct'))}；相对0925 {pp(row.get('change_vs_0925_pp'))}；"
                          f"名次变化 {row.get('rank_change') if row.get('rank_change') is not None else '—'}；"
                          f"额 {yi(row.get('amount_sum_yuan'))} 亿；有效成员 {row.get('effective_member_count', '—')}；"
                          f"涨停 {row.get('limit_up_count') if row.get('limit_up_count') is not None else '—'}；昨板 {row.get('yest_pool_member_count') if row.get('yest_pool_member_count') is not None else '—'}；"
                          f"Top1 {share(row.get('top1_concentration'))}{' CONCENTRATED' if row.get('concentrated') else ''}。", "",
                          "| 个股 | 当前涨幅 | 相对0925 | 成交额(亿) | 开板 | 新进 | 昨板 | 其他板块 |",
                          "|---|---:|---:|---:|---|---|---|---|"])
            for member in (item for item in row["members"] if item.get("display_member")):
                lines.append(f"| {member['symbol']} {member.get('name') or ''} | {pct(member.get('pct'))} | "
                             f"{pp(member.get('change_vs_0925_pp'))} | {yi(member.get('amount_yuan'))} | "
                             f"{'是' if member.get('opened_limit') else '否'} | "
                             f"{'是' if member.get('newly_emerged') else '否'} | "
                             f"{'是' if member.get('in_yest_pool') else '否'} | "
                             f"{'、'.join(member.get('also_in_plates', ())) or '—'} |")
            lines.append("")
        if folded:
            lines.extend([f"其余 {folded} 个板块无显著变化(见 JSON)", ""])
        previous_stage = {str(row["plate"]): row for row in candidates}
    tolerance = float(view.get("continuation", {}).get("tolerance_pp", CONTINUATION_TOLERANCE_PP))
    lines.extend(["## 延续 / 反转", "",
                  f"判据：延续=0945−0925≥−{tolerance:.1f}pp 且 0945>0；转弱=变化<−{tolerance:.1f}pp 且 0945>0；其余跨零为反转。", "",
                  "| 板块 | 0925 | 0932 | 0945 | 状态 |", "|---|---:|---:|---:|---|"])
    for row in view.get("continuation", {}).get("plates", ()):
        lines.append(f"| {row['plate']} | {pct(row.get('pct_0925'))} | {pct(row.get('pct_0932'))} | "
                     f"{pct(row.get('pct_0945'))} | {row.get('state') or '—'} |")
    return "\n".join(lines)


__all__ = ["CONTINUATION_TOLERANCE_PP", "OPENING_FOLD_PCT_CHANGE_PP", "OPENING_FOLD_RANK_CHANGE",
           "build_plate_stock_view", "continuation_state",
           "render_plate_stock_view", "select_focus_plates"]
