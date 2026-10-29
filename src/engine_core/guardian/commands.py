"""Pure command construction shared by the legacy guardian and contract bridge."""
from __future__ import annotations

import os
from typing import Any
import json
import math
from collections.abc import Mapping
from numbers import Real
from pathlib import Path


_DAILY_KLINE_PROVIDERS = ("baostock", "wencai", "q2_final")


def _event_args(event):
    return getattr(event, "args", None) or {}


def _arg(event_args, name, default=None):
    if isinstance(event_args, Mapping):
        return event_args.get(name, default)
    return getattr(event_args, name, default)


def _has_arg(event_args, name):
    if isinstance(event_args, Mapping):
        return name in event_args
    return hasattr(event_args, name)


def _append_baostock_no_progress(cmd, event_args):
    if _has_arg(event_args, "baostock_no_progress_seconds"):
        cmd.extend(["--baostock-no-progress-seconds",
                    str(_arg(event_args, "baostock_no_progress_seconds"))])


def _append_baostock_completion_policy(cmd, event_args):
    policy = _arg(event_args, "daily_kline_policy")
    if not isinstance(policy, Mapping):
        return
    if "formal_coverage_threshold" in policy:
        cmd.extend(["--baostock-formal-coverage-threshold",
                    str(policy["formal_coverage_threshold"])])
    if "source_empty_max" in policy:
        cmd.extend(["--baostock-source-empty-max", str(policy["source_empty_max"])])


_WENCAI_ARG_FLAGS = (
    ("wencai_cookie_file", "--wencai-cookie-file"),
    ("wencai_cooldown_seconds", "--wencai-cooldown-seconds"),
    ("wencai_cooldown_max_seconds", "--wencai-cooldown-max-seconds"),
    ("wencai_max_cooldowns", "--wencai-max-cooldowns"),
    ("wencai_max_total_requests", "--wencai-max-total-requests"),
    ("wencai_segments", "--wencai-segments"),
    ("wencai_segment_edges_yi", "--wencai-segment-edges-yi"),
    ("wencai_segment_max_rows", "--wencai-segment-max-rows"),
    ("wencai_coverage_threshold", "--wencai-coverage-threshold"),
)


def _append_wencai_args(cmd, event_args):
    """Config-only WenCai knobs (schedule.json ladder args) become CLI flags; Python holds no defaults here."""
    for name, flag in _WENCAI_ARG_FLAGS:
        if _has_arg(event_args, name):
            cmd.extend([flag, str(_arg(event_args, name))])


def daily_kline_budget_seconds(timeout_seconds, event_args):
    """Return the daily-kline subprocess budget bounded by its event timeout."""
    if timeout_seconds is None or timeout_seconds == "":
        return None
    ratio = _arg(event_args, "budget_ratio", 0.9)
    floor = _arg(event_args, "budget_floor_seconds", 60)
    if isinstance(ratio, bool) or not isinstance(ratio, Real):
        raise ValueError("daily_kline_budget_ratio_must_be_between_zero_and_one")
    try:
        ratio = float(ratio)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("daily_kline_budget_ratio_must_be_between_zero_and_one") from None
    if not math.isfinite(ratio) or not 0 < ratio <= 1:
        raise ValueError("daily_kline_budget_ratio_must_be_between_zero_and_one")
    if type(floor) is not int or floor < 1:
        raise ValueError("daily_kline_budget_floor_seconds_must_be_positive_int")
    if isinstance(timeout_seconds, bool):
        raise ValueError("daily_kline_timeout_seconds_invalid")
    try:
        timeout = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("daily_kline_timeout_seconds_invalid") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("daily_kline_timeout_seconds_must_be_positive")
    return max(floor, int(timeout * ratio))


def _daily_kline_provider_arg(value):
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise ValueError("daily_kline_providers_must_be_list_or_csv")
    providers = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("daily_kline_provider_invalid")
        name = item.strip()
        if name not in _DAILY_KLINE_PROVIDERS:
            raise ValueError("daily_kline_provider_invalid:" + name)
        if name not in providers:
            providers.append(name)
    if not providers:
        raise ValueError("daily_kline_providers_empty")
    return ",".join(providers)


