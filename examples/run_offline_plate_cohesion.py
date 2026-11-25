"""Read-only TD extraction and offline plate behavior fact report."""

from __future__ import annotations

import argparse
from datetime import datetime
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from engine_core.auction_book import AuctionBookTick, derive_auction_book
from engine_core.plate_cohesion import calculate_plate_cohesion, to_dict
from engine_core.plate_cohesion_phases import compute_phase_cohesion
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
        amount_snapshots: dict[str, dict[str, float | None]] = {}
        price_snapshots: dict[str, dict[str, float | None]] = {}
        for tag in ("0920", "0924", "0925"):
            cursor.execute(
                f'SELECT symbol, px_milli, match_amt_yuan FROM {database}.auction_snapshot_v2 '
                f'WHERE trade_date="{trade_date.replace("-", "")}" AND auction_tag="{tag}"'
            )
            fetched = cursor.fetchall()
            amount_snapshots[tag] = {str(symbol): float(amount) if amount is not None else None
                                     for symbol, _, amount in fetched}
            price_snapshots[tag] = {str(symbol): float(px) / 1000 if px is not None else None
                                    for symbol, px, _ in fetched}
        auction_rows = {symbol: {"match_amt_yuan": amount}
                        for symbol, amount in amount_snapshots["0925"].items()}
        plates = select_top_plates(memberships, hierarchy, auction_rows, max_plates)
        symbols = sorted({symbol for plate in plates for symbol in members_of(plate, memberships, hierarchy)})
        series: dict[str, list[tuple]] = {symbol: [] for symbol in symbols}
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
                "SELECT ts, px_milli, pc_milli, amt_yuan, vol_units, "
                "bp1_milli, ap1_milli, bv1, av1, bv2, av2, symbol "
                f"FROM {database}.stock_tick_v2 WHERE ts >= '{start}' AND ts <= '{end}' "
                f"AND symbol IN ({quoted}) ORDER BY ts, symbol"
            )
            for ts, px, pc, amount, volume, bp1, ap1, bv1, av1, bv2, av2, symbol in cursor.fetchall():
                symbol = str(symbol)
                ts_ms = int(ts.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000) if hasattr(ts, "replace") else int(ts)
                state = derive_auction_book(AuctionBookTick(
                    ts_ms, px, bp1, ap1, bv1, av1, bv2, av2, amount, volume,
                ))
                price = state.effective_price_milli / 1000 if state.effective_price_milli > 0 else None
                close = float(pc) / 1000 if pc is not None else None
                local = datetime.fromtimestamp(ts_ms / 1000, ZoneInfo("Asia/Shanghai"))
                sub_phase = ("CANCELLABLE" if (local.hour, local.minute) < (9, 20) else "LOCKED") \
                    if state.phase == "CALL" else None
                series[symbol].append((ts_ms, price, float(state.match_amt_yuan), close,
                                       state.rest_bid_amt_yuan, state.rest_ask_amt_yuan,
                                       state.imbalance_side, sub_phase, state.matched_volume,
                                       state.phase, state.flags))
        return (plates, series, pre_close, len(auction_rows), sum(map(len, series.values())),
                amount_snapshots, price_snapshots)
    finally:
        connection.close()


