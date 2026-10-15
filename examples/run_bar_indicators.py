#!/usr/bin/env python3
"""Build compact rolling bars, turning points, and a self-contained HTML report."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core.auction_tracking import Tick, _int, _load_json, _load_relations, _pool_symbols, _seconds  # noqa: E402
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
    retained: dict[tuple[str, int], tuple[int, int, tuple[int, ...], str, str]] = {}
    count = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for ordinal, row in enumerate(csv.DictReader(handle)):
            if str(row.get("trade_date", "")).strip() != trade_date:
                continue
            ts = str(row.get("ts", ""))[-8:]
            try:
                sec = _seconds(ts)
            except (ValueError, TypeError):
                continue
            if not start <= sec <= end:
                continue
            count += 1
            sample = start + ((sec - start + step - 1) // step) * step
            symbol = str(row.get("symbol", "")).strip().zfill(6)
            if not symbol:
                continue
            key = (symbol, sample)
            old = retained.get(key)
            if old is None or (sec, ordinal) >= (old[0], old[1]):
                retained[key] = (sec, ordinal, tuple(_int(row, k) for k in columns),
                                 str(row.get("name", "")).strip(), ts)
    ticks = [Tick(trade_date=trade_date, ts=ts, symbol=symbol, name=name,
                  **dict(zip(columns, values)))
             for (symbol, _sample), (_sec, _ordinal, values, name, ts) in retained.items()]
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
        opened, open_tick_count = _sampled_ticks(args.open_ticks_csv, args.trade_date, "OPEN")
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
        (args.out_dir / "bars.json").write_text(json.dumps({k: v for k, v in report.items() if k not in ("html", "markdown")}, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.out_dir / "bars_report.md").write_text(report["markdown"], encoding="utf-8")
        (args.out_dir / "bars_report.html").write_text(report["html"], encoding="utf-8")
        (args.out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"trade_date={args.trade_date} ticks={tick_count} sampled_ticks={len(ticks)} open_ticks={open_tick_count} sampled_open_ticks={len(opened)} bars={len(report['bars'])} turning_points={len(report['turning_points'])} json_mode={report['json_mode']}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
