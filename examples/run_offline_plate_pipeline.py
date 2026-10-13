"""Build date-scoped offline plate membership and auction facts from TD read-only data."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from engine_core.limit_up_pool_derivation import derive_limit_up_pool_from_daily_klines
from engine_core.plate_aggregation import StockFact, aggregate_plates
from engine_core.plate_membership import (
    MEMBERSHIP_POLICY_LEGACY_V1,
    MEMBERSHIP_POLICY_USER_PRIORITY_V1,
    PlateHierarchy,
    apply_hierarchy,
    build_memberships,
    default_alias_hierarchy,
)
from engine_core.plate_rules import build_plate_candidates_from_reason
from engine_core.stock_plate_assignment import build_stock_plate_assignments


def ensure_empty_output_dir(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"output directory must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _reason_candidates(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for symbol, items in rows.items():
        candidates: list[str] = []
        for row in items:
            candidates.extend(build_plate_candidates_from_reason(
                reason=str(row.get("reason") or ""), group_str=str(row.get("group_str") or ""),
                gnsm=str(row.get("gnsm") or ""),
            ))
        if candidates:
            result[str(symbol)] = list(dict.fromkeys(candidates))
    return result


def _kline_by_day(klines: Mapping[str, Sequence[Sequence[Any]]]) -> dict[str, dict[str, Sequence[Any]]]:
    output: dict[str, dict[str, Sequence[Any]]] = {}
    for symbol, rows in klines.items():
        for row in rows:
            if len(row) == 9 and isinstance(row[0], str):
                day = row[0][:10]
                try:
                    date.fromisoformat(day)
                except ValueError:
                    continue
                output.setdefault(day, {})[str(symbol)] = row
    return output


def _jsonable_memberships(memberships: Any) -> dict[str, Any]:
    return {
        "policy": memberships.policy,
        "rule_version": memberships.rule_version,
        "content_hash": memberships.content_hash,
        "by_symbol": {
            symbol: [
                {"plate": row.plate, "tier": row.tier, "rank_in_stock": row.rank_in_stock,
                 "source": row.source, "generic": row.generic, "hard_generic": row.hard_generic,
                 "ancestors": list(row.ancestors), "flags": list(row.flags)}
                for row in rows
            ] for symbol, rows in sorted(memberships.by_symbol.items())
        },
        "legacy_weights": {symbol: [[plate, weight] for plate, weight in rows]
                           for symbol, rows in sorted(memberships.legacy_weights.items())},
    }


def _jsonable_aggregation(aggregation: Any) -> dict[str, Any]:
    return {
        "content_hash": aggregation.content_hash,
        "rule_version": aggregation.rule_version,
        "market": aggregation.market.__dict__,
        "stats": [
            {**row.__dict__, "tier_counts": {str(k): v for k, v in row.tier_counts.items()},
             "flags": list(row.flags)} for row in aggregation.stats
        ],
    }


def build_day(
    *, trade_date: str, klines_by_symbol: Mapping[str, Sequence[Sequence[Any]]],
    auction_rows: Mapping[str, Mapping[str, Any]], snapshot: Mapping[str, Any],
    reasons_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    reason_asof_unknown: bool, auction_0924_rows: Mapping[str, Mapping[str, Any]] | None = None,
    min_auction_rows: int = 4000,
) -> dict[str, Any]:
    """Pure daily composition; accepts loaded data and performs no I/O."""
    if not isinstance(snapshot.get("stock_plate"), Mapping) or not isinstance(snapshot.get("s2p"), Mapping):
        raise ValueError("snapshot must contain stock_plate and s2p mappings")
    auction_0924_rows = auction_0924_rows or {}
    daily_rows = _kline_by_day(klines_by_symbol)
    sessions = sorted(daily_rows)
    prior_sessions = [day for day in sessions if day < trade_date]
    prior_day = prior_sessions[-1] if prior_sessions else None
    prior_close_rows = daily_rows.get(prior_day, {}) if prior_day else {}
    current_to_previous = {symbol: row for symbol, row in prior_close_rows.items()}

    pool_rows: tuple[dict[str, Any], ...] = ()
    pool_symbols: set[str] = set()
    derivation_hash = None
    if prior_day:
        derivation = derive_limit_up_pool_from_daily_klines(
            klines_by_symbol, prior_day, trading_dates=sessions,
        )
        pool_rows = derivation.rows
        pool_symbols = {str(row["symbol"]) for row in pool_rows}
        derivation_hash = derivation.content_hash

    static_assignment = build_stock_plate_assignments(
        trade_date=trade_date,
        static_primary_by_symbol=snapshot["stock_plate"],
        static_themes_by_symbol=snapshot["s2p"],
    )
    static_themes: dict[str, list[str]] = {}
    static_primary: dict[str, str] = {}
    for symbol, assignment in static_assignment.assignments.items():
        if assignment.primary_plate:
            static_primary[symbol] = assignment.primary_plate
        static_themes[symbol] = list(dict.fromkeys(
            ([assignment.primary_plate] if assignment.primary_plate else []) + list(assignment.themes)
        ))

    reason_candidates = _reason_candidates(reasons_by_symbol)
    pool_plates = {str(row["symbol"]): row["plate"] for row in pool_rows if row.get("plate")}
    symbols = set(map(str, auction_rows)) | set(static_assignment.assignments) | set(pool_symbols) | set(reason_candidates)
    hierarchy = default_alias_hierarchy()
    memberships = apply_hierarchy(build_memberships(
        symbols=symbols, pool_plate_by_symbol=pool_plates,
        reason_candidates_by_symbol=reason_candidates,
        static_themes_by_symbol=static_themes, policy=MEMBERSHIP_POLICY_USER_PRIORITY_V1,
    ), hierarchy)
    legacy = build_memberships(
        symbols=symbols, pool_plate_by_symbol=pool_plates,
        reason_candidates_by_symbol=reason_candidates,
        static_themes_by_symbol=static_themes, policy=MEMBERSHIP_POLICY_LEGACY_V1,
    )

    facts: dict[str, StockFact] = {}
    missing = Counter()
    for symbol, raw in auction_rows.items():
        prior = current_to_previous.get(str(symbol))
        pre_close = _number(prior[4]) if prior is not None else None
        if pre_close is not None and pre_close <= 0:
            pre_close = None
        px_milli = _number(raw.get("px_milli"))
        price = px_milli / 1000.0 if px_milli is not None and px_milli > 0 else None
        amount = _number(raw.get("match_amt_yuan"))
        state_value = _number(raw.get("limit_state"))
        state = int(state_value) if state_value is not None and state_value.is_integer() else None
        amount_prev = _number(auction_0924_rows.get(str(symbol), {}).get("match_amt_yuan"))
        for key, value in (("pre_close", pre_close), ("price", price), ("match_amt_yuan", amount),
                           ("limit_state", state), ("amount_prev", amount_prev)):
            if value is None:
                missing[key] += 1
        facts[str(symbol)] = StockFact(
            symbol=str(symbol), pre_close=pre_close, price=price, amount=amount,
            limit_state=state, lb_days=None, in_yest_pool=str(symbol) in pool_symbols,
            amount_prev=amount_prev,
        )

    aggregation = aggregate_plates(memberships, hierarchy, facts)
    membership_primary = {symbol: rows[0].plate for symbol, rows in memberships.by_symbol.items() if rows}
    legacy_primary = {symbol: rows[0].plate for symbol, rows in legacy.by_symbol.items() if rows}
    comparable = set(membership_primary) & set(legacy_primary)
    equal_count = sum(membership_primary[symbol] == legacy_primary[symbol] for symbol in comparable)
    no_plate = sum(not memberships.by_symbol.get(str(symbol)) for symbol in auction_rows)
    generic_count = sum(row.generic for row in aggregation.stats)
    quality = {
        "trade_date": trade_date,
        "auction_row_count": len(auction_rows), "membership_symbol_count": len(memberships.by_symbol),
        "yest_pool_member_count": len(pool_symbols), "mapped_auction_symbol_count": len(auction_rows) - no_plate,
        "unmapped_auction_symbol_count": no_plate,
        "auction_coverage": (len(auction_rows) - no_plate) / len(auction_rows) if auction_rows else None,
        "membership_clipped_count": 0,
        "generic_plate_count": generic_count, "plate_count": len(aggregation.stats),
        "generic_plate_share": generic_count / len(aggregation.stats) if aggregation.stats else None,
        "missing_counts": dict(sorted(missing.items())),
        "previous_kline_date": prior_day, "pool_derivation_hash": derivation_hash,
        "legacy_v1_primary_match_count": equal_count,
        "legacy_v1_primary_comparable_count": len(comparable),
        "legacy_v1_primary_match_rate": equal_count / len(comparable) if comparable else None,
    }
    return {"memberships": memberships, "aggregation": aggregation, "quality": quality,
            "reason_asof_unknown": reason_asof_unknown, "yest_pool_symbols": pool_symbols}


def run_pipeline(
    *, dates: Sequence[str], klines_by_symbol: Mapping[str, Sequence[Sequence[Any]]],
    auction_by_date: Mapping[str, Mapping[str, Mapping[str, Any]]], snapshot: Mapping[str, Any],
    reasons_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]], start_date: str, end_date: str,
    reason_asof_unknown: bool = False, auction_0924_by_date: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    min_auction_rows: int = 4000,
) -> dict[str, Any]:
    included, skipped = [], []
    for day in dates:
        if not start_date <= day <= end_date:
            continue
        auction = auction_by_date.get(day, {})
        if len(auction) < min_auction_rows:
            skipped.append({"date": day, "reason": "auction_row_count_below_minimum", "auction_row_count": len(auction)})
            continue
        included.append((day, build_day(
            trade_date=day, klines_by_symbol=klines_by_symbol, auction_rows=auction, snapshot=snapshot,
            reasons_by_symbol=reasons_by_symbol, reason_asof_unknown=reason_asof_unknown,
            auction_0924_rows=(auction_0924_by_date or {}).get(day, {}), min_auction_rows=min_auction_rows,
        )))
    return {"days": included, "skipped": skipped}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_snapshot(path: Path) -> dict[str, Any]:
    if path.is_dir():
        candidates = (path / "legacy_snapshot.json", path / "snapshot.json")
        for candidate in candidates:
            if candidate.exists():
                raw = _read_json(candidate)
                if "stock_plate" in raw and "s2p" in raw:
                    return raw
        stock_path, s2p_path = path / "stock_plate.json", path / "s2p.json"
        if stock_path.exists() and s2p_path.exists():
            return {"stock_plate": _read_json(stock_path), "s2p": _read_json(s2p_path)}
        raise ValueError("snapshot directory must contain stock_plate.json and s2p.json or legacy_snapshot.json")
    raw = _read_json(path)
    if "stock_plate" not in raw or "s2p" not in raw:
        raise ValueError("snapshot JSON must contain stock_plate and s2p")
    return raw


def _load_reasons(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {}
    raw = _read_json(path)
    if isinstance(raw, Mapping):
        return {str(symbol): list(rows) if isinstance(rows, list) else [rows]
                for symbol, rows in raw.items()}
    result: dict[str, list[dict[str, Any]]] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        symbol = row.get("symbol") or row.get("field")
        if symbol is not None:
            value = row.get("value")
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except ValueError:
                    decoded = {"reason": value}
                row = decoded if isinstance(decoded, Mapping) else {"reason": value}
            result.setdefault(str(symbol), []).append(dict(row))
    return result


def _connect_td(host: str):
    try:
        import taos  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("TD query requires the 'taos' Python client") from exc
    database = os.environ.get("TDENGINE_DATABASE", "market_data1")
    if not database.replace("_", "").isalnum():
        raise ValueError("invalid TDENGINE_DATABASE")
    return taos.connect(
        host=host, port=int(os.environ.get("TDENGINE_PORT", "6030")),
        user=os.environ.get("TDENGINE_USER", "root"), password=os.environ.get("TDENGINE_PASSWORD", "taosdata"),
        database=database, config=os.environ.get("TDENGINE_CONFIG", "/etc/taos"),
    ), database


def _read_td( host: str, database: str, symbols: Sequence[str], kline_start: str, kline_end: str,
              auction_dates: Sequence[str]) -> tuple[dict[str, list[Sequence[Any]]], dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, dict[str, Any]]]]:
    connection, actual_database = _connect_td(host)
    if database != actual_database:
        raise ValueError("TD database configuration changed during connection")
    try:
        cursor = connection.cursor()
        klines: dict[str, list[Sequence[Any]]] = {}
        for symbol in symbols:
            if not re.fullmatch(r"\d{6}", symbol):
                continue
            table = "d_" + symbol
            cursor.execute(
                f"SELECT ts, open, high, low, close, volume, amount, turnover, pct_chg FROM {table} "
                f"WHERE ts >= '{kline_start}' AND ts <= '{kline_end}' ORDER BY ts"
            )
            klines[symbol] = list(cursor.fetchall())
        auction: dict[str, dict[str, dict[str, Any]]] = {}
        previous: dict[str, dict[str, dict[str, Any]]] = {}
        for day in auction_dates:
            compact = day.replace("-", "")
            for tag, target in (("0925", auction), ("0924", previous)):
                cursor.execute(
                    "SELECT symbol, px_milli, match_amt_yuan, limit_state "
                    f'FROM {actual_database}.auction_snapshot_v2 WHERE trade_date="{compact}" AND auction_tag="{tag}"'
                )
                target[day] = {
                    str(symbol): {"px_milli": px, "match_amt_yuan": amount, "limit_state": state}
                    for symbol, px, amount, state in cursor.fetchall()
                }
        return klines, auction, previous
    finally:
        connection.close()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _legacy_pool_symbols(path: Path, day: str) -> set[str] | None:
    candidates = (path / f"{day}.json", path / day / "yest_limit_pool.json",
                  path / day / "legacy_pool.json", path / "yest_limit_pool.json")
    source = next((candidate for candidate in candidates if candidate.exists()), None)
    if source is None:
        return None
    raw = _read_json(source)
    if isinstance(raw, Mapping):
        raw = raw.get("rows", raw.get("yest_limit_pool", list(raw.values())))
    symbols: set[str] = set()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for row in raw:
            if isinstance(row, Mapping) and row.get("symbol") is not None:
                symbols.add(str(row["symbol"]))
            elif isinstance(row, str):
                symbols.add(row)
    return symbols


def _emit(output_dir: Path, result: Mapping[str, Any], args: argparse.Namespace) -> None:
    daily_rows = result["days"]
    plate_days: Counter[str] = Counter()
    hashes: dict[str, str] = {}
    unmapped: dict[str, int] = {}
    same_matches = same_comparable = 0
    legacy_pool_comparison: dict[str, Any] = {}
    for day, value in daily_rows:
        folder = output_dir / day
        folder.mkdir()
        membership_payload = _jsonable_memberships(value["memberships"])
        membership_payload["yest_pool_symbols"] = sorted(value["yest_pool_symbols"])
        membership_payload["yest_pool_members"] = {
            symbol: {"tier": 1, "source": "td_daily_kline", "plate": None}
            for symbol in sorted(value["yest_pool_symbols"])
        }
        membership_payload["engine_membership_content_hash"] = membership_payload["content_hash"]
        canonical_membership_payload = json.dumps(
            membership_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        membership_payload["content_hash"] = hashlib.sha256(canonical_membership_payload.encode("utf-8")).hexdigest()
        _write_json(folder / "memberships.json", membership_payload)
        _write_json(folder / "plates.json", _jsonable_aggregation(value["aggregation"]))
        quality = dict(value["quality"])
        quality["reason_asof_unknown"] = value["reason_asof_unknown"]
        _write_json(folder / "day_quality.json", quality)
        hashes[day] = membership_payload["content_hash"]
        unmapped[day] = quality["unmapped_auction_symbol_count"]
        same_matches += quality["legacy_v1_primary_match_count"]
        same_comparable += quality["legacy_v1_primary_comparable_count"]
        for plate in {row.plate for rows in value["memberships"].by_symbol.values() for row in rows}:
            plate_days[plate] += 1
        legacy_dir = getattr(args, "legacy_pool_dir", None)
        if legacy_dir is not None:
            legacy_symbols = _legacy_pool_symbols(legacy_dir, day)
            if legacy_symbols is None:
                legacy_pool_comparison[day] = {"status": "MISSING_LEGACY_FILE"}
            else:
                derived = value["yest_pool_symbols"]
                legacy_pool_comparison[day] = {
                    "status": "COMPARED", "legacy_member_count": len(legacy_symbols),
                    "derived_member_count": value["quality"]["yest_pool_member_count"],
                    "intersection_count": len(derived & legacy_symbols) if derived else None,
                }
    summary = {
        "start_date": args.start_date, "end_date": args.end_date,
        "included_dates": [day for day, _ in daily_rows], "skipped_dates": result["skipped"],
        "membership_content_hash_by_date": hashes,
        "plate_appearance_days": dict(sorted(plate_days.items())),
        "legacy_v1_primary_match_count": same_matches,
        "legacy_v1_primary_comparable_count": same_comparable,
        "legacy_v1_primary_match_rate": same_matches / same_comparable if same_comparable else None,
        "unmapped_auction_symbol_count_by_date": unmapped,
        "legacy_pool_comparison": legacy_pool_comparison if getattr(args, "legacy_pool_dir", None) else None,
        "interpretation": "离线事实, 非策略结论",
        "reason_asof_unknown": bool(args.reasons),
    }
    _write_json(output_dir / "summary.json", summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--reasons", type=Path)
    parser.add_argument("--kline-range", required=True, help="bounded TD date range YYYY-MM-DD:YYYY-MM-DD")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--td-host", default="127.0.0.1")
    parser.add_argument("--legacy-pool-dir", type=Path, help="optional read-only comparison input (reserved)")
    args = parser.parse_args(argv)
    start, end = date.fromisoformat(args.start_date), date.fromisoformat(args.end_date)
    try:
        kline_start_text, kline_end_text = args.kline_range.split(":", 1)
        kline_start, kline_end = date.fromisoformat(kline_start_text), date.fromisoformat(kline_end_text)
    except (ValueError, TypeError):
        parser.error("--kline-range must be YYYY-MM-DD:YYYY-MM-DD")
    if start > end or kline_start > kline_end or kline_start > start - timedelta(days=1) or kline_end < end:
        parser.error("date range must be ordered and kline range must cover a prior session through end date")
    ensure_empty_output_dir(args.output_dir)
    snapshot = _load_snapshot(args.snapshot)
    reasons = _load_reasons(args.reasons)
    symbols = sorted(set(map(str, snapshot["stock_plate"])) | set(map(str, snapshot["s2p"])) | set(reasons))
    auction_dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    # Confirm database value before opening and reuse that same value for the query.
    klines, auctions, previous = _read_td(args.td_host, os.environ.get("TDENGINE_DATABASE", "market_data1"),
                                           symbols, kline_start.isoformat(), kline_end.isoformat(), auction_dates)
    result = run_pipeline(
        dates=auction_dates, klines_by_symbol=klines, auction_by_date=auctions, snapshot=snapshot,
        reasons_by_symbol=reasons, start_date=args.start_date, end_date=args.end_date,
        reason_asof_unknown=args.reasons is not None, auction_0924_by_date=previous,
    )
    _emit(args.output_dir, result, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
