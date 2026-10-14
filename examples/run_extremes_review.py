"""Build a standalone post-close auction-to-close extremes review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from engine_core.extremes_review import assemble_from_files, compare_history, render_extremes_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--checkpoints-csv", required=True)
    parser.add_argument("--auction-snapshot-csv", required=True)
    parser.add_argument("--daily-capture-dir", required=True)
    parser.add_argument("--daily-history-csv")
    parser.add_argument("--relation-dir")
    parser.add_argument("--stock-caps-json")
    parser.add_argument("--generic-names-json")
    parser.add_argument("--history-dir")
    parser.add_argument("--out-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.out_dir)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError(f"output directory must be new or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    data, manifest = assemble_from_files(
        args.trade_date, args.report_dir, args.checkpoints_csv, args.auction_snapshot_csv,
        args.daily_capture_dir, args.daily_history_csv, args.relation_dir, args.stock_caps_json, args.generic_names_json,
    )
    history = compare_history(data, args.history_dir)
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
