"""Read-only TD report for stock/plate auction and 09:32 opening facts."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from engine_core.auction_book import AuctionBookTick, derive_auction_book
from engine_core.plate_membership import (
    MEMBERSHIP_POLICY_USER_PRIORITY_V1, PlateHierarchy, apply_hierarchy, build_memberships,
    default_alias_hierarchy,
)
from engine_core.plate_rules import build_plate_candidates_from_reason
from engine_core.plate_hierarchy_kaipan import build_hierarchy_as_of, parse_kaipan_reason_items
from engine_core.plate_auction_open_report import build_session_report, render_report_markdown
from engine_core.plate_significance import select_independent_plates
from engine_core.plate_rules import normalize_plate_name
from examples.run_offline_plate_pipeline import (
    _load_kaipan_hot_plates, _load_kaipan_inputs_with_status, _load_kaipan_reasons, _snapshot_asof,
    _load_snapshot, _snapshot_sealed_at, ensure_empty_output_dir,
)


_ZONE = ZoneInfo("Asia/Shanghai")
_DEFAULT_OPEN_TIMES = ("0932", "0935", "0940", "0945")


def parse_open_times(value: str) -> tuple[str, ...]:
    tags = tuple(item.strip() for item in value.split(",") if item.strip())
    if not tags:
        raise ValueError("--open-times must contain at least one HHMM value")
    for tag in tags:
        if not re.fullmatch(r"\d{4}", tag):
            raise ValueError("--open-times values must use HHMM")
        hour, minute = int(tag[:2]), int(tag[2:])
        if hour > 23 or minute > 59 or tag < "0930":
            raise ValueError("--open-times values must be valid opening-session times >= 0930")
    if tuple(sorted(set(tags))) != tags:
        raise ValueError("--open-times values must be unique and increasing")
    return tags


def _point_in_time_flags(snapshot_asof: str | None, trade_date: str,
                         input_cutoffs: Mapping[str, str], sealed_at: datetime | None = None) -> list[str]:
    if snapshot_asof is None:
        return list(input_cutoffs)
    sealed_at = sealed_at or datetime.fromisoformat(f"{snapshot_asof}T15:10:00").replace(tzinfo=_ZONE)
    flagged = []
    for tag, value in input_cutoffs.items():
        try:
            cutoff = datetime.fromisoformat(value)
            cutoff = cutoff.replace(tzinfo=_ZONE) if cutoff.tzinfo is None else cutoff.astimezone(_ZONE)
        except (TypeError, ValueError):
            cutoff = datetime.fromisoformat(f"{trade_date}T{tag[:2]}:{tag[2:]}:00").replace(tzinfo=_ZONE)
        if sealed_at > cutoff:
            flagged.append(tag)
    return flagged


def _reject_eod_pipeline_product(path: Path) -> None:
    candidates = [path / "summary.json", path / "manifest.json", path / "day_quality.json"] if path.is_dir() else []
    if not path.is_dir():
        candidates = [path.parent / "summary.json", path.parent / "manifest.json", path.parent / "day_quality.json"]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, Mapping) and str(raw.get("conclusion_phase", "")).upper() == "EOD":
            raise ValueError("EOD pipeline product cannot be used as a 09:20-09:32 session report input")


def _td_connect(host: str):
    try:
        import taos  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("TD query requires the 'taos' Python client") from exc
    database = os.environ.get("TDENGINE_DATABASE", "market_data1")
    if not database.replace("_", "").isalnum():
        raise ValueError("invalid TDENGINE_DATABASE")
    connection = taos.connect(
        host=host, port=int(os.environ.get("TDENGINE_PORT", "6030")),
        user=os.environ.get("TDENGINE_USER", "root"),
        password=os.environ.get("TDENGINE_PASSWORD", "taosdata"), database=database,
        config=os.environ.get("TDENGINE_CONFIG", "/etc/taos"),
    )
    return connection, database


def _read_td(host: str, trade_date: str, symbols: list[str], kline_range: str,
             open_times: tuple[str, ...] = ("0932",)):
    start, end = kline_range.split(":", 1)
    if date.fromisoformat(start) > date.fromisoformat(trade_date) - timedelta(days=1):
        raise ValueError("--kline-range must include dates before trade date")
    if date.fromisoformat(end) < date.fromisoformat(trade_date) - timedelta(days=1):
        raise ValueError("--kline-range must include at least one preceding date")
    if any(not re.fullmatch(r"\d{6}", symbol) for symbol in symbols):
        raise ValueError("snapshot contains invalid stock symbol")
    connection, database = _td_connect(host)
    total_started = time.perf_counter()
    read_seconds = 0.0
    query_count = 0
    compact = trade_date.replace("-", "")
    auctions: dict[str, dict[str, dict[str, Any]]] = {tag: {} for tag in ("0920", "0924", "0925")}
    baseline_auction: dict[str, list[dict[str, Any]]] = {tag: [] for tag in auctions}
    pre_close: dict[str, float | None] = {}
    klines: dict[str, list[tuple[Any, ...]]] = {}
    opening_by_tag: dict[str, dict[str, dict[str, Any]]] = {tag: {} for tag in open_times}

    def fetchall(sql: str):
        nonlocal read_seconds, query_count
        started = time.perf_counter()
        cursor.execute(sql)
        rows = cursor.fetchall()
        read_seconds += time.perf_counter() - started
        query_count += 1
        return rows

    try:
        cursor = connection.cursor()
        for tag in auctions:
            rows = fetchall(
                f"SELECT ts, symbol, px_milli, match_amt_yuan, rest_bid_amt_yuan, rest_ask_amt_yuan, limit_state "
                f'FROM {database}.auction_snapshot_v2 WHERE trade_date="{compact}" AND auction_tag="{tag}"'
            )
            for ts, symbol, px, amount, bid, ask, state in rows:
                ts_dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(int(ts) / 1000, _ZONE)
                ts_dt = ts_dt.replace(tzinfo=_ZONE) if ts_dt.tzinfo is None else ts_dt.astimezone(_ZONE)
                auctions[tag][str(symbol)] = {"ts": ts_dt.isoformat(sep=" "), "px_milli": px, "match_amt_yuan": amount,
                                               "rest_bid_amt_yuan": bid, "rest_ask_amt_yuan": ask,
                                               "limit_state": state}
            lower = (date.fromisoformat(trade_date) - timedelta(days=14)).strftime("%Y%m%d")
            rows = fetchall(
                f"SELECT trade_date, symbol, match_amt_yuan FROM {database}.auction_snapshot_v2 "
                f'WHERE trade_date >= "{lower}" AND trade_date < "{compact}" AND auction_tag="{tag}"'
            )
            grouped: dict[str, dict[str, float | None]] = {}
            for day, symbol, amount in rows:
                day_text = day.date().isoformat() if isinstance(day, datetime) else str(day)
                if len(day_text) == 8 and day_text.isdigit():
                    day_text = f"{day_text[:4]}-{day_text[4:6]}-{day_text[6:]}"
                grouped.setdefault(day_text, {})[str(symbol)] = float(amount) if amount is not None else None
            baseline_auction[tag] = [
                {"trade_date": day, "amount_by_symbol": grouped[day]}
                for day in sorted(grouped)[-5:]
            ]
        if symbols:
            for tag in open_times:
                cutoff = f"{trade_date} {tag[:2]}:{tag[2:]}:00"
                rows = fetchall(
                    "SELECT last_row(px_milli), last_row(amt_yuan), last_row(ts), "
                    "last_row(bp1_milli), last_row(ap1_milli), last_row(bv1), "
                    "last_row(av1), last_row(bv2), last_row(av2), symbol "
                    f"FROM {database}.stock_tick_v2 WHERE ts >= '{trade_date} 09:00:00' "
                    f"AND ts <= '{cutoff}' PARTITION BY symbol"
                )
                for raw in rows:
                    px, amount, ts, bp1, ap1, bv1, av1, bv2, av2, symbol = raw
                    symbol = str(symbol)
                    ts_dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(int(ts) / 1000, _ZONE)
                    ts_dt = ts_dt.replace(tzinfo=_ZONE) if ts_dt.tzinfo is None else ts_dt.astimezone(_ZONE)
                    ts_ms = int(ts_dt.timestamp() * 1000)
                    flags = []
                    if ts_dt.time() < datetime.strptime("09:30:00", "%H:%M:%S").time():
                        state = derive_auction_book(AuctionBookTick(ts_ms, px, bp1, ap1, bv1, av1,
                                                                     bv2, av2, amount, None))
                        price = state.effective_price_milli if state.effective_price_milli > 0 else None
                        flags.append("OPEN_SLICE_BEFORE_0930")
                    else:
                        price = int(px) if px is not None and int(px) > 0 else None
                    opening_by_tag[tag][symbol] = {"ts": ts_dt.isoformat(sep=" "), "px_milli": price,
                                                    "amt_yuan": amount, "flags": flags}

        rows = fetchall(
            f"SELECT ts, open, high, low, close, volume, amount, turnover, pct_chg, tbname "
            f"FROM {database}.daily_kline WHERE ts >= '{start}' AND ts < '{trade_date}'"
        )
        by_symbol: dict[str, list[tuple[Any, ...]]] = {symbol: [] for symbol in symbols}
        for raw in rows:
            ts = raw[0]
            symbol = str(raw[9])
            if symbol.startswith("d_"):
                symbol = symbol[2:]
            if symbol not in by_symbol or ts is None or str(ts)[:10] >= trade_date:
                continue
            row = tuple(raw[:9])
            by_symbol[symbol].append(row)
        for symbol in symbols:
            symbol_rows = sorted(by_symbol[symbol], key=lambda row: str(row[0]))
            klines[symbol] = symbol_rows
            eligible = [row for row in symbol_rows if row[0] is not None and str(row[0])[:10] < trade_date]
            pre_close[symbol] = (float(eligible[-1][4]) if eligible and eligible[-1][4] is not None else None)
    finally:
        connection.close()
    total_seconds = time.perf_counter() - total_started
    timing = {"read_s": read_seconds, "build_s": max(0.0, total_seconds - read_seconds),
              "total_s": total_seconds, "query_count": query_count}
    opening_0932 = opening_by_tag.get("0932", {})
    opening = {symbol: {**row, "_asof_by_tag": {tag: values[symbol] for tag, values in opening_by_tag.items()
                                                  if symbol in values}}
               for symbol, row in opening_0932.items()}
    return auctions, opening, pre_close, klines, baseline_auction, timing


def _load_hot_captures(path: Path | None) -> list[dict[str, Any]]:
    captures = []
    if path is None or not path.is_dir():
        return captures
    for source in sorted(path.glob("hot_*.json")):
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        response = raw.get("response", {}) if isinstance(raw, Mapping) else {}
        rows = response.get("list", []) if isinstance(response, Mapping) else []
        normalized_rows = []
        for rank, row in enumerate(rows, 1):
            if isinstance(row, (list, tuple)) and len(row) >= 4:
                normalized_rows.append({"rank": rank, "name": normalize_plate_name(row[1]),
                                        "strength": row[2], "change_pct": row[3]})
            elif isinstance(row, Mapping):
                normalized_rows.append({"rank": row.get("rank", rank),
                                         "name": normalize_plate_name(row.get("name", row.get("plate", ""))),
                                         "strength": row.get("strength"),
                                         "change_pct": row.get("change_pct", row.get("pct"))})
        captures.append({"captured_local": raw.get("captured_local"),
                         "response": {"Day": response.get("Day"), "Min": response.get("Min"),
                                      "Max": response.get("Max"), "list": normalized_rows}})
    return captures


def _load_prev_hot(path: Path | None, prior_date: str | None) -> dict[str, dict[str, Any]]:
    if path is None or prior_date is None:
        return {}
    source = path / "hot_plates" / f"{prior_date}.json"
    if not source.is_file():
        return {}
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    declared_day = raw.get("Day", raw.get("asof_date")) if isinstance(raw, Mapping) else None
    if isinstance(declared_day, (list, tuple)) and declared_day:  # real shape: ["YYYY-MM-DD"]
        declared_day = declared_day[0]
    if declared_day is not None and str(declared_day).replace("-", "")[:8] != prior_date.replace("-", ""):
        return {}
    rows = raw.get("list", []) if isinstance(raw, Mapping) else []
    result = {}
    for rank, row in enumerate(rows, 1):
        if isinstance(row, (list, tuple)) and len(row) >= 4:
            name, strength, pct = normalize_plate_name(row[1]), row[2], row[3]
        elif isinstance(row, Mapping):
            name = normalize_plate_name(row.get("name", row.get("plate", "")))
            strength, pct = row.get("strength"), row.get("change_pct", row.get("pct"))
            rank = row.get("rank", rank)
        else:
            continue
        if name:
            result[name] = {"rank": rank, "strength": strength, "change_pct": pct}
    return result


def _memberships(snapshot: Mapping[str, Any], symbols: list[str], trade_date: str, prior_date: str | None,
                 kaipan_dir: Path | None, conclusion_phase: str = "AUCTION",
                 reason_rows: Mapping[str, list[dict[str, Any]]] | None = None, reason_items: Any = (),
                 static_relation: Mapping[str, Any] | None = None):
    static = snapshot.get("stock_plate", {})
    s2p = snapshot.get("s2p", {})
    static_themes = {
        symbol: list(dict.fromkeys(
            ([static[symbol]] if static.get(symbol) else [])
            + ([s2p[symbol]] if isinstance(s2p.get(symbol), str) else list(s2p.get(symbol, ())))
        ))
        for symbol in symbols
    }
    if static_relation is not None:
        raw_by_symbol = static_relation.get("by_symbol", {})
        if not isinstance(raw_by_symbol, Mapping):
            raise ValueError("static_relation.by_symbol must be a mapping")
        static_themes = {
            str(symbol): list(dict.fromkeys(map(str, themes)))
            for symbol, themes in raw_by_symbol.items()
            if isinstance(themes, (list, tuple))
        }
    pool_by_symbol: dict[str, Any] = {}
    reasons: Mapping[str, list[Mapping[str, Any]]] = reason_rows or {}
    hot_names: list[str] = []
    degradation: list[str] = []
    hierarchy = default_alias_hierarchy()
    if kaipan_dir is None:
        degradation.append("KAIPAN_INPUTS_MISSING")
    else:
        pools, _kaipan_hierarchy, partial = _load_kaipan_inputs_with_status(
            kaipan_dir, parse_hierarchy=False,
        )
        hot = _load_kaipan_hot_plates(kaipan_dir)
        if prior_date and prior_date in pools:
            pool_by_symbol = pools[prior_date]
        elif prior_date in partial:
            degradation.append("KAIPAN_POOL_PARTIAL")
        else:
            degradation.append("KAIPAN_POOL_MISSING")
        if prior_date and prior_date in hot:
            hot_names = [str(row.get("name")) for row in hot[prior_date].get("rows", ()) if row.get("name")]
        else:
            degradation.append("KAIPAN_HOT_PLATES_MISSING")
        asof_hierarchy = build_hierarchy_as_of(
            reason_items, trade_date,
            inclusive=conclusion_phase == "EOD",
        )
        hierarchy = PlateHierarchy(
            {**hierarchy.parent_by_child, **asof_hierarchy.parent_by_child},
            {**hierarchy.source_by_child, **asof_hierarchy.source_by_child},
            asof_hierarchy.evidence_by_child,
        )
    pool_plates = {symbol: row.plate for symbol, row in pool_by_symbol.items() if row.plate}
    pool_lb = {symbol: row.lb_days for symbol, row in pool_by_symbol.items()}
    reason_candidates: dict[str, list[str]] = {}
    for symbol, items in reasons.items():
        eligible = []
        for item in items:
            raw_date = item.get("Date", item.get("date"))
            eligible_date = (raw_date <= trade_date if conclusion_phase == "EOD" else raw_date < trade_date) \
                if isinstance(raw_date, str) else False
            if eligible_date and str(item.get("Type", item.get("type", "1"))) == "0":
                eligible.append(item)
        if eligible:
            recent = max(eligible, key=lambda row: str(row.get("Date", row.get("date", ""))))
            reason_candidates[symbol] = build_plate_candidates_from_reason(
                reason=str(recent.get("Reason", recent.get("reason", ""))),
                group_str=str(recent.get("Group_Str", recent.get("group_str", ""))),
                gnsm=str(recent.get("GNSM", recent.get("gnsm", ""))),
            )
    memberships = apply_hierarchy(build_memberships(
        symbols=symbols, pool_plate_by_symbol=pool_plates, reason_candidates_by_symbol=reason_candidates,
        static_themes_by_symbol=static_themes, hot_plate_names=hot_names,
        policy=MEMBERSHIP_POLICY_USER_PRIORITY_V1,
    ), hierarchy)
    return memberships, hierarchy, {s: {"lb_days": row.lb_days, "plate": row.plate}
                                    for s, row in pool_by_symbol.items()}, degradation


def _write(path: Path, data: Any):
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def _registry_point_in_time_flag(registry_asof: str, trade_date: str) -> bool:
    try:
        asof = date.fromisoformat(registry_asof)
        trade_day = date.fromisoformat(trade_date)
    except ValueError as exc:
        raise ValueError("registry dates must use YYYY-MM-DD") from exc
    if asof.isoformat() != registry_asof or trade_day.isoformat() != trade_date:
        raise ValueError("registry dates must use YYYY-MM-DD")
    return asof >= trade_day


def _load_registry_dir(registry_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    registry_path = registry_dir / "registry.json"
    relation_path = registry_dir / "relation.csv"
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    if isinstance(raw, Mapping):
        rows = raw.get("plates")
        if rows is None:
            rows = [dict(value, code=key) if isinstance(value, Mapping) and "code" not in value else value
                    for key, value in raw.items()]
    else:
        rows = raw
    if not isinstance(rows, list):
        raise ValueError("registry.json must contain a plate list or a list value")
    registry = {str(row["code"]): dict(row) for row in rows
                if isinstance(row, Mapping) and row.get("code") is not None}
    members: dict[str, set[str]] = {}
    with relation_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"plateid", "stockid"}.issubset(reader.fieldnames or ()):
            raise ValueError("relation.csv must have plateid and stockid columns")
        for row in reader:
            code, symbol = str(row["plateid"]).strip(), str(row["stockid"]).strip()
            if code and symbol:
                members.setdefault(code, set()).add(symbol)
    return registry, members


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _apply_selection_stability(out_by_tag: dict[str, dict[str, Any]], tags: tuple[str, ...]) -> None:
    auction_tags = tuple(tag for tag in ("0920", "0924", "0925") if tag in tags)
    opening_tags = tuple(tag for tag in tags if tag not in auction_tags)
    for stage_tags in (auction_tags, opening_tags):
        previous: set[str] = set()
        runs: dict[str, int] = {}
        for index, tag in enumerate(stage_tags):
            current_rows = out_by_tag.get(tag, {}).get("selected", ())
            current = {str(row.get("code")) for row in current_rows}
            union = previous | current
            jaccard = len(previous & current) / len(union) if union else 1.0
            prior_rows = out_by_tag.get(stage_tags[index - 1], {}).get("selected", ()) if index else ()
            prior_direction = {str(row.get("code")): row.get("direction") for row in prior_rows}
            for row in current_rows:
                code = str(row.get("code"))
                runs[code] = runs.get(code, 0) + 1 if index and code in previous else 1
                row["consecutive_selected"] = runs[code]
                row["previous_direction"] = prior_direction.get(code)
            out_by_tag.setdefault(tag, {})["stability"] = {
                "previous_jaccard": jaccard if index else None,
                "previous_selected_count": len(previous), "selected_count": len(current),
            }
            previous = current


def _attach_independent_plates(report: dict[str, Any], registry_dir: Path, registry_asof: str,
                               trade_date: str, *, reason_rows: Mapping[str, list[dict[str, Any]]] | None = None,
                               pool_by_symbol: Mapping[str, Any] | None = None) -> dict[str, Any]:
    registry, members = _load_registry_dir(registry_dir)
    out_by_tag: dict[str, dict[str, Any]] = {}
    flagged = _registry_point_in_time_flag(registry_asof, trade_date)
    plate_rows = {str(row.get("plate")): row for row in report.get("plates", ())}
    all_symbols = set(members_symbol for group in members.values() for members_symbol in group)
    all_symbols.update(str(row["symbol"]) for row in report.get("stocks", ()) if row.get("symbol"))
    stocks = {str(row["symbol"]): row for row in report.get("stocks", ()) if row.get("symbol")}
    params = {"fdr": 0.10, "min_n": 5, "trim": 0.1, "weight_cap_share": 0.25,
              "max_selected": 20, "min_residual_share": 0.4, "min_residual_n": 8}
    tags = tuple(report.get("input_cutoff", {})) or tuple(
        next(iter(report.get("stocks", ())), {}).get("times", {}).keys())
    for tag in tags:
        returns = {symbol: (stocks.get(symbol, {}).get("times", {}).get(tag, {}).get("pct_vs_pre_close"))
                   for symbol in all_symbols}
        limit_up = {symbol for symbol, stock in stocks.items()
                    if stock.get("times", {}).get(tag, {}).get("limit_state") == 1}
        result = select_independent_plates(returns, members, registry, limit_up_symbols=limit_up)
        selected = []
        for row in result.plates:
            item = dict(row)
            source_plate = plate_rows.get(str(item.get("name")), {})
            stage = source_plate.get("times", {}).get(tag, {})
            item["hot_rank"] = stage.get("hot_live_rank")
            selected.append(item)
        controls = sorted(result.controls.items(), key=lambda item: (-abs(item[1]), item[0]))[:8]
        control_rows = [{"code": code, "name": registry.get(code, {}).get("name") or code, "z": z}
                        for code, z in controls]
        out_by_tag[tag] = _json_safe({
            "market": result.market, "selected": selected, "uncovered": result.uncovered,
            "controls_top": control_rows, "controls": result.controls, "rounds": result.rounds,
            "missing_count": result.missing_count,
            "params": params, "registry_asof": registry_asof,
            "flags": ["NOT_POINT_IN_TIME"] if flagged else [],
        })
        for plate in report.get("plates", ()):
            stage = plate.get("times", {}).get(tag)
            if stage is not None:
                stage["independent"] = out_by_tag[tag]
    _apply_selection_stability(out_by_tag, tags)
    report_stock = {str(row["symbol"]): row for row in report.get("stocks", ()) if row.get("symbol")}
    event_codes = sorted((code for code, row in registry.items() if row.get("class") == "EVENT"),
                         key=lambda code: (-len(members.get(code, ())), code))
    for tag in tags:
        stage_returns = {str(stock["symbol"]): stock.get("times", {}).get(tag, {}).get("pct_vs_pre_close")
                         for stock in report.get("stocks", ()) if stock.get("symbol")}
        valid_market = [value for value in stage_returns.values()
                        if isinstance(value, (int, float)) and not isinstance(value, bool)]
        market_mean = sum(valid_market) / len(valid_market) if valid_market else None
        event_rows = []
        for code in event_codes:
            symbols = sorted(members.get(code, ()))
            values = [(symbol, stage_returns.get(symbol)) for symbol in symbols
                      if isinstance(stage_returns.get(symbol), (int, float))]
            ordered = sorted(values, key=lambda item: (item[1], item[0]))
            trimmed = [value for _, value in ordered]
            cut = int(len(trimmed) * 0.1)
            sample = trimmed[cut:len(trimmed) - cut] if len(trimmed) > 2 * cut else trimmed
            event_rows.append({"code": code, "name": registry[code].get("name") or code,
                "member_count": len(symbols), "covered_count": len(values),
                "trim_mean": sum(sample) / len(sample) if sample else None,
                "limit_up_count": sum(1 for symbol, value in values if (
                    report_stock.get(symbol, {}).get("times", {}).get(tag, {}).get("limit_state") == 1 or
                    (report_stock.get(symbol, {}).get("times", {}).get(tag, {}).get("at_limit_price") is True
                     and value > 0))),
                "top3": [{"symbol": symbol, "name": report_stock.get(symbol, {}).get("name"), "ret": value}
                         for symbol, value in sorted(values, key=lambda item: (-item[1], item[0]))[:3]]})
        control_rows = []
        for code in sorted((c for c, row in registry.items() if row.get("class") == "GENERIC_FACTOR")):
            vals = [stage_returns.get(symbol) for symbol in members.get(code, ())
                    if isinstance(stage_returns.get(symbol), (int, float))]
            delta = sum(vals) / len(vals) - market_mean if vals and market_mean is not None else None
            control_rows.append({"code": code, "name": registry[code].get("name") or code,
                                 "n": len(vals), "relative_market_pp": delta,
                                 "z": out_by_tag.get(tag, {}).get("controls", {}).get(code)})
        out_by_tag.setdefault(tag, {})["event_plates"] = event_rows
        out_by_tag[tag]["generic_controls"] = control_rows
    # Registry feature sets provide the C_s static fallback and evidence flags.
    from engine_core.plate_assignment import FeatureEvidence, build_feature_sets
    from engine_core.plate_stock_labels import label_stock
    registry_payload = {"plates": list(registry.values())}
    code_by_name = {str(row.get("name")): code for code, row in registry.items() if row.get("name")}
    pool_evidence = {str(symbol): code_by_name.get(str(value.get("plate", "")) if isinstance(value, Mapping)
                     else str(getattr(value, "plate", ""))) for symbol, value in (pool_by_symbol or {}).items()}
    recent_reasons: dict[str, list[tuple[str, int]]] = {}
    for symbol, rows in (reason_rows or {}).items():
        eligible = []
        for row in rows:
            day = row.get("Date", row.get("date"))
            if isinstance(day, str) and day < trade_date and str(row.get("Type", row.get("type", "1"))) == "0":
                codes = row.get("ZSCode", row.get("zs_code", ()))
                if isinstance(codes, str):
                    codes = [codes]
                eligible.extend((str(code), (date.fromisoformat(trade_date) - date.fromisoformat(day)).days)
                                for code in codes if str(code) in registry)
        if eligible:
            recent_reasons[str(symbol)] = eligible
    feature_evidence = (FeatureEvidence(yesterday_pool_plates={k: v for k, v in pool_evidence.items() if v},
                        recent_reason_plates=recent_reasons)
                        if pool_evidence or recent_reasons else None)
    feature_sets = build_feature_sets(registry_payload, members, feature_evidence)
    for tag in tags:
        selected = out_by_tag.get(tag, {}).get("selected", ())
        valid_returns = sorted((
            (str(stock["symbol"]), stock.get("times", {}).get(tag, {}).get("pct_vs_pre_close"))
            for stock in report.get("stocks", ()) if stock.get("symbol") and
            isinstance(stock.get("times", {}).get(tag, {}).get("pct_vs_pre_close"), (int, float))),
            key=lambda item: (item[1], item[0]))
        extreme_count = max(1, (len(valid_returns) + 19) // 20) if valid_returns else 0
        bottom_symbols = {symbol for symbol, _ in valid_returns[:extreme_count]}
        bottom_symbols.update(str(stock["symbol"]) for stock in report.get("stocks", ())
                              if stock.get("symbol") and stock.get("times", {}).get(tag, {}).get("limit_state") == -1)
        top_symbols = {symbol for symbol, _ in valid_returns[-extreme_count:]} if extreme_count else set()
        top_symbols.update(str(stock["symbol"]) for stock in report.get("stocks", ())
                           if stock.get("symbol") and stock.get("times", {}).get(tag, {}).get("limit_state") == 1)
        selected_with_members = []
        selected_codes = {str(row.get("code")) for row in selected}
        for code, row in registry.items():
            selected_with_members.append({"code": code, "name": row.get("name"),
                "class": row.get("class"), "members": members.get(code, set()),
                "member_count": len(members.get(code, ())),
                "counter_weak_symbols": top_symbols,
                "counter_strong_symbols": bottom_symbols,
                "direction": next((item.get("direction") for item in selected if str(item.get("code")) == code), None)})
        for stock in report.get("stocks", ()):
            stock_tag = stock.get("times", {}).get(tag, {})
            stock.setdefault("labels", {})[tag] = label_stock(
                stock["symbol"], selected_with_members, feature_sets.get(stock["symbol"]), registry,
                stock_tag.get("pct_vs_pre_close"), stock_tag.get("limit_state") == 1)
    report["independent_by_tag"] = out_by_tag
    if flagged:
        if "NOT_POINT_IN_TIME" not in report.setdefault("degradation", []):
            report["degradation"].append("NOT_POINT_IN_TIME")
    return {
        "registry_dir": str(registry_dir),
        "registry_json_sha256": hashlib.sha256((registry_dir / "registry.json").read_bytes()).hexdigest(),
        "relation_csv_sha256": hashlib.sha256((registry_dir / "relation.csv").read_bytes()).hexdigest(),
        "registry_asof": registry_asof,
        "select_params": params,
        "selected_count_by_stage": {tag: len(row["selected"]) for tag, row in out_by_tag.items()},
        "flags": ["NOT_POINT_IN_TIME"] if flagged else [],
    }


def run_report(*, trade_dates: list[str], snapshot_path: Path, kaipan_dir: Path | None,
               kline_range: str, td_host: str, output_dir: Path, snapshot_asof: str | None = None,
               conclusion_phase: str = "AUCTION", hot_capture_dir: Path | None = None,
               static_relation_path: Path | None = None, registry_dir: Path | None = None,
               registry_asof: str | None = None,
               open_times: tuple[str, ...] = ("0932",)) -> None:
    if conclusion_phase != "AUCTION":
        raise ValueError("session report only supports conclusion_phase=AUCTION")
    _reject_eod_pipeline_product(snapshot_path)
    if kaipan_dir is not None:
        _reject_eod_pipeline_product(kaipan_dir)
    if registry_dir is not None and registry_asof is None:
        raise ValueError("--registry-asof is required with --registry-dir")
    if registry_dir is not None and registry_asof is not None:
        for trade_date in trade_dates:
            _registry_point_in_time_flag(registry_asof, trade_date)
    ensure_empty_output_dir(output_dir)
    snapshot = _load_snapshot(snapshot_path)
    static_relation = None
    static_relation_hash = None
    if static_relation_path is not None:
        static_relation_bytes = static_relation_path.read_bytes()
        static_relation = json.loads(static_relation_bytes)
        if not isinstance(static_relation, Mapping):
            raise ValueError("static relation JSON must contain an object")
        static_relation_hash = hashlib.sha256(static_relation_bytes).hexdigest()
    snapshot_asof = _snapshot_asof(snapshot_path, snapshot_asof, snapshot)
    snapshot_sealed_at = _snapshot_sealed_at(snapshot_path, snapshot_asof, snapshot)
    reason_rows = _load_kaipan_reasons(kaipan_dir) if kaipan_dir else {}
    reason_items = parse_kaipan_reason_items([
        {"StockID": symbol, "List": rows} for symbol, rows in reason_rows.items()
    ])
    symbols = set(map(str, snapshot["stock_plate"])) | set(map(str, snapshot["s2p"]))
    if static_relation is not None:
        by_symbol = static_relation.get("by_symbol", {})
        if not isinstance(by_symbol, Mapping):
            raise ValueError("static_relation.by_symbol must be a mapping")
        symbols.update(map(str, by_symbol))
    symbols = sorted(symbols)
    for trade_date in trade_dates:
        report_started = time.perf_counter()
        auction, opening, pre_close, klines, baseline_auction, timing = _read_td(
            td_host, trade_date, symbols, kline_range, open_times,
        )
        opening_by_tag: dict[str, dict[str, dict[str, Any]]] = {tag: {} for tag in open_times}
        for symbol, row in opening.items():
            tagged_rows = row.get("_asof_by_tag", {}) if isinstance(row, Mapping) else {}
            for tag in open_times:
                if tag in tagged_rows:
                    opening_by_tag[tag][symbol] = tagged_rows[tag]
        if not opening_by_tag.get("0932"):
            opening_by_tag["0932"] = {symbol: row for symbol, row in opening.items()
                                        if isinstance(row, Mapping) and "ts" in row}
        sessions = sorted({str(row[0])[:10] for rows in klines.values() for row in rows if row and row[0] is not None})
        prior = max((day for day in sessions if day < trade_date), default=None)
        memberships, hierarchy, pool, degradation = _memberships(
            snapshot, symbols, trade_date, prior, kaipan_dir, conclusion_phase, reason_rows, reason_items,
            static_relation=static_relation,
        )
        report = build_session_report(
            trade_date=trade_date, snapshot=snapshot, pre_close_by_symbol=pre_close,
            auction_by_tag=auction, opening_by_symbol=opening, opening_by_tag=opening_by_tag,
            open_times=open_times, yest_pool_by_symbol=pool,
            degradation=degradation, memberships=memberships, hierarchy=hierarchy,
            baseline_auction_by_tag=baseline_auction,
            hot_prev_by_plate=_load_prev_hot(kaipan_dir, prior),
            hot_captures=_load_hot_captures(hot_capture_dir),
        )
        independent_manifest = (_attach_independent_plates(report, registry_dir, registry_asof, trade_date,
                                   reason_rows=reason_rows, pool_by_symbol=pool)
                                if registry_dir is not None and registry_asof is not None else None)
        if independent_manifest is not None:
            report["report_md"] = render_report_markdown(report)
        opening_flags = {symbol: set(row.get("flags", ())) for symbol, row in opening.items()}
        static_asof = static_relation.get("asof") if static_relation is not None else None
        static_asof_unknown = static_relation is not None and static_asof is None
        point_in_time_asof = static_asof if static_relation is not None else snapshot_asof
        point_in_time_by_stage = [] if static_asof_unknown else _point_in_time_flags(
            point_in_time_asof, trade_date, report["input_cutoff"],
            snapshot_sealed_at if static_relation is None else None,
        )
        if static_asof_unknown:
            degradation.append("STATIC_ASOF_UNKNOWN")
        if point_in_time_by_stage and "NOT_POINT_IN_TIME" not in degradation:
            degradation.append("NOT_POINT_IN_TIME")
        for stock in report["stocks"]:
            for tag in point_in_time_by_stage:
                stock["times"][tag]["flags"] = sorted(set(stock["times"][tag].get("flags", ())) |
                                                         {"NOT_POINT_IN_TIME"})
        for plate in report["plates"]:
            for tag in point_in_time_by_stage:
                if plate["times"].get(tag) is not None:
                    plate["times"][tag]["flags"] = sorted(set(plate["times"][tag].get("flags", ())) |
                                                            {"NOT_POINT_IN_TIME"})
        for tag in point_in_time_by_stage:
            report["market"][tag]["flags"] = ["NOT_POINT_IN_TIME"]
        report["point_in_time_by_stage"] = point_in_time_by_stage
        report["degradation"] = degradation
        report["report_md"] = render_report_markdown(report)
        if static_asof_unknown:
            report["report_md"] = "标签: STATIC_ASOF_UNKNOWN\n\n" + report["report_md"]
        for tag in point_in_time_by_stage:
            market = report["market"][tag]
            plain_cell = f"| {tag} | {market['covered_count']} |"
            flagged_cell = f"| {tag} (NOT_POINT_IN_TIME) | {market['covered_count']} |"
            report["report_md"] = report["report_md"].replace(plain_cell, flagged_cell, 1)
        for stock in report["stocks"]:
            stock["flags"] = sorted(set(stock.get("flags", ())) | opening_flags.get(stock["symbol"], set()))
        excluded_same_day = sum(
            1 for rows in reason_rows.values() for row in rows
            if row.get("Date", row.get("date")) == trade_date
        ) if conclusion_phase == "AUCTION" else 0
        day_dir = output_dir / trade_date
        day_dir.mkdir()
        _write(day_dir / "stocks.json", report["stocks"])
        plates_payload = ({"plates": report["plates"], "independent": report["independent_by_tag"]}
                          if registry_dir is not None else report["plates"])
        _write(day_dir / "plates.json", plates_payload)
        (day_dir / "report.md").write_text(report["report_md"], encoding="utf-8")
        _write(day_dir / "manifest.json", {
            "trade_date": trade_date, "snapshot": str(snapshot_path), "kaipan_dir": str(kaipan_dir) if kaipan_dir else None,
            "static_source": "kaipan_static_csv" if static_relation is not None else "snapshot_s2p",
            "static_relation": ({"path": str(static_relation_path), "content_hash": static_relation_hash,
                                 "asof": static_asof if static_asof is not None else "UNKNOWN"}
                                if static_relation_path is not None else None),
            "stock_row_count": len(report["stocks"]), "plate_row_count": len(report["plates"]),
            "membership_policy": report["memberships_policy"], "degradation": degradation,
            "snapshot_asof": snapshot_asof, "conclusion_phase": conclusion_phase,
            "snapshot_sealed_at": snapshot_sealed_at.isoformat() if snapshot_sealed_at else None,
            "point_in_time_by_stage": point_in_time_by_stage,
            "input_cutoff": report["input_cutoff"],
            "coverage_count_by_stage": {tag: row["covered_count"] for tag, row in report["market"].items()},
            **({"independent_plates": independent_manifest} if independent_manifest is not None else {}),
            "timing": {"read_s": timing["read_s"],
                       "build_s": max(0.0, time.perf_counter() - report_started - timing["read_s"]),
                       "total_s": time.perf_counter() - report_started},
            "query_count": timing["query_count"],
            "reason_asof_rule": "Date<=D" if conclusion_phase == "EOD" else "Date<D",
            "reason_item_count_excluded_same_day": excluded_same_day,
            "no_close_or_after_inputs": True,
            "sources": ["TD auction_snapshot_v2", "TD stock_tick_v2 last_row as-of each --open-times cutoff", "TD daily kline"],
            "stage_semantics": {"0920": "竞价金额为虚拟匹配额", "0924": "竞价金额为虚拟匹配额",
                                "0925": "竞价金额为虚拟匹配额",
                                **{tag: "真实累计成交额" for tag in open_times}},
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, help="comma-separated YYYY-MM-DD dates")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--static-relation", type=Path, help="optional static relation JSON artifact")
    parser.add_argument("--registry-dir", type=Path, help="dated registry.json and relation.csv directory")
    parser.add_argument("--registry-asof", help="registry point-in-time date YYYY-MM-DD")
    parser.add_argument("--open-times", default=",".join(_DEFAULT_OPEN_TIMES),
                        help="comma-separated opening cutoffs HHMM (default: 0932,0935,0940,0945)")
    parser.add_argument("--snapshot-asof", help="static snapshot date YYYY-MM-DD when its name does not include one")
    parser.add_argument("--conclusion-phase", choices=("AUCTION",), default="AUCTION")
    parser.add_argument("--kaipan-dir", "--prev-hot-dir", dest="kaipan_dir", type=Path)
    parser.add_argument("--hot-capture-dir", type=Path)
    parser.add_argument("--kline-range", required=True, help="bounded YYYY-MM-DD:YYYY-MM-DD range")
    parser.add_argument("--td-host", default="127.0.0.1")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.registry_asof and not args.registry_dir:
        parser.error("--registry-asof requires --registry-dir")
    if args.registry_dir and not args.registry_asof:
        parser.error("--registry-asof is required with --registry-dir")
    try:
        open_times = parse_open_times(args.open_times)
    except ValueError as exc:
        parser.error(str(exc))
    dates = [item.strip() for item in args.trade_date.split(",") if item.strip()]
    for item in dates:
        date.fromisoformat(item)
    if not dates:
        parser.error("--trade-date must contain at least one date")
    run_report(trade_dates=dates, snapshot_path=args.snapshot, kaipan_dir=args.kaipan_dir,
               kline_range=args.kline_range, td_host=args.td_host, output_dir=args.output_dir,
               snapshot_asof=args.snapshot_asof, conclusion_phase=args.conclusion_phase,
               hot_capture_dir=args.hot_capture_dir, static_relation_path=args.static_relation,
               registry_dir=args.registry_dir, registry_asof=args.registry_asof, open_times=open_times)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
