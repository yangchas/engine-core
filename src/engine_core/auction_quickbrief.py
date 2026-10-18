"""Deterministic V7 09:26 brief using frozen V1 engineering thresholds."""
from __future__ import annotations

import statistics
from typing import Any, Mapping, Sequence

from .auction_panorama import _has_valid_auction_price, _positive_auction_amount_aggregate

QUICKBRIEF_THRESHOLDS_V1 = {
    "version": "V1", "market_up_ratio": 0.70, "market_median_pct": 0.5,
    "market_price_coverage": 0.95, "relay_lag_pp": -15.0, "relay_min_n": 20,
    "plate_min_n": 5, "plate_price_coverage": 0.90, "plate_amount_coverage": 0.90,
    "min_plate_amount_share": 0.005,
    "plate_d_pct": 0.5, "plate_e_pp": 5.0, "broad_min_n": 100,
    "backup_overlap_max": 1,
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _money_text(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "金额待核实"
    return f"{number / 1e8:.2f}亿" if abs(number) >= 1e8 else f"{number / 1e4:.2f}万"


def _symbol(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else str(value or "")


def _name(symbol: str, row: Mapping[str, Any], name_map: Mapping[str, Any]) -> str:
    value = row.get("name")
    mapped = name_map.get(symbol)
    if not value and isinstance(mapped, Mapping):
        value = mapped.get("name")
    elif not value:
        value = mapped
    return str(value or symbol)


def build_auction_quickbrief(stock_facts: Mapping[str, Any], *, plate_members: Any,
                             yesterday_pool: Sequence[Mapping[str, Any]],
                             plate_classes: Mapping[str, str] | None = None,
                             name_map: Mapping[str, Any] | None = None,
                             market_amount_history: Sequence[float] = (),
                             replay: bool = False,
                             verified_0932: bool = False) -> dict[str, Any]:
    names = name_map or {}
    rows = {_symbol(r.get("symbol")): dict(r) for r in stock_facts.get("rows", [])
            if isinstance(r, Mapping) and _symbol(r.get("symbol"))}
    valid = [r for r in rows.values()
             if _has_valid_auction_price(r) and _num(r.get("pct_change")) is not None]
    changes = [_num(r.get("pct_change")) for r in valid]
    up_n = sum(value > 0 for value in changes)
    ratio = up_n / len(changes) if changes else None
    median = statistics.median(changes) if changes else None
    coverage = len(valid) / len(rows) if rows else None
    market_ready = (coverage is not None and coverage >= QUICKBRIEF_THRESHOLDS_V1["market_price_coverage"]
                    and ratio is not None and median is not None)
    market_open = bool(market_ready and ratio >= QUICKBRIEF_THRESHOLDS_V1["market_up_ratio"]
                       and median >= QUICKBRIEF_THRESHOLDS_V1["market_median_pct"])

    pool_syms = {_symbol(r.get("symbol", r.get("code"))): r for r in yesterday_pool
                 if isinstance(r, Mapping) and _symbol(r.get("symbol", r.get("code")))}
    pool_rows = [rows[s] for s in pool_syms if s in rows and _has_valid_auction_price(rows[s])
                 and _num(rows[s].get("pct_change")) is not None]
    pool_up = sum(_num(r.get("pct_change")) > 0 for r in pool_rows)
    pool_ratio = pool_up / len(pool_rows) if pool_rows else None
    lag = (pool_ratio - ratio) * 100 if pool_ratio is not None and ratio is not None else None
    relay = bool(len(pool_rows) >= QUICKBRIEF_THRESHOLDS_V1["relay_min_n"] and lag is not None
                 and lag <= QUICKBRIEF_THRESHOLDS_V1["relay_lag_pp"])

    member_map: dict[str, dict[str, Any]] = {}
    for member in plate_members or []:
        if not isinstance(member, Mapping):
            continue
        pid = str(member.get("plate_id", member.get("plate_code", member.get("plate", ""))))
        symbol = _symbol(member.get("symbol", member.get("stock_code")))
        if not pid or not symbol:
            continue
        meta = member_map.setdefault(pid, {"name": str(member.get("plate_name", member.get("name", pid))),
                                           "symbols": set()})
        meta["symbols"].add(symbol)

    amount_rows = [r for r in rows.values()
                   if (_num(r.get("auction_amount_yuan")) or 0) > 0]
    _, market_amount = _positive_auction_amount_aggregate(list(rows.values()))

    candidates: list[dict[str, Any]] = []
    rejections: dict[str, dict[str, Any]] = {}
    metrics_by_plate: dict[str, dict[str, Any]] = {}
    for pid, meta in member_map.items():
        symbols = meta["symbols"]
        present = [rows[s] for s in symbols if s in rows]
        priced = [r for r in present if _has_valid_auction_price(r) and _num(r.get("pct_change")) is not None]
        amounts = [r for r in present if (_num(r.get("auction_amount_yuan")) or 0) > 0]
        n = len(symbols)
        price_cov = len(priced) / n if n else 0
        amount_cov = len(amounts) / n if n else 0
        plate_median = statistics.median(_num(r["pct_change"]) for r in priced) if priced else None
        d = plate_median
        d_excess = plate_median - median if plate_median is not None and median is not None else None
        e = (sum(_num(r["pct_change"]) > 0 for r in priced) / len(priced) - ratio) * 100 if priced and ratio is not None else None
        amount = sum(_num(r["auction_amount_yuan"]) for r in amounts) if amounts else None
        amount_share = amount / market_amount if amount is not None and market_amount and market_amount > 0 else None
        top3_rows = sorted(amounts, key=lambda r: (-_num(r.get("auction_amount_yuan")),
                                                    _symbol(r.get("symbol"))))[:3]
        top3 = [{"symbol": _symbol(r.get("symbol")), "name": _name(_symbol(r.get("symbol")), r, names),
                 "amount_yuan": _num(r.get("auction_amount_yuan")),
                 "pct_change": _num(r.get("pct_change")),
                 "amount_rank": r.get("amount_rank")} for r in top3_rows]
        evidence = {"plate_id": pid, "name": meta["name"], "n": n,
                    "price_coverage": price_cov, "price_n": len(priced),
                    "amount_coverage": amount_cov, "amount_n": len(amounts),
                    "median_pct": plate_median, "median_excess_pp": d_excess,
                    "up_ratio": sum(_num(r["pct_change"]) > 0 for r in priced) / len(priced) if priced else None,
                    "d_pct": d, "d_excess_pp": d_excess, "e_pp": e,
                    "up_n": sum(_num(r["pct_change"]) > 0 for r in priced),
                    "auction_amount_yuan": amount,
                    "market_amount_share": amount_share,
                    "top_amount_stocks": top3}
        metrics_by_plate[pid] = {key: evidence[key] for key in (
            "plate_id", "name", "n", "price_coverage", "price_n", "amount_coverage",
            "amount_n", "median_pct", "median_excess_pp", "up_ratio", "d_pct",
            "d_excess_pp", "e_pp", "up_n", "auction_amount_yuan", "market_amount_share")}
        reasons = []
        if (plate_classes or {}).get(pid) == "BROAD": reasons.append("BROAD")
        if not market_ready: reasons.append("MARKET_COVERAGE_INSUFFICIENT")
        if n < QUICKBRIEF_THRESHOLDS_V1["plate_min_n"]: reasons.append("MEMBERS_BELOW_MIN")
        if price_cov < QUICKBRIEF_THRESHOLDS_V1["plate_price_coverage"]: reasons.append("PRICE_COVERAGE_BELOW_MIN")
        if amount_cov < QUICKBRIEF_THRESHOLDS_V1["plate_amount_coverage"]: reasons.append("AMOUNT_COVERAGE_BELOW_MIN")
        if (amount_share is None or amount_share + 1e-12 <
                QUICKBRIEF_THRESHOLDS_V1["min_plate_amount_share"]):
            reasons.append("PLATE_AMOUNT_SHARE_BELOW_MIN")
        if d is None or d + 1e-9 < QUICKBRIEF_THRESHOLDS_V1["plate_d_pct"]: reasons.append("D_BELOW_MIN")
        if e is None or e + 1e-9 < QUICKBRIEF_THRESHOLDS_V1["plate_e_pp"]: reasons.append("E_BELOW_MIN")
        if reasons:
            rejections[pid] = {"reasons": reasons, "n": n,
                               "price_coverage": price_cov, "amount_coverage": amount_cov,
                               "d_pct": d, "d_excess_pp": d_excess, "e_pp": e,
                               "auction_amount_yuan": amount, "market_amount_share": amount_share}
        else:
            candidates.append({**evidence, "top3_symbols": {item["symbol"] for item in top3}})

    sort_key = lambda c: (-c["d_pct"], -c["e_pp"], -(c["auction_amount_yuan"] or 0), c["plate_id"])
    large = sorted((c for c in candidates if c["n"] >= QUICKBRIEF_THRESHOLDS_V1["broad_min_n"]), key=sort_key)
    small = sorted((c for c in candidates if QUICKBRIEF_THRESHOLDS_V1["plate_min_n"] <= c["n"] < QUICKBRIEF_THRESHOLDS_V1["broad_min_n"]), key=sort_key)
    main = large[0] if large else None
    backup = None
    backup_overlap: list[dict[str, Any]] = []
    if main:
        for candidate in small:
            overlap = len(candidate["top3_symbols"] & main["top3_symbols"])
            if overlap > QUICKBRIEF_THRESHOLDS_V1["backup_overlap_max"]:
                backup_overlap.append({"plate_id": candidate["plate_id"], "overlap_n": overlap,
                                       "score_d_pct": candidate["d_pct"], "score_e_pp": candidate["e_pp"],
                                       "amount_yuan": candidate["auction_amount_yuan"]})
                continue
            backup = candidate
            break
    elif small:
        backup = small[0]

    baseline = [_num(value) for value in market_amount_history]
    baseline = [value for value in baseline if value is not None and value > 0]
    baseline_median = statistics.median(baseline) if baseline else None
    amount_multiple = market_amount / baseline_median if market_amount is not None and baseline_median else None

    seals = [r for r in rows.values() if isinstance(r.get("seal"), Mapping)
             and r["seal"].get("status") in ("VERIFIED", "VERIFIED_REPLAY")
             and _num(r["seal"].get("seal_amount_yuan")) is not None]
    seal_total = sum(_num(r["seal"]["seal_amount_yuan"]) for r in seals)
    seal_top = max(seals, key=lambda r: (_num(r["seal"]["seal_amount_yuan"]), _symbol(r.get("symbol"))), default=None)

    multi_pool = []
    multi_pool_symbols: set[str] = set()
    for symbol, pool_row in pool_syms.items():
        try:
            streak = float(pool_row.get("lb_days", pool_row.get("streak", 1)))
        except (TypeError, ValueError):
            continue
        if streak >= 2:
            multi_pool_symbols.add(symbol)
            if symbol in rows:
                multi_pool.append(rows[symbol])
    confirmed_next = [r for r in multi_pool if isinstance(r.get("seal"), Mapping)
                      and r["seal"].get("status") in ("VERIFIED", "VERIFIED_REPLAY")
                      and r.get("limit_up_candidate") is True]
    weak = sorted((r for r in multi_pool if _has_valid_auction_price(r)
                   and (_num(r.get("pct_change")) or 0) <= -3),
                  key=lambda r: (_num(r.get("pct_change")), _symbol(r.get("symbol"))))[:2]
    weak_text = "、".join(f"{_name(_symbol(r.get('symbol')), r, names)} {_num(r.get('pct_change')):+.2f}%"
                          for r in weak) or "无符合阈值的昨日≥2板弱势股"
    seal_text = (f"封单极值 {_name(_symbol(seal_top.get('symbol')), seal_top, names)} "
                 f"{_num(seal_top['seal']['seal_amount_yuan']) / 1e8:.2f}亿"
                 if seal_top else "封单极值 待核实")
    if seal_top and seal_total:
        seal_text += f"，占已核实封单 {100 * _num(seal_top['seal']['seal_amount_yuan']) / seal_total:.1f}%（封单金额不等于实际成交）"
    multi_text = f"昨日≥2板竞价封下一板 {len(confirmed_next)}/{len(multi_pool_symbols)}"

    if not market_ready:
        market_text = "市场 覆盖不足,不下结论"
    else:
        relay_text = ("昨板接力样本不足" if len(pool_rows) < QUICKBRIEF_THRESHOLDS_V1["relay_min_n"] else
                      f"昨日涨停接力明显落后（昨板高开{pool_up}/{len(pool_rows)}"
                      f"({pool_ratio:.1%})，落后市场{abs(lag):.2f}pp）"
                      if relay else ("昨日涨停接力不弱于阈值" if lag is not None else "昨板接力样本不足"))
        baseline_text = (f"基线{len(baseline)}/64不足" if len(baseline) < 64 else
                         f"基线64/64中位{baseline_median / 1e8:.2f}亿/{amount_multiple:.2f}倍"
                         if amount_multiple is not None else "基线待核实")
        history_text = (f"竞价额{market_amount / 1e8:.2f}亿、{baseline_text}"
                        if market_amount is not None else "竞价额待核实")
        market_text = (f"市场 {'普遍高开' if market_open else '未触发普遍高开'}，上涨{ratio * 100:.1f}%，"
                       f"中位{median:+.2f}%；{relay_text}；{history_text}")

    def candidate_line(label: str, candidate: Mapping[str, Any] | None, fallback: str) -> str:
        if not candidate:
            return f"{label} {fallback}"
        top = "、".join(f"{item['name']}#{item['amount_rank'] if item['amount_rank'] is not None else '待核实'}"
                        f"({_money_text(item['amount_yuan'])})"
                        for item in candidate["top_amount_stocks"])
        return (f"{label} {candidate['name']}（{candidate['n']}只，中位{candidate['d_pct']:+.2f}%"
                f"（超市场{candidate['d_excess_pp']:+.2f}pp）、上涨{candidate['up_n']}/{candidate['price_n']}"
                f"（超市场{candidate['e_pp']:+.1f}pp）、竞价额{candidate['auction_amount_yuan'] / 1e8:.2f}亿）"
                f"；金额Top {top}")

    main_line = candidate_line("主看", main, "暂无符合广泛强势条件的板块")
    backup_line = candidate_line("备看", backup, "暂无符合条件的中小板块")
    overlap_ids = {item["plate_id"] for item in backup_overlap}
    backup_top_scores = [{"plate_id": candidate["plate_id"], "name": candidate["name"],
                          "d_pct": candidate["d_pct"], "e_pp": candidate["e_pp"],
                          "auction_amount_yuan": candidate["auction_amount_yuan"],
                          "market_amount_share": candidate["market_amount_share"],
                          "selected": candidate is backup,
                          "reason": ("SELECTED" if candidate is backup else
                                     "TOP3_OVERLAP" if candidate["plate_id"] in overlap_ids else "LOWER_SCORE")}
                         for candidate in small[:3]]
    share_rejected = [metrics_by_plate[pid] for pid in rejections
                      if QUICKBRIEF_THRESHOLDS_V1["plate_min_n"] <= metrics_by_plate[pid]["n"]
                      < QUICKBRIEF_THRESHOLDS_V1["broad_min_n"]
                      and rejections[pid]["reasons"] == ["PLATE_AMOUNT_SHARE_BELOW_MIN"]]
    share_rejected.sort(key=lambda c: (-c["d_pct"], -c["e_pp"], -(c["auction_amount_yuan"] or 0), c["plate_id"]))
    rejected_backup_scores = {
        candidate["plate_id"]: {"name": candidate["name"],
                                "market_amount_share": candidate["market_amount_share"],
                                "reason": "PLATE_AMOUNT_SHARE_BELOW_MIN"}
        for candidate in share_rejected[:3]
    }
    backup_rank_line = ("备看排序 " + "；".join(
        f"{index}. {candidate['name']} D={candidate['d_pct']:+.2f}% E={candidate['e_pp']:+.1f}pp"
        f" 占比={candidate['market_amount_share']:.2%}"
        f"（{'入选' if candidate['selected'] else 'Top3重叠淘汰' if candidate['reason'] == 'TOP3_OVERLAP' else '排序靠后'}）"
        for index, candidate in enumerate(backup_top_scores, start=1))
        if backup_top_scores else "备看排序 无合格中小板块")
    excluded_share_line = ("流动性门槛排除 " + "；".join(
        f"{item['name']}占比{item['market_amount_share']:.2%}" if item["market_amount_share"] is not None
        else f"{item['name']}占比待核实"
        for item in rejected_backup_scores.values()) if rejected_backup_scores else None)
    if excluded_share_line:
        backup_rank_line += "；" + excluded_share_line
    status = ("有观察候选,等待09:32开盘验证;尚无开盘成交确认" if (main or backup) and not verified_0932
              else "暂无观察候选" if not (main or backup) else "09:32开盘成交已验证")
    lines = [f"09:26 竞价速览 | {stock_facts.get('trade_date', '')}"
             + (" | 历史回放样本,非当时实时生成的交易信号" if replay else ""),
             market_text, main_line, backup_line, backup_rank_line,
             f"风险 {multi_text}；{weak_text}；{seal_text}", f"结论 {status}"]
    lines.append("阈值为 V1 冻结工程阈值,非市场规律,未回测")

    # Public email copy is intentionally separate from the diagnostic lines
    # above, which remain in report.json for review and evidence.
    email_lines = [f"09:26 竞价速览 · {stock_facts.get('trade_date', '')}"]
    if replay:
        email_lines.append("历史回放样本,非当时实时生成的交易信号")
    if coverage is None or coverage < QUICKBRIEF_THRESHOLDS_V1["market_price_coverage"]:
        email_market_line = "市场:覆盖不足,不下结论。"
    elif not market_ready:
        email_market_line = "市场:样本不足,不下结论。"
    elif not market_open:
        email_market_line = "市场:未触发普遍高开。"
    elif len(pool_rows) < QUICKBRIEF_THRESHOLDS_V1["relay_min_n"]:
        email_market_line = "市场:普遍高开,接力样本不足,不下结论。"
    elif relay:
        email_market_line = "市场:普遍高开,昨日涨停接力明显落后。"
    else:
        email_market_line = "市场:普遍高开,昨日涨停接力未明显落后。"
    email_lines.append(email_market_line)
    if market_ready:
        relay_summary = (f"昨板高开{pool_up}/{len(pool_rows)}({pool_ratio:.1%})，落后市场{abs(lag):.2f}个百分点"
                         if relay else "昨日涨停接力未触发明显落后")
        if len(pool_rows) < QUICKBRIEF_THRESHOLDS_V1["relay_min_n"]:
            relay_summary = "昨板接力样本不足,不下结论"
        baseline_summary = (f"历史基线{len(baseline)}/64日不足" if len(baseline) < 64 else
                            f"历史基线64/64日,中位{baseline_median / 1e8:.2f}亿" if baseline_median is not None else
                            "历史基线样本不足,不下结论")
        email_lines.append(f"{ratio*100:.1f}%上涨、中位{median:+.2f}%;{relay_summary}。竞价额{market_amount/1e8:.2f}亿,{baseline_summary}。"
                           if market_amount is not None else f"{ratio*100:.1f}%上涨、中位{median:+.2f}%;{relay_summary}。竞价额样本不足,不下结论。")

    def public_candidate_lines(label: str, candidate: Mapping[str, Any] | None,
                               fallback: str) -> list[str]:
        if not candidate:
            return [f"{label}:{fallback}"]
        amount_stocks = candidate["top_amount_stocks"]
        top_text = "、".join(f"{item['name']}金额全市场#{item['amount_rank']}"
                             for item in amount_stocks[:2] if item.get("amount_rank") is not None)
        header = f"{label}:{candidate['name']}"
        metrics = (f"中位{candidate['d_pct']:+.2f}%(超市场{candidate['d_excess_pp']:.2f}pp),"
                   f"上涨{candidate['up_n']}/{candidate['price_n']}(超市场{candidate['e_pp']:.1f}pp),"
                   f"竞价{candidate['auction_amount_yuan']/1e8:.2f}亿。{top_text}")
        if label == "备看" and amount_stocks:
            top = amount_stocks[0]
            pct = top.get("pct_change")
            state = "平开" if pct == 0 else "高开" if isinstance(pct, (int, float)) and pct > 0 else "低开" if isinstance(pct, (int, float)) else None
            if state:
                metrics += f"。金额Top1{top['name']}竞价{state}"
        return [header, metrics]

    email_lines.extend(public_candidate_lines("主看", main, "暂无符合广泛强势条件的板块"))
    email_lines.extend(public_candidate_lines("备看", backup, "暂无符合广泛强势条件的板块"))
    email_lines.append("风险:高标接力分化" if relay or (multi_pool_symbols and not confirmed_next)
                       else "风险:未见明确高标接力负反馈")
    email_lines.append(f"{multi_text.replace('下一板 ', '下一板')};{weak_text}" if relay or (multi_pool_symbols and not confirmed_next)
                       else "高标接力负反馈样本不足,不下结论")
    if seal_top and seal_total:
        seal_name = _name(_symbol(seal_top.get("symbol")), seal_top, names)
        seal_amount = _num(seal_top["seal"]["seal_amount_yuan"])
        email_lines.extend([f"封单极值:{seal_name}{seal_amount / 1e8:.2f}亿",
                            f"全市场封单{seal_total / 1e8:.2f}亿,{seal_name}占{100*seal_amount/seal_total:.1f}%;"
                            "封单金额不等于实际成交。"])
    else:
        email_lines.extend(["封单极值:样本不足,不下结论", "全市场封单样本不足,不下结论"])
    email_lines.append("结论:有板块观察候选,09:32等待开盘验证;尚无开盘成交确认"
                       if (main or backup) and not verified_0932 else
                       "结论:暂无符合广泛强势条件的板块" if not (main or backup) else
                       "结论:09:32开盘成交已验证")
    email_lines.append("阈值为 V1 冻结工程阈值,非市场规律,未回测")

    def public_candidate(candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
        return ({k: candidate[k] for k in ("plate_id", "name", "n", "price_n", "price_coverage",
                                            "amount_n", "amount_coverage", "median_pct", "up_ratio",
                                            "d_pct", "d_excess_pp", "e_pp", "up_n",
                                            "auction_amount_yuan", "market_amount_share", "top_amount_stocks")}
                if candidate else None)

    plate_metric_fields = ["n", "price_coverage", "amount_coverage",
                           "d_pct", "e_pp", "auction_amount_yuan"]
    packed_plate_metrics = {
        pid: [evidence.get(field) for field in plate_metric_fields]
        for pid, evidence in sorted(metrics_by_plate.items())
    }

    return {
        "schema": "auction_quickbrief_v1", "threshold_version": "V1",
        "threshold_note": "V1 冻结工程阈值,非市场规律、未回测；板块竞价额占市场下限0.5%",
        "thresholds": QUICKBRIEF_THRESHOLDS_V1,
        "metrics": {"market": {"valid_n": len(valid), "universe_n": len(rows), "coverage": coverage,
                                "up_n": up_n, "up_ratio": ratio, "median_pct": median,
                                "auction_amount_yuan": market_amount,
                                "amount_coverage": len(amount_rows) / len(rows) if rows else None,
                                "baseline_n": len(baseline), "baseline_median_yuan": baseline_median,
                                "baseline_multiple": amount_multiple},
                   "yesterday_pool": {"valid_n": len(pool_rows), "up_n": pool_up,
                                      "up_ratio": pool_ratio, "lag_pp": lag},
                   "seal": {"verified_n": len(seals), "total_yuan": seal_total,
                            "top_symbol": _symbol(seal_top.get("symbol")) if seal_top else None,
                            "top_name": _name(_symbol(seal_top.get("symbol")), seal_top, names) if seal_top else None,
                            "top_amount_yuan": _num(seal_top["seal"]["seal_amount_yuan"]) if seal_top else None},
                   "multi_board": {"n": len(multi_pool_symbols), "observed_n": len(multi_pool),
                                   "unobserved_n": len(multi_pool_symbols) - len(multi_pool),
                                   "sealed_next_board_n": len(confirmed_next)}},
        "rules": {"market_coverage_sufficient": market_ready, "market_open": market_open,
                  "relay_lag": relay, "relay_evidence": {"pool_up_n": pool_up, "pool_valid_n": len(pool_rows),
                                                           "pool_up_ratio": pool_ratio, "market_up_ratio": ratio,
                                                           "lag_pp": lag},
                  "plate_candidates": len(candidates), "plate_metric_fields": plate_metric_fields,
                  "plate_metrics": packed_plate_metrics, "rejections": {pid: item["reasons"]
                                                                          for pid, item in sorted(rejections.items())},
                  "main": public_candidate(main), "backup": public_candidate(backup),
             "backup_overlap_rejections": backup_overlap, "replay_banner": bool(replay),
                  "backup_top_scores": backup_top_scores,
                  "rejected_backup_scores": rejected_backup_scores,
                  "weak_multi_board": [_symbol(r.get("symbol")) for r in weak]},
        "lines": lines, "email_lines": email_lines,
    }
