"""Pure command construction shared by the legacy guardian and contract bridge."""
from __future__ import annotations

import os
import json
from pathlib import Path


def validate_handler_postcondition(name: str, day: str, root: str | Path) -> tuple[bool, str]:
    """Read Core catalog/file outputs after a successful guardian command."""
    root = Path(root).expanduser()
    if name == "calendar_refresh":
        path = root / "trade_calendar" / "calendar.json"
        try:
            matrix = json.loads(path.read_text(encoding="utf-8"))
            ok = (isinstance(matrix, dict) and
                  matrix.get("lit_from", "") <= day <= matrix.get("lit_to", ""))
            return (True, "") if ok else (False, "calendar_missing_or_not_lit")
        except (OSError, ValueError, TypeError, AttributeError):
            return False, "calendar_missing_or_invalid"
    if name not in {"premarket_data", "kaipan_fetch", "kaipan_hot_capture",
                    "postmarket_data", "baostock_probe"}:
        return True, ""
    from ..data_pipeline.storage import ArtifactCatalog
    catalog = ArtifactCatalog(root)
    if name == "premarket_data":
        # prep_trade_day materializes the target-day universe. stock_name_map is
        # a report slice backed by stock_static, and plate_registry is a
        # versioned reference input; neither is emitted for each trade date.
        datasets = ("symbol_universe",)
    elif name == "kaipan_fetch":
        datasets = ("limit_up_pool",)
    elif name == "kaipan_hot_capture":
        datasets = ("hot_plates",)
    else:
        datasets = ("daily_kline",)
    for dataset in datasets:
        target = day
        if name == "kaipan_fetch":
            # The morning limit-up pool is the previous trade day's pool.
            from ..trade_day_context import previous_trade_date
            try:
                target = previous_trade_date(root, day)
            except Exception:
                return False, "previous_trade_date_unavailable"
        manifest_path = catalog.locate(dataset, target)
        if manifest_path is None or not manifest_path.is_file():
            if name == "kaipan_fetch":
                return False, "limit_up_pool_missing_or_empty"
            return False, dataset + "_manifest_missing"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if name == "kaipan_fetch" and int(manifest.get("row_count", 0)) <= 0:
                return False, "limit_up_pool_missing_or_empty"
            if dataset == "daily_kline":
                from .registry import kline_coverage_problem
                problem = kline_coverage_problem(manifest.get("quality"))
                if problem:
                    return False, problem
        except (OSError, ValueError, TypeError):
            return False, dataset + "_manifest_invalid"
    return True, ""


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
            cmd.extend(["--asof", str((getattr(event, "args", None) or {}).get("asof", event.start))])
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
        elif getattr(opts, "simulate_clock", False):
            # Fakeclock acceptance runs against a temporary artifact root. Persist
            # there so dependent jobs can consume the real provider result, while
            # leaving Redis publication disabled.
            cmd.append("--execute")
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
