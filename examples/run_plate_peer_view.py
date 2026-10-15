"""Build a local replayable A-to-B plate peer view from report and CSV inputs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from engine_core.plate_peer_view import build_plate_peer_view, render_plate_peer_markdown


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _empty_dir(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("--out-dir must be empty")
    path.mkdir(parents=True, exist_ok=True)


def _pool_symbols(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        rows = payload.get("members", payload.get("stocks", payload))
        if isinstance(rows, dict):
            return {str(symbol).zfill(6) for symbol in rows}
    else:
        rows = payload
    if not isinstance(rows, list):
        return set()
    return {str(row.get("symbol", row.get("code", row.get("stockid", "")))).zfill(6)
            for row in rows if isinstance(row, dict)
            and row.get("symbol", row.get("code", row.get("stockid"))) is not None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--checkpoints-csv", type=Path, required=True)
    parser.add_argument("--auction-snapshot-csv", type=Path, required=True)
    parser.add_argument("--daily-history-csv", type=Path, required=True)
    parser.add_argument("--relation-dir", type=Path, required=True)
    parser.add_argument("--pool-json", type=Path)
    parser.add_argument("--generic-names-json", type=Path, required=True)
    parser.add_argument("--baseline-json", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    _empty_dir(args.out_dir)

    for required in ("stocks.json", "plates.json", "plate_stock_view.json"):
        if not (args.report_dir / required).is_file():
            raise ValueError(f"--report-dir missing required {required}")
    for required in (args.relation_dir / "registry.json", args.relation_dir / "relation.csv"):
        if not required.is_file():
            raise ValueError(f"--relation-dir missing required {required.name}")
    stock_rows = _read_json(args.report_dir / "stocks.json", [])
    plates_report = _read_json(args.report_dir / "plates.json", [])
    if isinstance(plates_report, dict) and isinstance(plates_report.get("plates"), list):
        plates_report = plates_report["plates"]  # real report layout: {"plates": [...], "independent": ...}
    if not isinstance(stock_rows, list) or not isinstance(plates_report, list):
        raise ValueError("report stocks.json and plates.json must contain arrays")
    # Report membership labels are a secondary alias/name source; PIT relation.csv is authoritative.
    plate_names: dict[str, str] = {}
    for row in plates_report if isinstance(plates_report, list) else []:
        name = str(row.get("plate", ""))
        if name:
            plate_names[name] = name
    registry_raw = _read_json(args.relation_dir / "registry.json", [])
    if isinstance(registry_raw, dict):
        registry_rows = registry_raw.get("plates")
        if registry_rows is None:
            registry_rows = [dict(value, code=key) if isinstance(value, dict) and "code" not in value else value
                             for key, value in registry_raw.items()]
    else:
        registry_rows = registry_raw
    for row in registry_rows if isinstance(registry_rows, list) else []:
        if row.get("code") is not None:
            plate_names[str(row["code"])] = str(row.get("name") or row["code"])
    memberships: dict[str, set[str]] = {}
    for row in _read_csv(args.relation_dir / "relation.csv"):
        code, symbol = str(row.get("plateid", "")).strip(), str(row.get("stockid", "")).strip()
        if code and symbol:
            memberships.setdefault(code, set()).add(symbol.zfill(6))
    for row in plates_report if isinstance(plates_report, list) else []:
        plate = str(row.get("plate", ""))
        if not plate_names.get(plate):
            plate_names[plate] = plate
    generic_raw = _read_json(args.generic_names_json, [])
    generic = set(map(str, generic_raw.keys() if isinstance(generic_raw, dict) else generic_raw))
    checkpoints = _read_csv(args.checkpoints_csv)
    auctions = _read_csv(args.auction_snapshot_csv)
    history = _read_csv(args.daily_history_csv)
    baseline = _read_json(args.baseline_json) if args.baseline_json else None
    pool = _read_json(args.pool_json) if args.pool_json else None
    input_paths = [args.report_dir / "stocks.json", args.report_dir / "plates.json",
                   args.report_dir / "plate_stock_view.json",
                   args.checkpoints_csv, args.auction_snapshot_csv, args.daily_history_csv,
                   args.relation_dir / "registry.json", args.relation_dir / "relation.csv",
                   args.generic_names_json]
    if args.pool_json:
        input_paths.append(args.pool_json)
    if args.baseline_json:
        input_paths.append(args.baseline_json)
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in input_paths if path.exists()}
    view = build_plate_peer_view(trade_date=args.trade_date, stocks=stock_rows,
        checkpoints=checkpoints, auctions=auctions, history=history, memberships=memberships,
        plate_names=plate_names, generic_names=generic, baseline=baseline,
        pool_symbols=_pool_symbols(pool) if pool is not None else None, input_hashes=hashes)
    (args.out_dir / "plate_peer_view.json").write_text(
        json.dumps(view, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (args.out_dir / "plate_peer_view.md").write_text(render_plate_peer_markdown(view), encoding="utf-8")
    (args.out_dir / "manifest.json").write_text(
        json.dumps(view["manifest"], ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"A={view['manifest']['a_count']} plates={view['manifest']['plate_count']} peers={view['manifest']['peer_count']} position_missing={view['manifest']['position_missing_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
