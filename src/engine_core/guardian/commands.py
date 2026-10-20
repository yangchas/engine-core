"""Pure command construction shared by the legacy guardian and contract bridge."""
from __future__ import annotations

import os
from pathlib import Path


def build_handler_command(name, event, opts, root: Path, day: str, py: str, repo: Path):
    """Return the argv for a CLI-backed handler without executing or probing anything."""
    root, repo = Path(root), Path(repo)
    if name == "premarket_data":
        cmd = [py, str(repo / "examples/prep_trade_day.py"), "--date", day,
               "--artifact-root", str(root)]
        if opts.write:
            cmd.append("--write")
        return cmd
    if name in {"intraday_data", "postmarket_data"}:
        phase = {"intraday_data": "intraday", "postmarket_data": "postmarket"}[name]
        cmd = [py, str(repo / "examples/core_data_daily.py"), "--phase", phase, "--date", day,
               "--artifact-root", str(root)]
        if opts.write:
            cmd.append("--write")
        if opts.td_write:
            cmd.extend(["--write", "--td-write"])
        return cmd
    if name in {"auction_report", "open_report", "close_report"}:
        stage = {"auction_report": "auction", "open_report": "open", "close_report": "close"}[name]
        cmd = [py, str(repo / "examples/run_daily_reports.py"), "--stage", stage,
               "--trade-date", day, "--out-root", str(root / "reports")]
        if name == "auction_report":
            cmd.extend(["--asof", "09:26:00"])
        elif name == "open_report":
            cmd.extend(["--asof", event.start])
        if opts.simulate_clock or (not opts.write and not opts.email_self):
            cmd.append("--dry-run")
        elif name == "close_report":
            cmd.append("--publish")
        if getattr(opts, "email_self", False):
            if opts.simulate_clock:
                cmd.extend(["--email-to", "412826313@qq.com"])
            else:
                cmd.extend(["--email-send", "--email-to", "412826313@qq.com"])
        return cmd
    if name == "kaipan_job":
        cmd = [py, str(repo / "examples/run_data_jobs.py"), "--job", opts.job,
               "--date", day, "--artifact-root", str(root)]
        if opts.write:
            cmd.append("--write")
        return cmd
    if name == "kaipan_hot_capture":
        cmd = [py, str(repo / "examples/capture_kaipan_plates.py"), "--trade-date", day,
               "--dataset", "hot_plates", "--artifact-root", str(root)]
        if opts.write:
            cmd.append("--execute")
        return cmd
    if name == "baostock_probe":
        external = os.getenv("CORE_BAOSTOCK_PROBE_COMMAND")
        cmd = __import__("shlex").split(external) + [day] if external else [
            py, str(repo / "examples/core_data_daily.py"), "--phase", "postmarket", "--date", day,
            "--artifact-root", str(root)]
        if opts.write:
            cmd.append("--write")
        if opts.td_write:
            cmd.extend(["--write", "--td-write"])
        return cmd
    if name == "night_report":
        return [py, str(repo / "examples/run_daily_reports.py"), "--stage", "close",
                "--trade-date", day, "--out-root", str(root / "reports"), "--dry-run"]
    raise ValueError("handler_command_not_supported:" + name)
