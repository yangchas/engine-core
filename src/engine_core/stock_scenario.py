"""Pure factual derivations for per-stock auction-to-open scenarios."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Mapping, Sequence


AUCTION_HIGH_PP = 1.0
AUCTION_LOW_PP = -1.0
CHANGE_UP_PP = 0.5
CHANGE_DOWN_PP = -0.5
FINAL_PRICE_SOURCES = frozenset(("SNAPSHOT_PX", "TICK_PX_FINAL", "FINAL"))


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _pp(stage: Mapping[str, Any]) -> float | None:
    value = _num(stage.get("pct_vs_pre_close"))
    return value * 100 if value is not None else None


def _final_flag(stage: Mapping[str, Any]) -> str:
    if _num(stage.get("price")) is None and _pp(stage) is None:
        return "NO_PRICE"
    flags = stage.get("price_flags", stage.get("flags", ()))
    if isinstance(flags, str):
        flags = (flags,)
    source = stage.get("price_source")
    if "PRE_FINAL_INDICATIVE" in flags or "INDICATIVE" in str(source):
        return "INDICATIVE"
    return "FINAL" if source in FINAL_PRICE_SOURCES else "INDICATIVE"


def _rankings(rows: Sequence[Mapping[str, Any]], primary_plate_of: Callable[[Mapping[str, Any]], Any]):
    cohorts: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        plate = primary_plate_of(row)
        if plate is not None and str(plate).strip():
            cohorts[str(plate)].append(row)
    output: dict[str, dict[str, int | None]] = {}
    for members in cohorts.values():
        for stage_tag, pct_key, amt_key, rank_pct_key, rank_amt_key, n_key in (
            ("0925", "pct_0925", "matched_amount_0925", "rank_pct_0925", "rank_amt_0925", "n_0925"),
            ("0945", "pct_0945", "amount_0945", "rank_pct_0945", None, "n_0945"),
        ):
            pct_rows = [(row, row[pct_key]) for row in members if row.get(pct_key) is not None]
            amt_rows = [(row, row[amt_key]) for row in members if row.get(amt_key) is not None]
            pct_counts = {str(row["symbol"]): len(pct_rows) for row, _ in pct_rows}
            pct_rank = {str(row["symbol"]): 1 + sum(value > val for _, value in pct_rows)
                        for row, val in pct_rows}
            amt_rank = {str(row["symbol"]): 1 + sum(value > val for _, value in amt_rows)
                        for row, val in amt_rows}
            for row in members:
                symbol = str(row["symbol"])
                values = output.setdefault(symbol, {})
                values[rank_pct_key] = pct_rank.get(symbol)
                values[n_key] = pct_counts.get(symbol, 0)
                if rank_amt_key:
                    values[rank_amt_key] = amt_rank.get(symbol)
    return output


def build_stock_scenarios(
    stocks: Sequence[Mapping[str, Any]],
    plate_pct_by_stage: Mapping[str, Mapping[str, Any]],
    *,
    primary_plate_of: Callable[[Mapping[str, Any]], Any],
) -> dict[str, dict[str, Any] | None]:
    """Derive pp changes, primary-plate comparisons, ranks, and path labels."""
    staging: list[dict[str, Any]] = []
    for stock in stocks:
        times = stock.get("times")
        symbol = str(stock.get("symbol", ""))
        if not isinstance(times, Mapping) or not times:
            staging.append({"symbol": symbol, "scenario": None, "primary_plate": primary_plate_of(stock)})
            continue
        auction = {tag: _pp(times.get(tag, {}) or {}) for tag in ("0920", "0924", "0925")}
        opened = {tag: _pp(times.get(tag, {}) or {}) for tag in ("0932", "0935", "0940", "0945")}
        a25 = times.get("0925", {}) or {}
        a32 = times.get("0932", {}) or {}
        a45 = times.get("0945", {}) or {}
        matched = _num(a25.get("amount"))
        amount_0932 = _num(a32.get("amount"))
        amount_0945 = _num(a45.get("amount"))
        delta_0932 = opened["0932"] - auction["0925"] if opened["0932"] is not None and auction["0925"] is not None else None
        delta_0945 = opened["0945"] - auction["0925"] if opened["0945"] is not None and auction["0925"] is not None else None
        state = a25.get("limit_state")
        try:
            state = int(state) if state is not None else None
        except (TypeError, ValueError, OverflowError):
            state = None
        final = _final_flag(a25)
        primary = primary_plate_of(stock)
        scenario = {
            "auction": {"pct_0920": auction["0920"], "pct_0924": auction["0924"],
                        "pct_0925": auction["0925"], "final_flag": final,
                        "matched_amount_0925": matched,
                        "unmatched_buy": _num(a25.get("rest_bid")),
                        "unmatched_sell": _num(a25.get("rest_ask")),
                        "limit_state_0925": state},
            "open": {**{f"pct_{tag}": value for tag, value in opened.items()},
                     "amount_0932": amount_0932, "amount_0945": amount_0945,
                     **{f"at_limit_{tag}": (times.get(tag, {}) or {}).get("at_limit_price")
                        for tag in ("0932", "0935", "0940", "0945")}},
            "change": {"d_0932_vs_0925": delta_0932, "d_0945_vs_0925": delta_0945},
            "open_amount_multiple": (amount_0932 / matched if amount_0932 is not None
                                      and matched is not None and matched > 0 else None),
            "vs_plate": {"rel_0925": None, "rel_0932": None, "rel_0945": None,
                         "rank_pct_0925": None, "rank_amt_0925": None,
                         "rank_pct_0945": None, "n_0925": 0, "n_0945": 0},
            "path": None,
        }
        if primary is not None:
            plate_values = plate_pct_by_stage.get(str(primary), {}) or {}
            for tag, key, stock_pct in (("0925", "rel_0925", auction["0925"]),
                                        ("0932", "rel_0932", opened["0932"]),
                                        ("0945", "rel_0945", opened["0945"])):
                plate_pct = _num(plate_values.get(tag))
                if stock_pct is not None and plate_pct is not None:
                    scenario["vs_plate"][key] = stock_pct - plate_pct * 100
        staging.append({"symbol": symbol, "scenario": scenario, "primary_plate": primary,
                        "pct_0925": auction["0925"], "matched_amount_0925": matched,
                        "pct_0945": opened["0945"], "amount_0945": amount_0945,
                        "limit_state_0925": state,
                        "at_limit_0945": a45.get("at_limit_price"),
                        "at_limit_any": any((times.get(tag, {}) or {}).get("at_limit_price") is True
                                            for tag in ("0932", "0935", "0940", "0945"))})

    ranks = _rankings([row for row in staging if row["scenario"] is not None], primary_plate_of)
    result: dict[str, dict[str, Any] | None] = {}
    for row in staging:
        scenario = row["scenario"]
        if scenario is None:
            result[row["symbol"]] = None
            continue
        scenario["vs_plate"].update(ranks.get(row["symbol"], {}))
        pct_a, delta = row.get("pct_0925"), scenario["change"]["d_0945_vs_0925"]
        auction_band = ("HIGH" if pct_a >= AUCTION_HIGH_PP else "LOW" if pct_a <= AUCTION_LOW_PP
                        else "FLAT") if pct_a is not None else None
        change_band = ("UP" if delta >= CHANGE_UP_PP else "DOWN" if delta <= CHANGE_DOWN_PP
                       else "SAME") if delta is not None else None
        state = row.get("limit_state_0925")
        at_limit_end = row.get("at_limit_0945")
        limit_prefix = ("LIMIT_UP" if state == 1 else "LIMIT_DOWN" if state == -1 else
                        "LIMIT_UP" if row.get("at_limit_any") and (pct_a or 0) >= 0 else
                        "LIMIT_DOWN" if row.get("at_limit_any") else None)
        broke = bool(state in (-1, 1) and at_limit_end is False)
        missing = []
        if auction_band is None:
            missing.append("MISSING_PCT_0925")
        if change_band is None:
            missing.append("MISSING_PCT_0945_OR_PCT_0925")
        label = (f"{limit_prefix}:{auction_band}_{change_band}" if limit_prefix and auction_band and change_band
                 else f"{auction_band}_{change_band}" if auction_band and change_band else None)
        scenario["path"] = ({"auction_band": auction_band, "change_band": change_band,
                             "label": label, "limit_prefix": limit_prefix, "broke_limit": broke}
                            if not missing else None)
        scenario["path_missing_reason"] = missing or None
        result[row["symbol"]] = scenario
    return result


__all__ = ["AUCTION_HIGH_PP", "AUCTION_LOW_PP", "CHANGE_UP_PP", "CHANGE_DOWN_PP",
           "build_stock_scenarios"]
