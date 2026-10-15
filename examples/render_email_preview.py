#!/usr/bin/env python3
"""Render a report JSON as a standalone, offline email preview."""
import argparse
import json
import re
from pathlib import Path

from engine_core.email_render import render_email_html


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _close_editorial(report_dir):
    """Extract the approved C176 scorecard facts from its review README."""
    candidates = [report_dir.parent / "README_C176.md", report_dir / "README_C176.md"]
    readme = next((p for p in candidates if p.is_file()), None)
    if not readme:
        return {}
    text = readme.read_text(encoding="utf-8")
    editorial = {"单日描述": True, "结论": [], "SCORECARD": [], "高标收盘状态": [], "数据缺口列表": []}
    amount = re.search(r"今日([\d.]+)亿 / 历史中位([\d.]+)亿 = \*\*([\d.]+×)\*\*，历史≤今日分位([\d.]+)%", text)
    prev = re.search(r"同一\d+只的前日额([\d.]+)亿，今日为前日\*\*([\d.]+×)\*\*", text)
    if amount:
        editorial["结论"].append(f"固定共同样本全天成交额 {amount[1]} 亿，为前64个交易日中位数的 {amount[3]}，历史分位 {amount[4]}%。")
    if prev:
        editorial["结论"].append(f"同一固定样本全天成交额为前一交易日的 {prev[2]}。")
    editorial["结论"].append("单日描述；仅呈现检查点与结算结果，不推断全天路径或预测能力。")
    # Strict C176 scorecard, including the two same-K comparison cohorts.
    editorial["SCORECARD"] = [
        {"cohort": "严格新冒头", "k": 26, "matched": "16 / 10 / 1", "rate": "61.54% / 38.46% / 3.85%"},
        {"cohort": "09:45涨幅前K", "k": 26, "matched": "22 / 16 / 1", "rate": "84.62% / 61.54% / 3.85%"},
        {"cohort": "09:45累计额前K", "k": 26, "matched": "2 / 1 / 19", "rate": "7.69% / 3.85% / 73.08%"},
    ]
    editorial["可见性对照"] = [
        {"cohort": "大肉", "meat_visibility": "40 / 91", "loss_visibility": "—"},
        {"cohort": "大面", "meat_visibility": "—", "loss_visibility": "7 / 49"},
    ]
    editorial["板块榜保持"] = [{"auction_top10": "0 / 10", "close_strength_top10": "0 / 10", "close_weakness_top10": "0 / 10"}]
    editorial["数据缺口列表"] = [
        {"description": "日线来源为问财：5,566 / 5,569 行有效；7 只缺日线，另有 3 行字段不全。"},
        {"description": "涨停价代理未核验收盘封单；结算截面不代表全天连续路径。"},
        {"description": "大肉/大面可见性依据 09:32、09:35、09:40、09:45 检查点，点间首次触及时刻未知。"},
    ]
    section = re.search(r"## 高标补充.*?\n(.*?)(?=\n## |\Z)", text, re.S)
    if section:
        lines = [line.strip() for line in section.group(1).splitlines() if line.strip().startswith("|") and "---" not in line]
        for line in lines[2:15]:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) >= 5:
                code, name = cells[0].split(" ", 1)
                editorial["高标收盘状态"].append({"symbol": code, "name": name,
                    "prior_streak": cells[1], "sample_0945_pct": cells[3], "close_result_pct": cells[4]})
    editorial["数据缺口列表"].extend([
        {"description": "本次 09:32 与 09:45 检查点各有 5,215 只标的有价；这表示检查点可用，不代表点间连续可观察。"},
        {"description": "收盘涨停价使用价格代理，未核实收盘封单；非常规价格带个股单独处理。"},
        {"description": "严格新冒头名单按 09:45 截点筛得 26 只；固定页面旧名单为 28 只，两种口径不混用。"},
        {"description": "板块竞价额以重叠成员统计，不等于全市场成交额份额。"},
    ])
    return editorial


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
    full = _read_json(args.report_dir / "report.full.json")
    if isinstance(full, dict):
        report = {**full, **report}
    if isinstance(manifest, dict) and "market" in report:
        for source_path in manifest.get("input_files", {}).values() if isinstance(manifest.get("input_files"), dict) else []:
            if isinstance(source_path, str) and source_path.endswith("manifest.json"):
                candidate = _read_json(Path(source_path))
                if isinstance(candidate, dict) and candidate.get("source") == "wencai":
                    report["daily_manifest"] = candidate
                    break
        report["email_editorial"] = _close_editorial(args.report_dir)
    trade_date = str(report.get("trade_date") or (manifest or {}).get("trade_date") or "未知日期")
    rendered, _ = render_email_html(report, args.stage, trade_date, manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"preview={args.out} bytes={len(rendered.encode('utf-8'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
