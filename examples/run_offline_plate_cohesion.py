"""Read-only TD extraction and offline plate behavior fact report."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from engine_core.plate_cohesion import calculate_plate_cohesion, to_dict
from engine_core.plate_kind import classify_plate_kind
from engine_core.plate_membership import (
    PlateHierarchy, apply_hierarchy, build_memberships, default_alias_hierarchy, members_of,
)
from examples.run_offline_plate_pipeline import _load_snapshot, ensure_empty_output_dir


def _json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                                allow_nan=False) + "\n", encoding="utf-8")


def _memberships(snapshot: Mapping[str, Any]):
    symbols = sorted(set(map(str, snapshot.get("stock_plate", {}))) | set(map(str, snapshot.get("s2p", {}))))
    themes = {
        symbol: list(dict.fromkeys(
            ([snapshot.get("stock_plate", {}).get(symbol)]
             if snapshot.get("stock_plate", {}).get(symbol) else [])
            + list(snapshot.get("s2p", {}).get(symbol, ()))
        ))
        for symbol in symbols
    }
    built = build_memberships(symbols=symbols, pool_plate_by_symbol={}, reason_candidates_by_symbol={},
                              static_themes_by_symbol=themes)
    hierarchy = default_alias_hierarchy()
    return apply_hierarchy(built, hierarchy), hierarchy


def select_top_plates(
    memberships, hierarchy: PlateHierarchy, auction_rows: Mapping[str, Mapping[str, Any]], max_plates: int,
) -> list[str]:
    if max_plates < 1:
        raise ValueError("max_plates must be positive")
    totals: dict[str, float] = {}
    candidates = sorted({row.plate for rows in memberships.by_symbol.values() for row in rows
                         if classify_plate_kind(row.plate) == "THEME"})
    for plate in candidates:
        amount_sum = 0.0
        for symbol in members_of(plate, memberships, hierarchy):
            amount = auction_rows.get(symbol, {}).get("match_amt_yuan")
            if amount is not None:
                try:
                    number = float(amount)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(number):
                    amount_sum += number
        totals[plate] = amount_sum
    return sorted(candidates, key=lambda plate: (-totals[plate], plate))[:max_plates]


def _td_connect(host: str):
    try:
        import taos  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("TD query requires the 'taos' Python client") from exc
    database = os.environ.get("TDENGINE_DATABASE", "market_data1")
    if not database.replace("_", "").isalnum():
        raise ValueError("invalid TDENGINE_DATABASE")
    connection = taos.connect(host=host, port=int(os.environ.get("TDENGINE_PORT", "6030")),
                               user=os.environ.get("TDENGINE_USER", "root"),
                               password=os.environ.get("TDENGINE_PASSWORD", "taosdata"),
                               database=database,
                               config=os.environ.get("TDENGINE_CONFIG", "/etc/taos"))
    return connection, database


def _read_td(host: str, trade_date: str, window_start: str, window_end: str,
             memberships, hierarchy: PlateHierarchy, max_plates: int):
    connection, database = _td_connect(host)
    start = f"{trade_date} {window_start}"
    end = f"{trade_date} {window_end}"
    try:
        cursor = connection.cursor()
        cursor.execute(
            f'SELECT symbol, match_amt_yuan FROM {database}.auction_snapshot_v2 '
            f'WHERE trade_date="{trade_date.replace("-", "")}" AND auction_tag="0925"'
        )
        auction_rows = {str(symbol): {"match_amt_yuan": amount} for symbol, amount in cursor.fetchall()}
        plates = select_top_plates(memberships, hierarchy, auction_rows, max_plates)
        symbols = sorted({symbol for plate in plates for symbol in members_of(plate, memberships, hierarchy)})
        series: dict[str, list[tuple[int, float | None, float | None, float | None]]] = {symbol: [] for symbol in symbols}
        pre_close: dict[str, float | None] = {}
        start_ms = int(datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
        end_ms = int(datetime.strptime(end, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
        if symbols:
            if any(not re.fullmatch(r"\d{6}", symbol) for symbol in symbols):
                raise ValueError("invalid symbol in membership snapshot")
            quoted = ",".join(f'"{symbol}"' for symbol in symbols)
            cursor.execute(
                "SELECT LAST(pc_milli), symbol "
                f"FROM {database}.stock_tick_v2 WHERE ts < '{start}' "
                f"AND symbol IN ({quoted}) GROUP BY symbol"
            )
            for pc, symbol in cursor.fetchall():
                if pc is not None:
                    pre_close[str(symbol)] = float(pc) / 1000
            cursor.execute(
                "SELECT ts, px_milli, pc_milli, amt_yuan, symbol "
                f"FROM {database}.stock_tick_v2 WHERE ts >= '{start}' AND ts <= '{end}' "
                f"AND symbol IN ({quoted}) ORDER BY ts, symbol"
            )
            for ts, px, pc, amount, symbol in cursor.fetchall():
                symbol = str(symbol)
                ts_ms = int(ts.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000) if hasattr(ts, "replace") else int(ts)
                price = float(px) / 1000 if px is not None else None
                close = float(pc) / 1000 if pc is not None else None
                series[symbol].append((ts_ms, price, float(amount) if amount is not None else None, close))
        return plates, series, pre_close, len(auction_rows), sum(map(len, series.values()))
    finally:
        connection.close()


def write_outputs(
    output_dir: Path, *, trade_date: str, snapshot_path: Path, window_start: str, window_end: str,
    max_plates: int, result, selected_plates: Sequence[str], auction_row_count: int,
    tick_row_count: int, series, pre_close,
) -> None:
    ensure_empty_output_dir(output_dir)
    _json_write(output_dir / "cohesion.json", [to_dict(row) for row in result.cohesion])
    _json_write(output_dir / "affinity.json", [to_dict(row) for row in result.affinities])
    missing = {
        "symbols_without_tick_rows": sum(not rows for rows in series.values()),
        "ticks_without_pre_close": sum(
            price is not None and pc is None and symbol not in pre_close
            for symbol, rows in series.items() for row in rows
            for price, pc in ((row[1], row[3] if len(row) >= 4 else None),)
        ),
        "ticks_without_price": sum(row[1] is None for rows in series.values() for row in rows),
        "ticks_without_amount": sum(row[2] is None for rows in series.values() for row in rows),
    }
    _json_write(output_dir / "manifest.json", {
        "trade_date": trade_date, "snapshot": str(snapshot_path), "window_start": window_start,
        "window_end": window_end, "max_plates": max_plates, "selected_plates": list(selected_plates),
        "auction_0925_row_count": auction_row_count,
        "selected_member_count": len(series), "stock_tick_row_count": tick_row_count,
        "missing_counts": missing, "grid_ms": result.grid_ms, "grid_points": result.grid_points,
        "window_ms": 30000, "min_members": 5, "min_points": 10, "max_lag_points": 10,
        "interpretation": "离线事实, 非策略结论",
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--window-start", default="09:20:00")
    parser.add_argument("--window-end", default="09:45:00")
    parser.add_argument("--td-host", default="127.0.0.1")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-plates", type=int, default=30)
    args = parser.parse_args(argv)
    for value in (args.window_start, args.window_end):
        try:
            datetime.strptime(value, "%H:%M:%S")
        except ValueError:
            parser.error("window bounds must use HH:MM:SS")
    if args.window_end <= args.window_start:
        parser.error("window end must be after window start")
    ensure_empty_output_dir(args.output_dir)
    snapshot = _load_snapshot(args.snapshot)
    memberships, hierarchy = _memberships(snapshot)
    selected, series, pre_close, auction_count, tick_count = _read_td(
        args.td_host, args.trade_date, args.window_start, args.window_end,
        memberships, hierarchy, args.max_plates,
    )
    start_ms = int(datetime.strptime(f"{args.trade_date} {args.window_start}", "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    end_ms = int(datetime.strptime(f"{args.trade_date} {args.window_end}", "%Y-%m-%d %H:%M:%S")
                 .replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    result = calculate_plate_cohesion(series, pre_close, memberships, hierarchy,
                                      grid_start_ms=start_ms, grid_end_ms=end_ms)
    # Keep only the selected amount-ranked themes in the report.
    result = type(result)(tuple(row for row in result.cohesion if row.plate in selected),
                          tuple(row for row in result.affinities if row.plate in selected),
                          result.grid_ms, result.grid_points)
    write_outputs(args.output_dir, trade_date=args.trade_date, snapshot_path=args.snapshot,
                  window_start=args.window_start, window_end=args.window_end, max_plates=args.max_plates,
                  result=result, selected_plates=selected, auction_row_count=auction_count,
                  tick_row_count=tick_count, series=series, pre_close=pre_close)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