def validate_handler_postcondition(name: str, day: str, root: str | Path,
                                   datasets: Any = None,
                                   event_args: Any = None) -> tuple[bool, str]:
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
        # C587: an event that lists its own datasets (D-1 reasons/members) is judged on those, not on the pool.
        datasets = tuple(datasets) if datasets else ("limit_up_pool",)
    elif name == "kaipan_hot_capture":
        declared = tuple(datasets) if datasets else ()
        if "hot_plates_history" in declared:
            from ..data_pipeline.hot_plates_gaps import (
                _existing_gap_rows, history_capture_exists, history_event_id,
                prior_hot_plate_dates,
            )
            count = _arg(event_args or {}, "hot_plates_history_days")
            if count is None:
                marker = next((value for value in declared
                               if isinstance(value, str) and
                               value.startswith("hot_plates_history_days=")), None)
                try:
                    count = int(marker.split("=", 1)[1]) if marker else 3
                except ValueError:
                    return False, "hot_plates_history_days_invalid"
            try:
                history_dates = prior_hot_plate_dates(root, day, count)
            except (RuntimeError, ValueError, TypeError):
                return False, "hot_plates_history_calendar_unknown"
            for target_date in history_dates:
                history_id = history_event_id(target_date)
                if history_capture_exists(root, target_date):
                    continue
                gaps = _existing_gap_rows(catalog, target_date)
                if any(row.get("event_id") == history_id and row.get("status") == "GAP"
                       for row in gaps):
                    continue
                return False, "hot_plates_history_missing:" + target_date
            return True, ""
        if "hot_plates_gaps" in declared:
            gap_path = catalog.locate("hot_plates_gaps", day)
            gaps = catalog.read_rows(gap_path) if gap_path and gap_path.is_file() else []
            manifest_path = catalog.locate("hot_plates", day)
            if gaps or (manifest_path is not None and manifest_path.is_file()):
                return True, ""
            return False, "hot_plates_gap_reconcile_missing_output"
        event_id = _arg(event_args or {}, "event_id")
        if not isinstance(event_id, str) or not event_id.startswith("kaipan_hot_"):
            event_id = next((value for value in declared
                             if isinstance(value, str) and value.startswith("kaipan_hot_")), None)
        manifest_path = catalog.locate("hot_plates", day)
        if manifest_path is not None and manifest_path.is_file():
            if event_id is None:
                return True, ""
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                rows = catalog.read_rows(manifest_path)
                captured = (manifest.get("event_id") == event_id or
                            any(row.get("event_id") == event_id for row in rows))
                if captured:
                    return True, ""
            except (OSError, ValueError, TypeError):
                return False, "hot_plates_manifest_invalid"
        gap_path = catalog.locate("hot_plates_gaps", day)
        if gap_path is not None and gap_path.is_file():
            try:
                gaps = catalog.read_rows(gap_path)
            except (OSError, ValueError, TypeError):
                return False, "hot_plates_gaps_invalid"
            accepted = any(row.get("status") == "GAP" and
                           (event_id is None or row.get("event_id") == event_id)
                           for row in gaps)
            if accepted:
                return True, ""
        return False, "hot_plates_event_missing" if event_id else "hot_plates_manifest_missing"
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
                return False, dataset + "_missing_or_empty"
            return False, dataset + "_manifest_missing"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if name == "kaipan_fetch" and int(manifest.get("row_count", 0)) <= 0:
                return False, dataset + "_missing_or_empty"
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
        deps = _event_args(event).get("theme_deps") if isinstance(_event_args(event), dict) else None
        if isinstance(deps, dict):
            # C587: dependency wait / degrade policy for the yesterday pool, reasons and members (schedule.json).
            cmd.extend(["--theme-wait-seconds", str(deps.get("wait_seconds", 0)),
                        "--theme-poll-seconds", str(deps.get("poll_seconds", 30)),
                        "--theme-on-missing", str(deps.get("on_missing", "fail"))])
        return cmd
    if name in {"intraday_data", "postmarket_data"}:
        phase = {"intraday_data": "intraday", "postmarket_data": "postmarket"}[name]
        cmd = [py, str(repo / "examples/core_data_daily.py"), "--phase", phase, "--date", day,
               "--artifact-root", str(root)]
        if opts.write:
            cmd.append("--write")
        if opts.td_write:
            cmd.extend(["--write", "--td-write"])
        _append_baostock_no_progress(cmd, _event_args(event))
        _append_baostock_completion_policy(cmd, _event_args(event))
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
        if opts.job == "daily_kline":
            # Keep the chain budget inside the event timeout so a long Baostock
            # fetch checkpoints and falls back instead of being killed.
            args = _event_args(event)
            budget = daily_kline_budget_seconds(getattr(event, "timeout_seconds", None), args)
            if budget is not None:
                cmd.extend(["--budget-seconds", str(budget)])
            if _has_arg(args, "providers"):
                cmd.extend(["--providers", _daily_kline_provider_arg(_arg(args, "providers"))])
            _append_baostock_no_progress(cmd, args)
            _append_wencai_args(cmd, args)
        if opts.write:
            cmd.append("--write")
        elif getattr(opts, "simulate_clock", False):
            # Fakeclock acceptance runs against a temporary artifact root. Persist
            # there so dependent jobs can consume the real provider result, while
            # leaving Redis publication disabled.
            cmd.append("--execute")
        return cmd
    if name == "kaipan_hot_capture":
        event_args = _event_args(event)
        history_days = _arg(event_args, "hot_plates_history_days")
        if history_days is not None:
            if type(history_days) is not int or not 1 <= history_days <= 3:
                raise ValueError("hot_plates_history_days_must_be_between_1_and_3")
            cmd = [py, str(repo / "examples/run_data_jobs.py"), "--job", "hot_plates",
                   "--date", day, "--artifact-root", str(root),
                   "--hot-plates-history-days", str(history_days)]
            scheduled_at = getattr(event, "start", None) or _arg(event_args, "scheduled_at")
            deadline = getattr(event, "deadline", None) or _arg(event_args, "deadline")
            if scheduled_at:
                cmd.extend(["--hot-plates-scheduled-at", str(scheduled_at)])
            if deadline:
                cmd.extend(["--hot-plates-deadline", str(deadline)])
            if opts.write or getattr(opts, "simulate_clock", False):
                # History is stored as an immutable Core artifact; never publish it to Redis.
                cmd.append("--execute")
            return cmd
        cmd = [py, str(repo / "examples/capture_kaipan_plates.py"), "--trade-date", day,
               "--dataset", "hot_plates", "--artifact-root", str(root)]
        if _arg(event_args, "reconcile_only"):
            cmd.append("--reconcile-only")
            if opts.write:
                cmd.append("--write-artifact")
            return cmd
        event_id = getattr(event, "event_id", None) or _arg(event_args, "event_id")
        scheduled_at = getattr(event, "start", None) or _arg(event_args, "scheduled_at")
        deadline = getattr(event, "deadline", None) or _arg(event_args, "deadline")
        if event_id:
            cmd.extend(["--event-id", str(event_id)])
        if scheduled_at:
            cmd.extend(["--scheduled-at", str(scheduled_at)])
        if deadline:
            cmd.extend(["--deadline", str(deadline)])
        if _has_arg(event_args, "max_pages"):
            cmd.extend(["--max-pages", str(_arg(event_args, "max_pages"))])
        if opts.write:
            cmd.append("--execute")
        return cmd
    if name == "baostock_probe":
        external = os.getenv("CORE_BAOSTOCK_PROBE_COMMAND")
        cmd = __import__("shlex").split(external) + [day] if external else [
            py, str(repo / "examples/core_data_daily.py"), "--phase", "postmarket", "--date", day,
            "--artifact-root", str(root)]
        if not external:
            event_args = _event_args(event)
            budget = daily_kline_budget_seconds(
                getattr(event, "timeout_seconds", None), event_args)
            if budget is not None:
                cmd.extend(["--baostock-total-timeout-seconds", str(budget)])
            _append_baostock_no_progress(cmd, event_args)
            _append_baostock_completion_policy(cmd, event_args)
        if opts.write:
            cmd.append("--write")
        if opts.td_write or opts.write:
            cmd.extend(["--write", "--td-write"])
        return cmd
    if name == "night_report":
        cmd = [py, str(repo / "examples/run_daily_reports.py"), "--stage", "close",
               "--trade-date", day, "--out-root", str(root / "reports")]
        if opts.simulate_clock or (not opts.write and not opts.email_self):
            cmd.append("--dry-run")
        if getattr(opts, "email_self", False):
            if opts.simulate_clock:
                cmd.extend(["--email-to", "412826313@qq.com"])
            else:
                cmd.extend(["--email-send", "--email-to", "412826313@qq.com"])
        return cmd
    raise ValueError("handler_command_not_supported:" + name)
