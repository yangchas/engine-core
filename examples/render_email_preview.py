#!/usr/bin/env python3
"""Render a report JSON as a standalone, offline email preview."""
import argparse
import json
from pathlib import Path

from engine_core.email_render import render_email_html


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = json.loads((args.report_dir / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"report.json unavailable ({type(exc).__name__})") from None
    try:
        manifest = json.loads((args.report_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    trade_date = str(report.get("trade_date") or (manifest or {}).get("trade_date") or "未知日期")
    rendered, _ = render_email_html(report, args.stage, trade_date, manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"preview={args.out} bytes={len(rendered.encode('utf-8'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