def write_outputs(
    output_dir: Path, *, trade_date: str, snapshot_path: Path, window_start: str, window_end: str,
    max_plates: int, result, selected_plates: Sequence[str], auction_row_count: int,
    tick_row_count: int, series, pre_close, phase_result=None, phases=None,
) -> None:
    ensure_empty_output_dir(output_dir)
    if phase_result is None:
        _json_write(output_dir / "cohesion.json", [to_dict(row) for row in result.cohesion])
        _json_write(output_dir / "affinity.json", [to_dict(row) for row in result.affinities])
    else:
        for phase, rows in phase_result.by_phase.items():
            suffix = phase.lower()
            _json_write(output_dir / f"cohesion_{suffix}.json", [to_dict(row) for row in rows])
            _json_write(output_dir / f"affinity_{suffix}.json",
                        [to_dict(row) for row in phase_result.affinities_by_phase.get(phase, ())])
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
        "missing_counts": missing, "grid_ms": result.grid_ms if result is not None else 3000,
        "grid_points": result.grid_points if result is not None else sum(
            phase_result.flags["phase_sample_counts"].values()) if phase_result is not None else 0,
        "window_ms": 30000, "min_members": 5, "min_points": 10, "max_lag_points": 10,
        "interpretation": "离线事实, 非策略结论",
    })
    if phase_result is not None:
        manifest_path = output_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["phases"] = list(phases or phase_result.by_phase)
        manifest["phase_sample_counts"] = phase_result.flags["phase_sample_counts"]
        manifest["phase_missing_counts"] = {
            phase: {"plate_uncovered_members": sum(max(0, row.member_count - row.covered_count)
                                                    for row in rows)}
            for phase, rows in phase_result.by_phase.items()
        }
        manifest["gap_skipped_points"] = phase_result.flags["gap_skipped_points"]
        manifest["gap"] = phase_result.flags["gap"]
        manifest["content_hash"] = phase_result.content_hash
        manifest["snapshot_crosscheck"] = phase_result.flags.get("snapshot_crosscheck", {})
        manifest["auction_sub_phases"] = phase_result.flags.get("auction_sub_phases", {})
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                            indent=2, allow_nan=False) + "\n", encoding="utf-8")
    else:
        manifest_path = output_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["legacy_window_semantics"] = "PHASE_MIXED_LEGACY_WINDOW"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                            indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    parser.add_argument("--phases", default="auction,call_result,open",
                        help="comma-separated phases: auction,call_result,open")
    parser.add_argument("--td-host", default="127.0.0.1")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-plates", type=int, default=30)
    args = parser.parse_args(argv)
    legacy_window = args.window_start is not None or args.window_end is not None
    window_start = args.window_start or "09:15:00"
    window_end = args.window_end or "09:45:00"
    for value in (window_start, window_end):
        try:
            datetime.strptime(value, "%H:%M:%S")
        except ValueError:
            parser.error("window bounds must use HH:MM:SS")
    if window_end <= window_start:
        parser.error("window end must be after window start")
    phases = tuple(part.strip().upper() for part in args.phases.split(",") if part.strip())
    if not phases or any(phase not in ("AUCTION", "CALL_RESULT", "OPEN") for phase in phases):
        parser.error("--phases must contain auction, call_result, and/or open")
    ensure_empty_output_dir(args.output_dir)
    snapshot = _load_snapshot(args.snapshot)
    memberships, hierarchy = _memberships(snapshot)
    selected, series, pre_close, auction_count, tick_count, amount_snapshots, price_snapshots = _read_td(
        args.td_host, args.trade_date, window_start, window_end,
        memberships, hierarchy, args.max_plates,
    )
    start_ms = int(datetime.strptime(f"{args.trade_date} {window_start}", "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    end_ms = int(datetime.strptime(f"{args.trade_date} {window_end}", "%Y-%m-%d %H:%M:%S")
                 .replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    if legacy_window:
        result = calculate_plate_cohesion(series, pre_close, memberships, hierarchy,
                                          grid_start_ms=start_ms, grid_end_ms=end_ms)
        result = type(result)(tuple(row for row in result.cohesion if row.plate in selected),
                              tuple(row for row in result.affinities if row.plate in selected),
                              result.grid_ms, result.grid_points)
        write_outputs(args.output_dir, trade_date=args.trade_date, snapshot_path=args.snapshot,
                      window_start=window_start, window_end=window_end, max_plates=args.max_plates,
                      result=result, selected_plates=selected, auction_row_count=auction_count,
                      tick_row_count=tick_count, series=series, pre_close=pre_close)
    else:
        phase_result = compute_phase_cohesion(
            series, pre_close, memberships, hierarchy, amount_snapshots=amount_snapshots,
            price_snapshots=price_snapshots, phases=phases)
        phase_result = type(phase_result)(
            {phase: tuple(row for row in rows if row.plate in selected)
             for phase, rows in phase_result.by_phase.items()},
            {phase: tuple(row for row in rows if row.plate in selected)
             for phase, rows in phase_result.affinities_by_phase.items()},
            phase_result.flags, phase_result.content_hash)
        hash_payload = {
            "by_phase": {phase: [asdict(row) for row in rows]
                         for phase, rows in phase_result.by_phase.items()},
            "affinities_by_phase": {phase: [asdict(row) for row in rows]
                                    for phase, rows in phase_result.affinities_by_phase.items()},
            "flags": phase_result.flags,
        }
        digest = hashlib.sha256(json.dumps(hash_payload, sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
        phase_result = type(phase_result)(phase_result.by_phase, phase_result.affinities_by_phase,
                                          phase_result.flags, digest)
        write_outputs(args.output_dir, trade_date=args.trade_date, snapshot_path=args.snapshot,
                      window_start=window_start, window_end=window_end, max_plates=args.max_plates,
                      result=None, selected_plates=selected, auction_row_count=auction_count,
                      tick_row_count=tick_count, series=series, pre_close=pre_close,
                      phase_result=phase_result, phases=phases)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
