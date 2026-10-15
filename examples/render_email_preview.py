#!/usr/bin/env python3
"""Render a report JSON as a standalone, offline email preview."""
import argparse
import json
import statistics
from pathlib import Path

from engine_core.daily_pipeline import _load_ladder_summary
from engine_core.email_render import render_email_html


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _enrich_close_report(report, reference_root):
    stocks = report.get("stocks")
    if not isinstance(stocks, list):
        return
    by_code = {str(row.get("symbol")): row for row in stocks if isinstance(row, dict)}
    auction_total = sum(row.get("auction_amount_yuan") for row in stocks
                        if isinstance(row, dict) and isinstance(row.get("auction_amount_yuan"), (int, float)))
    for row in stocks:
        if not isinstance(row, dict):
            continue
        amount = row.get("auction_amount_yuan")
        if row.get("auction_amount_bp") is None:
            row["auction_amount_bp"] = amount / auction_total * 10_000 if isinstance(amount, (int, float)) and auction_total else None
        if row.get("visibility_half_limit") is None:
            band, auction = row.get("limit_pct"), row.get("auction_pct", row.get("p0925_pct"))
            p932, p945 = row.get("p0932_pct"), row.get("p0945_pct")
            if not all(isinstance(value, (int, float)) for value in (band, auction, p932, p945)) or not band:
                row["visibility_half_limit"] = "不可判断：板幅或检查点缺失"
            else:
                threshold = .5 if row.get("is_big_meat") else -.5
                reached = [((value - auction) / band >= threshold if threshold > 0
                            else (value - auction) / band <= threshold) for value in (p932, p945)]
                row["visibility_half_limit"] = ("09:32前已达0.5L" if reached[0] else
                    "09:32至09:45达到0.5L" if reached[1] else "09:45前未达0.5L")
    for group in ("big_meat", "big_loss"):
        for row in report.get(group, []) if isinstance(report.get(group), list) else []:
            source = by_code.get(str(row.get("symbol"))) if isinstance(row, dict) else None
            if source:
                for key in ("auction_amount_bp", "visibility_half_limit"):
                    row.setdefault(key, source.get(key))
    if not report.get("plate_evolution"):
        meat = report.get("big_meat") if isinstance(report.get("big_meat"), list) else []
        loss = report.get("big_loss") if isinstance(report.get("big_loss"), list) else []
        focus = {str(row.get("symbol")) for row in meat[:8] + loss[:8] if row.get("symbol")}
        by_plate = {}
        for row in stocks:
            if not isinstance(row, dict):
                continue
            names = row.get("plates") or ([row.get("main_plate")] if row.get("main_plate") else [])
            for name in names:
                by_plate.setdefault(str(name), []).append(row)
        evolution = []
        for plate, members in by_plate.items():
            for row in members:
                symbol = str(row.get("symbol"))
                if symbol not in focus:
                    continue
                trajectory = {}
                for output, key in (("p0925_pct", "p0925_pct"), ("p0932_pct", "p0932_pct"),
                                    ("p0945_pct", "p0945_pct"), ("close_pct", "close_pct")):
                    values = [member.get(key) for member in members if isinstance(member.get(key), (int, float))]
                    trajectory[output] = statistics.fmean(values) if values else None
                relative = row.get("close_pct") - trajectory["close_pct"] if (
                    isinstance(row.get("close_pct"), (int, float)) and trajectory["close_pct"] is not None) else None
                peers = sorted((member for member in members if member.get("symbol") != row.get("symbol")
                                and isinstance(member.get("close_pct"), (int, float))),
                               key=lambda member: (-member["close_pct"], str(member.get("symbol"))))[:3]
                evolution.append({"symbol": symbol, "name": row.get("name"), "plate": plate, **trajectory,
                                  "stock_close_pct": row.get("close_pct"), "relative_to_plate_pp": relative,
                                  "position": "领涨" if relative is not None and relative >= 2 else
                                              "落后板块" if relative is not None and relative <= -2 else "跟随板块",
                                  "peer_top3": "；".join(
                                      f"{peer.get('symbol')} {peer.get('name') or ''} {peer['close_pct']:+.2f}% "
                                      f"{((peer.get('auction_amount_yuan') or 0) / 1e4):.0f}万" for peer in peers)})
        report["plate_evolution"] = evolution[:20]
    trade_date = str(report.get("trade_date") or "")
    if not report.get("ladder_summary") and trade_date:
        ladder = _load_ladder_summary(reference_root, trade_date)
        if ladder:
            head = ladder[0]
            for code in str(head.get("top_symbols") or "").split("|"):
                row = by_code.get(code)
                if row:
                    ladder.append({"symbol": code, "name": row.get("name"), "main_plate": row.get("main_plate"),
                                   "prior_streak": row.get("prior_limit_streak"),
                                   "p0925_pct": row.get("p0925_pct"), "p0945_pct": row.get("p0945_pct"),
                                   "close_pct": row.get("close_pct"), "in_yest_pool": row.get("in_yest_pool"),
                                   "status": "今日最高板；收盘涨停价代理，封单/炸板路径 UNKNOWN"})
            report["ladder_summary"] = ladder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--reference-root", type=Path, default=Path("/home/exedev/validation"))
    args = parser.parse_args(argv)
    try:
        report = json.loads((args.report_dir / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"report.json unavailable ({type(exc).__name__})") from None
    try:
        manifest = json.loads((args.report_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    full = _read_json(args.report_dir / "report.full.json")
    if isinstance(full, dict):
        report = {**report, **full}
    if "market" in report:
        _enrich_close_report(report, args.reference_root)
    if isinstance(manifest, dict) and "market" in report:
        for source_path in manifest.get("input_files", {}).values() if isinstance(manifest.get("input_files"), dict) else []:
            if isinstance(source_path, str) and source_path.endswith("manifest.json"):
                candidate = _read_json(Path(source_path))
                if isinstance(candidate, dict) and candidate.get("source") == "wencai":
                    report["daily_manifest"] = candidate
                    break
    trade_date = str(report.get("trade_date") or (manifest or {}).get("trade_date") or "未知日期")
    rendered, _ = render_email_html(report, args.stage, trade_date, manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"preview={args.out} bytes={len(rendered.encode('utf-8'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
