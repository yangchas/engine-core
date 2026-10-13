"""Evaluate historical auction plate rankings against same/next-day returns.

This is a descriptive historical sample. It does not establish a strategy result.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Mapping

from engine_core.plate_aggregation import StockFact, aggregate_plates
from engine_core.plate_membership import (
    MEMBERSHIP_POLICY_LEGACY_V1,
    MEMBERSHIP_POLICY_USER_PRIORITY_V1,
    PlateHierarchy,
    StockPlateMemberships,
    build_memberships,
    default_alias_hierarchy,
    members_of,
)
from engine_core.plate_rules import is_generic_plate


RANKING_NAMES = (
    "legacy_amount_weighted",
    "core_amount_share",
    "core_excess_pct",
    "core_amount_delta",
)


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _average(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: (values[i], i))
    ranks = [0.0] * len(values)
    first = 0
    while first < len(order):
        end = first + 1
        while end < len(order) and values[order[end]] == values[order[first]]:
            end += 1
        rank = (first + 1 + end) / 2.0
        for pos in range(first, end):
            ranks[order[pos]] = rank
        first = end
    return ranks


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left = _average_ranks([pair[0] for pair in pairs])
    right = _average_ranks([pair[1] for pair in pairs])
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    cov = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_ss = sum((a - left_mean) ** 2 for a in left)
    right_ss = sum((b - right_mean) ** 2 for b in right)
    denom = math.sqrt(left_ss * right_ss)
    return cov / denom if denom else None


def evaluate_day(
    auction_rows: Mapping[str, Mapping[str, object]],
    close_by_symbol_date: Mapping[str, Mapping[str, object]],
    memberships: StockPlateMemberships,
    hierarchy: PlateHierarchy,
    date: str,
    next_date: str,
    min_members: int = 8,
) -> dict:
    """Evaluate one auction snapshot using only supplied auction, membership and close facts."""
    if min_members < 1:
        raise ValueError("min_members must be positive")
    normalized: dict[str, dict[str, float | int | None]] = {}
    facts: dict[str, StockFact] = {}
    missing = {"auction_price": 0, "same_day_close": 0, "next_day_close": 0, "chg_bp": 0, "match_amt_yuan": 0}
    for symbol, raw in auction_rows.items():
        px_milli = _number(raw.get("px_milli"))
        change_bp = _number(raw.get("chg_bp"))
        amount = _number(raw.get("match_amt_yuan"))
        limit_state_value = _number(raw.get("limit_state"))
        limit_state = int(limit_state_value) if limit_state_value is not None else None
        if px_milli is None or px_milli <= 0:
            missing["auction_price"] += 1
        if change_bp is None:
            missing["chg_bp"] += 1
        if amount is None:
            missing["match_amt_yuan"] += 1
        normalized[symbol] = {"px_milli": px_milli, "chg_bp": change_bp,
                              "match_amt_yuan": amount, "limit_state": limit_state}
        if change_bp is not None:
            facts[symbol] = StockFact(
                symbol=symbol, pre_close=1.0, price=1.0 + change_bp / 10000.0,
                amount=amount, limit_state=limit_state, lb_days=None, amount_prev=None,
            )

    aggregated = aggregate_plates(memberships, hierarchy, facts)
    core_stats = {row.plate: row for row in aggregated.stats}
    legacy_scores: dict[str, float] = defaultdict(float)
    auction_members: dict[str, set[str]] = defaultdict(set)
    for symbol, rows in memberships.by_symbol.items():
        if symbol not in normalized:
            continue
        amount = normalized[symbol]["match_amt_yuan"]
        if amount is None:
            continue
        weights = memberships.legacy_weights.get(symbol)
        if weights is None:
            legacy_rows = sorted(rows, key=lambda row: row.rank_in_stock)
            names = [row.plate for row in legacy_rows]
            if len(names) > 1 and is_generic_plate(names[0]) and not is_generic_plate(names[1]):
                names[0], names[1] = names[1], names[0]
            weights = tuple(
                (plate, (1.0 if index == 0 else 0.6) * (0.18 if is_generic_plate(plate) else 1.0))
                for index, plate in enumerate(names[:2])
            )
        for plate, weight in weights:
            legacy_scores[plate] += amount * weight
    for plate in {row.plate for rows in memberships.by_symbol.values() for row in rows}:
        auction_members[plate] = members_of(plate, memberships, hierarchy).intersection(normalized)

    same_returns: dict[str, float] = {}
    next_returns: dict[str, float] = {}
    for symbol, row in normalized.items():
        px_milli = row["px_milli"]
        close_today = _number(close_by_symbol_date.get(symbol, {}).get(date))
        close_next = _number(close_by_symbol_date.get(symbol, {}).get(next_date))
        if px_milli is not None and px_milli > 0 and close_today is not None and close_today > 0:
            same_returns[symbol] = close_today / (px_milli / 1000.0) - 1.0
        else:
            missing["same_day_close"] += 1
        if close_today is not None and close_today > 0 and close_next is not None and close_next > 0:
            next_returns[symbol] = close_next / close_today - 1.0
        else:
            missing["next_day_close"] += 1
    market_same, market_next = _average(list(same_returns.values())), _average(list(next_returns.values()))

    scores: dict[str, dict[str, float | None]] = {name: {} for name in RANKING_NAMES}
    for plate, stat in core_stats.items():
        scores["core_amount_share"][plate] = stat.amount_share_of_market
        scores["core_excess_pct"][plate] = stat.excess_vs_market * 100 if stat.excess_vs_market is not None else None
        scores["core_amount_delta"][plate] = stat.amount_delta_sum
    scores["legacy_amount_weighted"].update(legacy_scores)

    rankings: dict[str, dict] = {}
    for name in RANKING_NAMES:
        eligible = [
            (plate, score) for plate, score in scores[name].items()
            if score is not None and not is_generic_plate(plate)
            and len(auction_members.get(plate, ())) >= min_members
        ]
        eligible.sort(key=lambda item: (-float(item[1]), item[0]))
        ranked = eligible[:10]
        plate_rows = []
        correlation_pairs: list[tuple[float, float]] = []
        selected_symbols: set[str] = set()
        for rank, (plate, score) in enumerate(ranked, start=1):
            members = auction_members[plate]
            selected_symbols.update(members)
            returns = [same_returns[symbol] for symbol in members if symbol in same_returns]
            nexts = [next_returns[symbol] for symbol in members if symbol in next_returns]
            same_mean, next_mean = _average(returns), _average(nexts)
            same_excess = same_mean - market_same if same_mean is not None and market_same is not None else None
            next_excess = next_mean - market_next if next_mean is not None and market_next is not None else None
            if same_excess is not None:
                correlation_pairs.append((float(score), same_excess))
            plate_rows.append({
                "rank": rank, "plate": plate, "score": score, "auction_member_count": len(members),
                "same_day_member_count": len(returns), "same_day_mean_return_pct": same_mean * 100 if same_mean is not None else None,
                "same_day_excess_pct": same_excess * 100 if same_excess is not None else None,
                "next_day_member_count": len(nexts), "next_day_mean_return_pct": next_mean * 100 if next_mean is not None else None,
                "next_day_excess_pct": next_excess * 100 if next_excess is not None else None,
            })
        selected_same = [same_returns[symbol] for symbol in selected_symbols if symbol in same_returns]
        selected_next = [next_returns[symbol] for symbol in selected_symbols if symbol in next_returns]
        same_avg, next_avg = _average(selected_same), _average(selected_next)
        rankings[name] = {
            "top_plates": plate_rows,
            "selected_member_count": len(selected_symbols),
            "same_day": {"member_count": len(selected_same), "mean_return_pct": same_avg * 100 if same_avg is not None else None,
                         "market_mean_return_pct": market_same * 100 if market_same is not None else None,
                         "mean_excess_pct": (same_avg - market_same) * 100 if same_avg is not None and market_same is not None else None},
            "next_day": {"member_count": len(selected_next), "mean_return_pct": next_avg * 100 if next_avg is not None else None,
                         "market_mean_return_pct": market_next * 100 if market_next is not None else None,
                         "mean_excess_pct": (next_avg - market_next) * 100 if next_avg is not None and market_next is not None else None},
            "spearman_rank_vs_same_day_excess": _spearman(correlation_pairs),
        }
    return {"date": date, "next_date": next_date, "auction_row_count": len(auction_rows),
            "missing_counts": missing, "ranking_scores": scores, "rankings": rankings}


def ensure_empty_output_dir(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"output directory must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _load_inputs(snapshot_path: Path, kline_path: Path):
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    kline = json.loads(kline_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot.get("stock_plate"), Mapping) or not isinstance(snapshot.get("s2p"), Mapping):
        raise ValueError("snapshot JSON must contain stock_plate and s2p mappings")
    close_by_symbol_date: dict[str, dict[str, object]] = {}
    for symbol, rows in kline.items():
        daily: dict[str, object] = {}
        if isinstance(rows, Mapping):
            daily = {str(day)[:10]: close for day, close in rows.items()}
        else:
            for row in rows:
                if isinstance(row, (list, tuple)) and len(row) > 4 and row[0] is not None:
                    daily[str(row[0])[:10]] = row[4]
        close_by_symbol_date[str(symbol)] = daily
    symbols = set(map(str, snapshot["stock_plate"])) | set(map(str, snapshot["s2p"])) | set(close_by_symbol_date)
    primary = {str(symbol): value for symbol, value in snapshot["stock_plate"].items()}
    themes = {str(symbol): value for symbol, value in snapshot["s2p"].items()}
    for symbol, plate in primary.items():
        current = themes.get(symbol, ())
        current = [current] if isinstance(current, str) else list(current or ())
        themes[symbol] = [plate, *current]
    user = build_memberships(symbols=symbols, pool_plate_by_symbol={}, reason_candidates_by_symbol={},
                             static_themes_by_symbol=themes, policy=MEMBERSHIP_POLICY_USER_PRIORITY_V1)
    legacy = build_memberships(symbols=symbols, pool_plate_by_symbol=primary, reason_candidates_by_symbol={},
                               static_themes_by_symbol=themes, policy=MEMBERSHIP_POLICY_LEGACY_V1)
    memberships = replace(user, legacy_weights=legacy.legacy_weights)
    return close_by_symbol_date, memberships


def _read_auction_rows(td_host: str, trade_date: str) -> dict[str, dict]:
    try:
        import taos  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("TD query requires the 'taos' Python client; install it in this environment") from exc
    database = os.environ.get("TDENGINE_DATABASE", "market_data1")
    if not database.replace("_", "").isalnum():
        raise ValueError("invalid TDENGINE_DATABASE")
    conn = taos.connect(
        host=td_host, port=int(os.environ.get("TDENGINE_PORT", "6030")),
        user=os.environ.get("TDENGINE_USER", "root"), password=os.environ.get("TDENGINE_PASSWORD", "taosdata"),
        database=database, config=os.environ.get("TDENGINE_CONFIG", "/etc/taos"),
    )
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT symbol, px_milli, chg_bp, match_amt_yuan, limit_state "
            f'FROM {database}.auction_snapshot_v2 WHERE trade_date="{trade_date.replace("-", "")}" '
            'AND auction_tag="0925"'
        )
        return {
            str(symbol): {"px_milli": px, "chg_bp": bp, "match_amt_yuan": amount, "limit_state": state}
            for symbol, px, bp, amount, state in cursor.fetchall()
        }
    finally:
        conn.close()


def _summarize(days: list[dict]) -> dict:
    result = {}
    for name in RANKING_NAMES:
        samples = [day["rankings"][name] for day in days]
        same = [row["same_day"]["mean_excess_pct"] for row in samples if row["same_day"]["mean_excess_pct"] is not None]
        nxt = [row["next_day"]["mean_excess_pct"] for row in samples if row["next_day"]["mean_excess_pct"] is not None]
        result[name] = {
            "sample_days": len(same),
            "next_day_sample_days": len(nxt),
            "same_day_mean_excess_pct": _average(same),
            "same_day_positive_days": sum(value > 0 for value in same),
            "same_day_win_rate": sum(value > 0 for value in same) / len(same) if same else None,
            "next_day_mean_excess_pct": _average(nxt),
            "next_day_positive_days": sum(value > 0 for value in nxt),
            "next_day_win_rate": sum(value > 0 for value in nxt) / len(nxt) if nxt else None,
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--kline-range", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--td-host", default="127.0.0.1")
    args = parser.parse_args(argv)
    start, end = date.fromisoformat(args.start_date), date.fromisoformat(args.end_date)
    if start > end:
        parser.error("--start-date must be on or before --end-date")
    ensure_empty_output_dir(args.output_dir)
    close_by_symbol_date, memberships = _load_inputs(args.snapshot, args.kline_range)
    dates = sorted({day for rows in close_by_symbol_date.values() for day in rows if day >= args.start_date})
    daily, skipped = [], []
    for index, current in enumerate(dates[:-1]):
        if current > args.end_date:
            continue
        next_day = dates[index + 1]
        auction = _read_auction_rows(args.td_host, current)
        if len(auction) < 4000:
            skipped.append({"date": current, "reason": "auction_row_count_below_4000", "auction_row_count": len(auction)})
            continue
        daily.append(evaluate_day(auction, close_by_symbol_date, memberships, default_alias_hierarchy(), current, next_day))
    payload = {
        "methodology": {
            "auction_source": "TD auction_snapshot_v2, auction_tag=0925; read-only SELECT",
            "legacy_amount_weighted": "first two legacy-approx memberships; weights 1.0/0.6; generic plate weight multiplied by 0.18",
            "core_amount_share": "C058 amount share of covered market amount",
            "core_excess_pct": "C058 equal-weight auction change excess vs covered market, in percentage points",
            "core_amount_delta": "C058 amount_delta_sum; unavailable without previous auction amount",
            "same_day_return": "daily close / auction px_milli price - 1; excess vs equal-weight valid auction universe",
            "next_day_return": "next available daily close / same-day close - 1; excess vs valid paired auction universe",
            "membership_policy": "USER_PRIORITY_V1 for core facts, with LEGACY_V1 approximate weights",
            "missing": "None values are excluded, never replaced with zero; counts are reported",
            "caveat": "Historical sample does not constitute a strategy conclusion.",
        },
        "daily": daily,
        "skipped": skipped,
        "summary": _summarize(daily),
    }
    (args.output_dir / "evaluation.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    readme = """# Plate aggregation evaluation\n\n""" + """This is a descriptive historical sample, not a strategy conclusion.\n\n""" + """Auction rows are read from TD `auction_snapshot_v2` at tag `0925`. Legacy amount ranking applies 1.0/0.6 weights to the first two legacy-approximate memberships and multiplies generic-plate weights by 0.18. Core amount share and excess use C058 aggregation with USER_PRIORITY_V1 memberships. Core amount delta remains missing without prior auction amounts.\n\n""" + """Same-day return is daily close divided by auction price; next-day return is next available daily close divided by same-day close. Excess subtracts the equal-weight mean over valid auction-universe members. Missing data is excluded and counted, never filled with zero. Dates with fewer than 4,000 auction rows are skipped.\n"""
    (args.output_dir / "README.md").write_text(readme, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
