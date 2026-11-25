#!/usr/bin/env python3
"""Build compact rolling bars, turning points, and a self-contained HTML report."""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core.auction_tracking import Tick, _load_json, _load_relations, _pool_symbols, _seconds  # noqa: E402
from engine_core.bar_indicators import build_bar_report  # noqa: E402


def _sha(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path else None


def _sampled_ticks(path: Path | None, trade_date: str, phase: str) -> tuple[list[Tick], int]:
    """Stream a CSV and retain each symbol's last tick at each report sample."""
    if path is None:
        return [], 0
    start, end, step = ((_seconds("09:15:00"), _seconds("09:25:00"), 30) if phase == "AUCTION"
                        else (_seconds("09:30:00"), _seconds("09:45:00"), 60))
    columns = tuple(k for k in Tick.__dataclass_fields__
                    if k not in {"trade_date", "ts", "symbol", "name"})
    read_started = time.perf_counter()
    available = set(pd.read_csv(path, nrows=0, encoding="utf-8-sig").columns)
    wanted = [name for name in ("trade_date", "ts", "symbol", "name", *columns) if name in available]
    dtype = {name: str for name in ("trade_date", "ts", "symbol", "name") if name in wanted}
    dtype.update({name: "float64" for name in columns if name in wanted})
    if not {"trade_date", "ts", "symbol"}.issubset(available):
        raise ValueError(f"tick CSV missing required columns: {path}")
    retained_chunks = []
    count = 0
    ordinal_base = 0
    reader = pd.read_csv(path, usecols=wanted, dtype=dtype, encoding="utf-8-sig",
                         keep_default_na=False, na_values={name: [""] for name in columns},
                         chunksize=50_000)
    read_elapsed = time.perf_counter() - read_started
    iterator = iter(reader)
    filter_elapsed = 0.0
    while True:
        io_started = time.perf_counter()
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        read_elapsed += time.perf_counter() - io_started
        chunk_filter_started = time.perf_counter()
        dates = chunk["trade_date"].fillna("").str.strip().to_numpy(dtype=str)
        frame = chunk.loc[dates == trade_date].copy()
        if frame.empty:
            ordinal_base += len(chunk)
            filter_elapsed += time.perf_counter() - chunk_filter_started
            continue
        raw_ts = frame["ts"].fillna("").str.strip().str[-8:]
        clock = pd.to_datetime(raw_ts, format="%H:%M:%S", errors="coerce")
        seconds = (clock.dt.hour.to_numpy(dtype="float64") * 3600 +
                   clock.dt.minute.to_numpy(dtype="float64") * 60 +
                   clock.dt.second.to_numpy(dtype="float64"))
        sec_int = np.where(np.isfinite(seconds), seconds, -1).astype(np.int64)
        keep = (sec_int >= start) & (sec_int <= end)
        frame = frame.loc[keep].copy()
        sec_int = sec_int[keep]
        ts_values = raw_ts.to_numpy(dtype=str)[keep]
        if len(frame):
            count += len(frame)
            symbols = frame["symbol"].fillna("").str.strip().str.zfill(6).to_numpy(dtype=str)
            frame["_symbol"] = symbols
            frame["_sec"] = sec_int
            frame["_sample"] = start + ((sec_int - start + step - 1) // step) * step
            frame["_ordinal"] = ordinal_base + frame.index.to_numpy(dtype=np.int64)
            frame["_ts"] = ts_values
            frame = frame.loc[frame["_symbol"] != ""]
            frame = frame.sort_values(["_symbol", "_sample", "_sec", "_ordinal"], kind="stable")
            retained_chunks.append(frame.drop_duplicates(["_symbol", "_sample"], keep="last"))
        ordinal_base += len(chunk)
        filter_elapsed += time.perf_counter() - chunk_filter_started
    frame = pd.concat(retained_chunks, ignore_index=True) if retained_chunks else pd.DataFrame(
        columns=[*wanted, "_symbol", "_sample", "_sec", "_ordinal", "_ts"])
    frame = frame.sort_values(["_symbol", "_sample", "_sec", "_ordinal"], kind="stable")
    frame = frame.drop_duplicates(["_symbol", "_sample"], keep="last")
    conversion_started = time.perf_counter()
    value_arrays = {}
    for name in columns:
        if name in frame:
            # Match the legacy _int parser: blank or malformed values become zero.
            value_arrays[name] = pd.to_numeric(frame[name], errors="coerce").fillna(0).to_numpy(dtype=np.float64).astype(np.int64)
        else:
            value_arrays[name] = np.zeros(len(frame), dtype=np.int64)
    ticks = []
    symbols = frame["_symbol"].to_numpy(dtype=str)
    timestamps = frame["_ts"].to_numpy(dtype=str)
    names = frame["name"].to_numpy(dtype=str) if "name" in frame else np.full(len(frame), "")
    for row_index, symbol in enumerate(symbols):
        values = {name: int(value_arrays[name][row_index]) for name in columns}
        ticks.append(Tick(trade_date=trade_date, ts=timestamps[row_index], symbol=symbol,
                          name=names[row_index].strip(),
                          **values))
    _sampled_ticks.last_timings = {"read_seconds": read_elapsed,
                                   "filter_bucket_seconds": filter_elapsed + time.perf_counter() - conversion_started}
    return ticks, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--ticks-csv", required=True, type=Path)
    parser.add_argument("--open-ticks-csv", type=Path)
    parser.add_argument("--relation-dir", type=Path)
    parser.add_argument("--generic-names-json", type=Path)
    parser.add_argument("--prev-close-json", type=Path)
    parser.add_argument("--baseline-json", type=Path)
    parser.add_argument("--pool-json", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--full-json", action="store_true", help="include all non-focus series in bars.json")
    args = parser.parse_args(argv)
    try:
        end_to_end_started = time.perf_counter()
        for path in (args.ticks_csv, args.open_ticks_csv):
            if path and not path.is_file():
                raise ValueError(f"input does not exist: {path}")
        if args.out_dir.exists() and any(args.out_dir.iterdir()):
            raise ValueError("out-dir must be empty")
        members, names, generic = _load_relations(args.relation_dir)
        if args.generic_names_json:
            raw = _load_json(args.generic_names_json, [])
            generic = set(raw if isinstance(raw, list) else raw.get("names", []))
        prev_close = _load_json(args.prev_close_json, {})
        baseline = _load_json(args.baseline_json, {})
        ticks, tick_count = _sampled_ticks(args.ticks_csv, args.trade_date, "AUCTION")
        auction_timings = dict(_sampled_ticks.last_timings)
        opened, open_tick_count = _sampled_ticks(args.open_ticks_csv, args.trade_date, "OPEN")
        opening_timings = dict(_sampled_ticks.last_timings)
        report = build_bar_report(trade_date=args.trade_date, auction_ticks=ticks, open_ticks=opened,
            relations=members, plate_names=names, generic_names=generic,
            pool_symbols=_pool_symbols(_load_json(args.pool_json, [])), prev_close=prev_close,
            baseline=baseline, full_json=args.full_json)
        hashes = {name: _sha(path) for name, path in {
            "ticks_csv": args.ticks_csv, "open_ticks_csv": args.open_ticks_csv,
            "relation_registry": args.relation_dir / "registry.json" if args.relation_dir else None,
            "relation_csv": args.relation_dir / "relation.csv" if args.relation_dir else None,
            "generic_names_json": args.generic_names_json, "prev_close_json": args.prev_close_json,
            "baseline_json": args.baseline_json, "pool_json": args.pool_json,
        }.items()}
        manifest = {"contract": "BarIndicatorsV1", "trade_date": args.trade_date,
            "input_sha256": hashes, "tick_count": tick_count, "open_tick_count": open_tick_count,
            "sampled_tick_count": len(ticks), "sampled_open_tick_count": len(opened),
            "bar_count": len(report["bars"]), "turning_point_count": len(report["turning_points"]),
            "previous_day_terminal_amount_yuan": report["previous_day_terminal_amount_yuan"],
            "window_minutes": [1, 2, 5, 15],
            "method_note": "Auction amount uses matched-amount increments; from 09:25:00 opening FINAL ticks use amt_yuan increments. Missing values are not zero-filled."}
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "bars.json").write_text(json.dumps({k: v for k, v in report.items() if k not in ("html", "markdown", "_stage_timings")}, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.out_dir / "bars_report.md").write_text(report["markdown"], encoding="utf-8")
        (args.out_dir / "bars_report.html").write_text(report["html"], encoding="utf-8")
        (args.out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        timing = report["_stage_timings"]
        timing["read_seconds"] = auction_timings["read_seconds"] + opening_timings["read_seconds"]
        timing["filter_bucket_seconds"] = auction_timings["filter_bucket_seconds"] + opening_timings["filter_bucket_seconds"]
        timing["render_seconds"] += time.perf_counter() - end_to_end_started - sum(timing.values())
        end_to_end = time.perf_counter() - end_to_end_started
        peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(f"trade_date={args.trade_date} ticks={tick_count} sampled_ticks={len(ticks)} open_ticks={open_tick_count} sampled_open_ticks={len(opened)} bars={len(report['bars'])} turning_points={len(report['turning_points'])} json_mode={report['json_mode']}")
        print("timings_seconds=" + ",".join(f"{key}={timing[key]:.3f}" for key in (
            "read_seconds", "filter_bucket_seconds", "plate_aggregation_seconds",
            "turning_point_seconds", "render_seconds")))
        print(f"end_to_end_seconds={end_to_end:.3f} peak_rss_mb={peak_mb:.1f}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
