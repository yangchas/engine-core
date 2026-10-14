"""Build a standalone post-close auction-to-close extremes review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from engine_core.extremes_review import (assemble_from_files, build_daily_history, compare_history,
                                         render_daily_history_markdown, render_extremes_markdown)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date")
    parser.add_argument("--report-dir")
    parser.add_argument("--checkpoints-csv")
    parser.add_argument("--auction-snapshot-csv")
    parser.add_argument("--daily-capture-dir")
    parser.add_argument("--daily-history-csv")
    parser.add_argument("--history-from-daily-csv", help="build daily-only historical summaries from this CSV")
    parser.add_argument("--relation-dir")
    parser.add_argument("--stock-caps-json")
    parser.add_argument("--generic-names-json")
    parser.add_argument("--history-dir")
    parser.add_argument("--out-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    daily_only = args.history_from_daily_csv
    if not daily_only and not all((args.trade_date, args.report_dir, args.checkpoints_csv, args.auction_snapshot_csv, args.daily_capture_dir)):
        raise ValueError("live review requires --trade-date, --report-dir, --checkpoints-csv, --auction-snapshot-csv and --daily-capture-dir")
    if daily_only and any((args.report_dir, args.checkpoints_csv, args.auction_snapshot_csv, args.daily_capture_dir)):
        raise ValueError("--history-from-daily-csv is a standalone daily-only mode")
    output = Path(args.out_dir)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError(f"output directory must be new or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if daily_only:
        data = build_daily_history(args.history_from_daily_csv)
        source = Path(args.history_from_daily_csv)
        manifest = {"contract": "ExtremesReviewV1", "mode": "DAILY_ONLY", "source_file": str(source),
                    "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
                    "trade_dates": len(data["days"]), "daily_only_columns": data["daily_only_columns"],
                    "unavailable_intraday_columns": data["unavailable_intraday_columns"]}
        data["manifest"] = manifest
        (output / "extremes.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (output / "extremes_review.md").write_text(render_daily_history_markdown(data), encoding="utf-8")
        print(f"mode=DAILY_ONLY trade_dates={manifest['trade_dates']} out={output}")
        return 0
    data, manifest = assemble_from_files(
        args.trade_date, args.report_dir, args.checkpoints_csv, args.auction_snapshot_csv,
        args.daily_capture_dir, args.daily_history_csv, args.relation_dir, args.stock_caps_json, args.generic_names_json,
    )
    history = compare_history(data, args.history_dir)
    if data.get("daily_history"):
        regime_days = [d for d in data["daily_history"]["days"]
                       if d["regime"] == data.get("regime") and d["trade_date"] != args.trade_date]
        history["same_regime_days"] = len(regime_days)
        history["same_regime_market_median"] = (
            sum(d["summary"]["market_median"] for d in regime_days if d["summary"]["market_median"] is not None) /
            sum(d["summary"]["market_median"] is not None for d in regime_days)
            if any(d["summary"]["market_median"] is not None for d in regime_days) else None)
        loss_rates = [d["summary"]["big_loss_count"] / d["summary"]["n"] for d in regime_days if d["summary"]["n"]]
        history["same_regime_big_loss_rate"] = sum(loss_rates) / len(loss_rates) if loss_rates else None
    data["history"] = history
    (output / "extremes.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "extremes_review.md").write_text(render_extremes_markdown(data, history), encoding="utf-8")
    print(f"trade_date={args.trade_date} stocks={data['summary']['n']} indicative={manifest['indicative_count']} out={output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileExistsError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
