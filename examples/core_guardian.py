#!/usr/bin/env python3
"""Core wall-clock guardian. Dry-run is the default; no cron/service is installed."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import subprocess
import sys
import time
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from engine_core.guardian.scheduler import EventSpec, FileInstanceLock, Guardian, default_events
from engine_core.guardian.management import append_record, load_schedule
from engine_core.prep_trade_day import previous_trade_date


# Configured handlers either call an existing Core job/report or fail visibly.
HANDLER_IMPLEMENTATIONS = {
    "night_report": "examples/run_daily_reports.py:close dry-run",
    "premarket_data": "examples/prep_trade_day.py:premarket_and_auction_baseline",
    "kaipan_fetch": "examples/run_data_jobs.py:limit_up_pool,limit_reasons,hot_plates,plate_members",
    "auction_watch": "NOT_IMPLEMENTED",
    "kaipan_hot_capture": "examples/capture_kaipan_plates.py:hot_plates",
    "snapshot_check": "NOT_IMPLEMENTED",
    "auction_report": "examples/run_daily_reports.py:auction",
    "open_report": "examples/run_daily_reports.py:open",
    "intraday_data": "examples/core_data_daily.py:intraday",
    "postmarket_data": "examples/core_data_daily.py:postmarket",
    "baostock_probe": "examples/core_data_daily.py:postmarket (Baostock daily job)",
    "close_capture": "NOT_IMPLEMENTED",
    "close_report": "examples/run_daily_reports.py:close",
}


def _load_dates(root: Path, explicit: str | None):
    path = Path(explicit).expanduser() if explicit else root / "trade_dates.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("trading_dates", payload) if isinstance(payload, dict) else payload
        if not isinstance(values, list):
            return None
        return {str(item) for item in values}
    except (OSError, ValueError, TypeError):
        return None


def _module_callable(spec: str):
    module_name, sep, attr = spec.partition(":")
    if not sep:
        raise ValueError("adapter must be module:function")
    return getattr(importlib.import_module(module_name), attr)


def _build_gate_status(path: str | None):
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8")) if path else {}
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _command_handler(name: str, opts, root: Path):
    def handler(event: EventSpec, now: datetime, _context, degraded: bool):
        day = now.date().isoformat()
        if event.data_date_offset < 0:
            day = previous_trade_date(root, day) or ""
            if not day:
                raise RuntimeError("previous_trade_date_unavailable")
        py = sys.executable
        repo = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo / "src") + os.pathsep + str(repo)
        if name == "premarket_data":
            cmd = [py, str(repo / "examples/prep_trade_day.py"), "--date", day,
                   "--artifact-root", str(root)]
            if opts.write:
                cmd.append("--write")
        elif name in {"intraday_data", "postmarket_data"}:
            phase = {"premarket_data": "premarket", "intraday_data": "intraday",
                     "postmarket_data": "postmarket"}[name]
            cmd = [py, str(repo / "examples/core_data_daily.py"), "--phase", phase, "--date", day,
                   "--artifact-root", str(root)]
            if opts.write:
                cmd.append("--write")
            if opts.td_write:
                cmd.extend(["--write", "--td-write"])
        elif name in {"auction_report", "open_report", "close_report"}:
            stage = {"auction_report": "auction", "open_report": "open", "close_report": "close"}[name]
            cmd = [py, str(repo / "examples/run_daily_reports.py"), "--stage", stage,
                   "--trade-date", day, "--out-root", str(root / "reports")]
            if not opts.write and not opts.email_self:
                cmd.append("--dry-run")
            elif name == "close_report":
                cmd.append("--publish")
            if opts.email_self:
                marker = root / "guardian" / day / "audit_ok" / stage
                if not marker.is_file():
                    raise RuntimeError("audit_ok_missing:" + stage)
                cmd.extend(["--email-send", "--email-to", "412826313@qq.com"])
        elif name == "kaipan_fetch":
            from engine_core.data_pipeline.storage import ArtifactCatalog
            catalog = ArtifactCatalog(root)
            for job in ("limit_up_pool", "limit_reasons", "hot_plates", "plate_members"):
                existing = catalog.locate(job, day)
                if existing is not None:
                    try:
                        manifest = json.loads(existing.read_text(encoding="utf-8"))
                        quality = manifest.get("quality") if isinstance(manifest, dict) else None
                        if job == "plate_members":
                            from examples.run_data_jobs import _is_complete_member_manifest
                            reusable = _is_complete_member_manifest(manifest)
                        else:
                            reusable = (isinstance(quality, dict) and
                                        quality.get("status") == "READY" and
                                        int(manifest.get("row_count", 0)) > 0)
                        if reusable:
                            continue
                    except (OSError, ValueError, TypeError):
                        pass
                cmd = [py, str(repo / "examples/run_data_jobs.py"), "--job", job, "--date", day,
                       "--artifact-root", str(root)]
                if opts.write:
                    cmd.append("--write")
                result = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                                        timeout=event.timeout_seconds, check=False)
                if result.returncode:
                    raise RuntimeError(f"command_exit={result.returncode}: {result.stderr[-500:]}")
            return "Core Kaipan jobs checked; existing date versions reused"
        elif name == "kaipan_hot_capture":
            cmd = [py, str(repo / "examples/capture_kaipan_plates.py"), "--trade-date", day,
                   "--dataset", "hot_plates", "--artifact-root", str(root)]
            if opts.write:
                cmd.append("--execute")
        elif name == "close_capture":
            raise RuntimeError("NOT_IMPLEMENTED: no existing Core close raw-capture job")
        elif name == "baostock_probe":
            external = os.getenv("CORE_BAOSTOCK_PROBE_COMMAND")
            cmd = (shlex.split(external) + [day] if external else
                   [py, str(repo / "examples/core_data_daily.py"), "--phase", "postmarket", "--date", day,
                    "--artifact-root", str(root)])
            if opts.write:
                cmd.append("--write")
            if opts.td_write:
                cmd.extend(["--write", "--td-write"])
        elif name in {"snapshot_check", "auction_watch"}:
            raise RuntimeError("NOT_IMPLEMENTED: no existing Core " + name + " implementation")
        elif name == "night_report":
            dates = sorted(_load_dates(root, opts.calendar) or [])
            previous = [item for item in dates if item < day]
            previous_day = previous[-1] if previous else previous_trade_date(root, day)
            if previous_day is None:
                raise RuntimeError("previous_trade_date_unavailable")
            cmd = [py, str(repo / "examples/run_daily_reports.py"), "--stage", "close",
                   "--trade-date", previous_day, "--out-root", str(root / "reports"), "--dry-run"]
        else:
            raise RuntimeError("handler_not_implemented:" + name)
        result = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                                timeout=event.timeout_seconds, check=False)
        if result.returncode:
            raise RuntimeError(f"command_exit={result.returncode}: {result.stderr[-500:]}")
        if name == "premarket_data":
            try:
                report = json.loads(result.stdout)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("prep_report_invalid_json") from exc
            if not isinstance(report, dict):
                raise RuntimeError("prep_report_invalid_shape")
            errors = report.get("errors") or []
            if errors:
                raise RuntimeError("prep_errors:" + ";".join(map(str, errors))[-400:])
            missing = [str(item.get("step")) for item in report.get("steps", [])
                       if isinstance(item, dict) and item.get("status") == "MISSING"]
            if missing:
                raise RuntimeError("prep_critical_missing:" + ",".join(missing))
            if report.get("status") == "PARTIAL":
                partial = [str(item.get("step")) for item in report.get("steps", [])
                           if isinstance(item, dict) and item.get("status") == "PARTIAL"]
                return "DEGRADED:prep_partial:" + ",".join(partial)
            if report.get("status") != "READY":
                raise RuntimeError("prep_status_invalid:" + str(report.get("status")))
        return result.stdout[-500:]
    return handler


def _simulate(guardian: Guardian, trade_date: str, clock_range: str, tick_seconds: int):
    start_text, sep, end_text = clock_range.partition("..")
    if not sep:
        raise ValueError("--simulate-clock must be HH:MM[:SS]..HH:MM[:SS]")
    def parse(value):
        if len(value.split(":")) == 2:
            value += ":00"
        return datetime.fromisoformat(f"{trade_date}T{value}+08:00")
    cursor, end = parse(start_text), parse(end_text)
    while cursor <= end:
        result = guardian.tick(cursor)
        for row in result:
            if row.get("status", "").startswith("WOULD") or row.get("status") in {"MISSED", "CALENDAR_UNAVAILABLE"}:
                print(json.dumps({"at": cursor.strftime("%H:%M:%S"), **row}, ensure_ascii=False, sort_keys=True))
        cursor += timedelta(seconds=tick_seconds)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="execute configured handlers")
    mode.add_argument("--dry-run", action="store_true", help="default; describe actions only")
    mode.add_argument("--list", action="store_true", help="list today's event definitions and states")
    mode.add_argument("--once-now", action="store_true", help="evaluate once at current wall clock")
    parser.add_argument("--simulate-date", help="use a simulated YYYY-MM-DD")
    parser.add_argument("--simulate-clock", help="simulate inclusive HH:MM:SS..HH:MM:SS")
    parser.add_argument("--events", help="comma separated event ids")
    parser.add_argument("--schedule", help="versioned schedule JSON; defaults to <artifact-root>/guardian/schedule.json")
    parser.add_argument("--calendar", help="trade_dates.json path; missing calendar fails closed")
    parser.add_argument("--gate-status", help="JSON map of named readiness gates to booleans")
    parser.add_argument("--adapter", help="module:function returning {gates, handlers}")
    parser.add_argument("--tick-seconds", type=float, default=1.0)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--write", action="store_true", help="allow configured Redis/report publication")
    parser.add_argument("--td-write", action="store_true", help="allow TD writes; also requires --write")
    parser.add_argument("--email-self", action="store_true", help="send only to self after audit_ok marker")
    args = parser.parse_args(argv)
    if args.td_write and not args.write:
        parser.error("--td-write requires --write")
    if args.email_self and not args.run:
        parser.error("--email-self requires --run")
    if args.run and (args.simulate_clock or args.once_now or args.list):
        parser.error("--run cannot be combined with --simulate-clock, --once-now, or --list")
    if args.tick_seconds <= 0:
        parser.error("--tick-seconds must be positive")
    root = Path(args.artifact_root).expanduser()
    trade_date = args.simulate_date or date.today().isoformat()
    schedule_path = Path(args.schedule).expanduser() if args.schedule else root / "guardian" / "schedule.json"
    packaged_schedule = Path(__file__).resolve().parents[1] / "src" / "engine_core" / "guardian" / "schedule.json"
    try:
        if schedule_path.exists():
            configured, schedule_config = load_schedule(schedule_path, create_default=False)
        elif args.run:
            configured, schedule_config = load_schedule(schedule_path, create_default=True)
            append_record(schedule_path.parent, {"type": "CONFIG_APPLIED", "revision": schedule_config["revision"],
                                                 "new_hash": __import__("engine_core.guardian.management", fromlist=["schedule_hash"]).schedule_hash(schedule_config),
                                                 "result": "INITIALIZED"})
        else:
            configured, schedule_config = load_schedule(packaged_schedule, create_default=False)
    except (OSError, ValueError, TypeError) as exc:
        print("schedule_invalid:" + str(exc), file=sys.stderr)
        return 2
    selected = [event for event in configured
                if not args.events or event.event_id in set(args.events.split(","))]
    gates_data = _build_gate_status(args.gate_status)
    if args.simulate_clock and args.simulate_date and not args.gate_status:
        # A simulation can exercise the time sequence without data connections.
        gates_data = {event.gate: True for event in selected}
    gates = {name: (lambda _event, _now, _ctx, value=value: bool(value))
             for name, value in gates_data.items()}
    gates.setdefault("calendar_ready", lambda *_: True)
    handlers = {name: _command_handler(name, args, root) for name in
                {event.handler for event in selected}}
    if args.adapter:
        adapter = _module_callable(args.adapter)()
        gates.update(adapter.get("gates", {}))
        handlers.update(adapter.get("handlers", {}))
    guardian = Guardian(selected, calendar_dates=_load_dates(root, args.calendar),
                        state_root=root / "guardian", gates=gates, handlers=handlers,
                        dry_run=not args.run, context={"write": args.write, "td_write": args.td_write},
                        schedule_path=schedule_path if schedule_path.exists() else None,
                        config_revision=schedule_config["revision"], config_payload=schedule_config)
    if args.list:
        for event in selected:
            print(json.dumps({"id": event.event_id, "phase": event.phase, "window": [event.start, event.deadline],
                              "gate": event.gate, "handler": event.handler,
                              "status": guardian.status(trade_date, event.event_id)}, ensure_ascii=False))
        return 0
    if args.simulate_clock:
        if not args.simulate_date:
            parser.error("--simulate-clock requires --simulate-date")
        _simulate(guardian, trade_date, args.simulate_clock, max(1, int(args.tick_seconds)))
        return 0
    if args.once_now:
        lock = FileInstanceLock(root / "guardian" / "guardian.lock")
        try:
            lock.acquire()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            now = datetime.now().astimezone()
            actions = guardian.tick(now)
            guardian.write_heartbeat(now)
            for row in actions:
                print(json.dumps(row, ensure_ascii=False, sort_keys=True))
            return 0
        finally:
            lock.release()
    lock = FileInstanceLock(root / "guardian" / "guardian.lock")
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        last_heartbeat = 0.0
        heartbeat_stop = threading.Event()
        def heartbeat_loop():
            while not heartbeat_stop.wait(5.0):
                guardian.write_heartbeat(datetime.now().astimezone())
        heartbeat_thread = threading.Thread(target=heartbeat_loop, name="guardian-heartbeat", daemon=True)
        heartbeat_thread.start()
        while True:
            now = datetime.now().astimezone()
            for row in guardian.tick(now):
                print(json.dumps({"at": now.isoformat(), **row}, ensure_ascii=False, sort_keys=True), flush=True)
            if time.monotonic() - last_heartbeat >= 5.0:
                guardian.write_heartbeat(now)
                last_heartbeat = time.monotonic()
            time.sleep(args.tick_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        if "heartbeat_stop" in locals():
            heartbeat_stop.set()
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
