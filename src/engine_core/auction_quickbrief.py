"""Deterministic V7 09:26 brief using frozen V1 engineering thresholds."""
from __future__ import annotations

import statistics
from typing import Any, Mapping, Sequence

from .auction_panorama import _has_valid_auction_price

QUICKBRIEF_THRESHOLDS_V1 = {
    "version": "V1", "market_up_ratio": 0.70, "market_median_pct": 0.5,
    "market_price_coverage": 0.95, "relay_lag_pp": -15.0, "relay_min_n": 20,
    "plate_min_n": 5, "plate_price_coverage": 0.90, "plate_amount_coverage": 0.90,
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

    candidates: list[dict[str, Any]] = []
    rejections: dict[str, dict[str, Any]] = {}
    metrics_by_plate: dict[str, dict[str, Any]] = {}
    for pid, meta in member_map.items():
        symbols = meta["symbols"]
        present = [rows[s] for s in symbols if s in rows]
        priced = [r for r in present if _has_valid_auction_price(r) and _num(r.get("pct_change")) is not None]
        amounts = [r for r in present if _num(r.get("auction_amount_yuan")) is not None]
        n = len(symbols)
        price_cov = len(priced) / n if n else 0
        amount_cov = len(amounts) / n if n else 0
        plate_median = statistics.median(_num(r["pct_change"]) for r in priced) if priced else None
        d = plate_median
        d_excess = plate_median - median if plate_median is not None and median is not None else None
        e = (sum(_num(r["pct_change"]) > 0 for r in priced) / len(priced) - ratio) * 100 if priced and ratio is not None else None
        amount = sum(_num(r["auction_amount_yuan"]) for r in amounts) if amounts else None
        top3_rows = sorted(amounts, key=lambda r: (-_num(r.get("auction_amount_yuan")),
                                                    _symbol(r.get("symbol"))))[:3]
        top3 = [{"symbol": _symbol(r.get("symbol")), "name": _name(_symbol(r.get("symbol")), r, names),
                 "amount_yuan": _num(r.get("auction_amount_yuan")),
                 "amount_rank": r.get("amount_rank")} for r in top3_rows]
        evidence = {"plate_id": pid, "name": meta["name"], "n": n,
                    "price_coverage": price_cov, "price_n": len(priced),
                    "amount_coverage": amount_cov, "amount_n": len(amounts),
                    "median_pct": plate_median, "median_excess_pp": d_excess,
                    "up_ratio": sum(_num(r["pct_change"]) > 0 for r in priced) / len(priced) if priced else None,
                    "d_pct": d, "d_excess_pp": d_excess, "e_pp": e,
                    "up_n": sum(_num(r["pct_change"]) > 0 for r in priced),
                    "auction_amount_yuan": amount,
                    "top_amount_stocks": top3}
        metrics_by_plate[pid] = {key: evidence[key] for key in (
            "plate_id", "name", "n", "price_coverage", "price_n", "amount_coverage",
            "amount_n", "median_pct", "median_excess_pp", "up_ratio", "d_pct",
            "d_excess_pp", "e_pp", "up_n", "auction_amount_yuan")}
        reasons = []
        if (plate_classes or {}).get(pid) == "BROAD": reasons.append("BROAD")
        if not market_ready: reasons.append("MARKET_COVERAGE_INSUFFICIENT")
        if n < QUICKBRIEF_THRESHOLDS_V1["plate_min_n"]: reasons.append("MEMBERS_BELOW_MIN")
        if price_cov < QUICKBRIEF_THRESHOLDS_V1["plate_price_coverage"]: reasons.append("PRICE_COVERAGE_BELOW_MIN")
        if amount_cov < QUICKBRIEF_THRESHOLDS_V1["plate_amount_coverage"]: reasons.append("AMOUNT_COVERAGE_BELOW_MIN")
        if d is None or d + 1e-9 < QUICKBRIEF_THRESHOLDS_V1["plate_d_pct"]: reasons.append("D_BELOW_MIN")
        if e is None or e + 1e-9 < QUICKBRIEF_THRESHOLDS_V1["plate_e_pp"]: reasons.append("E_BELOW_MIN")
        if reasons:
            rejections[pid] = {"reasons": reasons, "n": n,
                               "price_coverage": price_cov, "amount_coverage": amount_cov,
                               "d_pct": d, "d_excess_pp": d_excess, "e_pp": e}
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

    amount_rows = [r for r in rows.values() if _num(r.get("auction_amount_yuan")) is not None]
    market_amount = sum(_num(r.get("auction_amount_yuan")) for r in amount_rows) if amount_rows else None
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
                          "selected": candidate is backup,
                          "reason": ("SELECTED" if candidate is backup else
                                     "TOP3_OVERLAP" if candidate["plate_id"] in overlap_ids else "LOWER_SCORE")}
                         for candidate in small[:3]]
    backup_rank_line = ("备看排序 " + "；".join(
        f"{index}. {candidate['name']} D={candidate['d_pct']:+.2f}% E={candidate['e_pp']:+.1f}pp"
        f"（{'入选' if candidate['selected'] else 'Top3重叠淘汰' if candidate['reason'] == 'TOP3_OVERLAP' else '排序靠后'}）"
        for index, candidate in enumerate(backup_top_scores, start=1))
        if backup_top_scores else "备看排序 无合格中小板块")
    status = ("有观察候选,等待09:32开盘验证;尚无开盘成交确认" if (main or backup) and not verified_0932
              else "暂无观察候选" if not (main or backup) else "09:32开盘成交已验证")
    lines = [f"09:26 竞价速览 | {stock_facts.get('trade_date', '')}"
             + (" | 历史回放样本,非当时实时生成的交易信号" if replay else ""),
             market_text, main_line, backup_line, backup_rank_line,
             f"风险 {multi_text}；{weak_text}；{seal_text}", f"结论 {status}"]
    lines.append("阈值为 V1 冻结工程阈值,非市场规律,未回测")

    def public_candidate(candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
        return ({k: candidate[k] for k in ("plate_id", "name", "n", "price_n", "price_coverage",
                                            "amount_n", "amount_coverage", "median_pct", "up_ratio",
                                            "d_pct", "d_excess_pp", "e_pp", "up_n",
                                            "auction_amount_yuan", "top_amount_stocks")}
                if candidate else None)

    plate_metric_fields = ["name", "n", "price_coverage", "amount_coverage",
                           "d_pct", "d_excess_pp", "e_pp", "auction_amount_yuan"]
    packed_plate_metrics = {
        pid: [evidence.get(field) for field in plate_metric_fields]
        for pid, evidence in sorted(metrics_by_plate.items())
    }

    return {
        "schema": "auction_quickbrief_v1", "threshold_version": "V1",
        "threshold_note": "V1 冻结工程阈值,非市场规律、未回测",
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
                  "weak_multi_board": [_symbol(r.get("symbol")) for r in weak]},
        "lines": lines,
    }
