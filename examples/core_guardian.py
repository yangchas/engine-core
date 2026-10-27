#!/usr/bin/env python3
"""Core wall-clock guardian. Dry-run is the default; no cron/service is installed."""
from __future__ import annotations

import argparse
import atexit
import importlib
import json
import os
import shlex
import subprocess
import sys
import shutil
import tempfile
import time
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from engine_core.guardian.scheduler import EventSpec, FileInstanceLock, Guardian, default_events
from engine_core.guardian.management import append_record, load_schedule
from engine_core.guardian.gates import default_gates
from engine_core.guardian.commands import build_handler_command
from engine_core.prep_trade_day import previous_trade_date
from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

_ALERTED: set[tuple[str, str, str]] = set()
_ACTIVE_GUARDIAN = None

class _CoveredDates(set):
    def __init__(self, dates, start, end):
        super().__init__(dates)
        self.coverage_start = start
        self.coverage_end = end

def _calendar_alert_once(root: Path, day: str, reason: str) -> bool:
    marker = root / "guardian" / "calendar_alerts.json"
    try:
        payload = json.loads(marker.read_text())
    except (OSError, ValueError):
        payload = {}
    reasons = payload.get(day, [])
    if isinstance(reasons, str):
        reasons = [reasons]
    if reason in reasons:
        return False
    payload[day] = sorted({*reasons, reason})
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(payload, sort_keys=True) + "\n")
    _ALERTED.add(("calendar", day, reason))
    return True


# Configured handlers either call an existing Core job/report or fail visibly.
HANDLER_IMPLEMENTATIONS = {
    "night_report": "examples/run_daily_reports.py:close dry-run",
    "premarket_data": "examples/prep_trade_day.py:premarket_and_auction_baseline",
    "kaipan_fetch": "examples/run_data_jobs.py:limit_up_pool,limit_reasons,hot_plates,plate_members",
    "auction_watch": "Core read-only TD arrival sample",
    "kaipan_hot_capture": "examples/capture_kaipan_plates.py:hot_plates",
    "snapshot_check": "Core read-only TD snapshot count and coverage",
    "auction_report": "examples/run_daily_reports.py:auction",
    "open_report": "examples/run_daily_reports.py:open",
    "intraday_data": "examples/core_data_daily.py:intraday",
    "postmarket_data": "examples/core_data_daily.py:postmarket",
    "baostock_probe": "examples/core_data_daily.py:postmarket (Baostock daily job)",
    "close_capture": "NOT_IMPLEMENTED",
    "close_report": "examples/run_daily_reports.py:close",
    "calendar_refresh": "Core versioned trading calendar refresh",
}


def _load_dates(root: Path, explicit: str | None):
    if not explicit:
        store = TradeCalendarStore(root)
        data = store.load()
        if not data:
            return None
        return _CoveredDates(store.all_trading_dates(), data.get("lit_from"), data.get("lit_to"))
    path = Path(explicit).expanduser() if explicit else root / "trade_dates.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("trading_dates", payload) if isinstance(payload, dict) else payload
        if not isinstance(values, list):
            return None
        return None  # unversioned lists have no coverage proof
    except (OSError, ValueError, TypeError):
        return None


def _module_callable(spec: str):
    module_name, sep, attr = spec.partition(":")
    if not sep:
        raise ValueError("adapter must be module:function")
    return getattr(importlib.import_module(module_name), attr)


def _command_failure(result: subprocess.CompletedProcess) -> RuntimeError:
    parts = [f"command_exit={result.returncode}"]
    for label, output in (("stdout", result.stdout), ("stderr", result.stderr)):
        if output:
            parts.extend((f"{label}_tail:", str(output)[-500:].rstrip()))
    return RuntimeError("\n".join(parts))


def _build_gate_status(path: str | None):
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8")) if path else {}
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _command_handler(name: str, opts, root: Path):
    def handler(event: EventSpec, now: datetime, _context, degraded: bool):
        event_id = getattr(event, "event_id", name)
        day = now.date().isoformat()
        if name == "calendar_refresh":
            store = TradeCalendarStore(root)
            refresh_mode = (getattr(event, "args", None) or {}).get("refresh")
            result = (store.force_refresh(now=now) if refresh_mode == "force" else
                      store.refresh_if_needed(now=now))
            if result.get("status") != "CONFIRMED":
                status = "STALE" if result.get("version") else result.get("status", "UNKNOWN")
                reason = str(result.get("reason_code", "REFRESH_FAILED"))
                raise RuntimeError(f"DEGRADED:calendar_refresh_{status}:{reason}")
            snapshot = store.load()
            if not snapshot:
                raise RuntimeError("DEGRADED:calendar_refresh_snapshot_unavailable")
            from engine_core.trade_day_context import refresh_context
            context = refresh_context(root, day)
            return json.dumps({"calendar": result, "day_context": {
                key: context[key] for key in ("today", "prev_trade_date", "next_trade_date")}}, ensure_ascii=False)
        if name in {"snapshot_check", "auction_watch"}:
            from engine_core.guardian.gates import _query, auction_snapshot_stats
            if name == "snapshot_check":
                tag = "0920" if "0920" in event.gate else "0924" if "0924" in event.gate else "0925"
                metrics = auction_snapshot_stats(now, tag)
                count = metrics["row_count"]
                ready = metrics["coverage"] >= 0.90
                outcome = "SUCCEEDED" if ready else ("SUCCEEDED_DEGRADED" if degraded else "INSUFFICIENT")
                record = {"event": event.event_id, "trade_date": day, "tag": tag,
                          **metrics, "outcome": outcome, "observed_at": now.isoformat()}
            else:
                rows = _query(f"SELECT symbol, ts FROM market_data1.auction_snapshot_v2 WHERE trade_date='{day.replace('-', '')}' AND auction_tag='0920'")
                count = len({str(row[0]) for row in rows})
                first = min((row[1] for row in rows), default=None)
                last = max((row[1] for row in rows), default=None)
                record = {"event": event.event_id, "trade_date": day, "tag": "0920",
                          "row_count": int(count), "first_at": str(first), "last_at": str(last),
                          "observed_at": now.isoformat()}
            target = root / "guardian" / day / "status_events.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            return "DEGRADED:insufficient_snapshot" if record.get("outcome") == "INSUFFICIENT" else json.dumps(record, ensure_ascii=False)
        if name == "close_capture":
            return "DEGRADED:disabled_not_implemented"
        if event.data_date_offset < 0:
            day = previous_trade_date(root, day) or ""
            if not day:
                raise RuntimeError("previous_trade_date_unavailable")
        py = sys.executable
        repo = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo / "src") + os.pathsep + str(repo)
        if name == "auction_report" and event_id == "auction_facts_0925":
            from examples import fetch_auction_item
            snapshots, ticks = fetch_auction_item._source_rows(day)
            outputs = []
            degraded_items = []
            for item in ("auction_stock_facts", "auction_points", "auction_panorama"):
                payload = fetch_auction_item._products(day, item, root, snapshots, ticks)
                if payload.get("status") not in {"ok", "degraded"}:
                    raise RuntimeError(f"auction_item_failed:{item}:{payload}")
                outputs.append(payload)
                if payload.get("status") == "degraded":
                    degraded_items.append({"item": item, "missing_inputs": payload.get("missing_inputs", [])})
            detail = json.dumps({"status": "degraded" if degraded_items else "ok",
                                 "items": outputs, "degraded": degraded_items},
                                ensure_ascii=False, separators=(",", ":"))
            return "DEGRADED:" + detail if degraded_items else detail
        if name == "night_report":
            try:
                from engine_core.trade_day_context import previous_trade_date as context_previous
                day = context_previous(root, day)
            except ValueError:
                raise RuntimeError("previous_trade_date_unavailable")
        if name == "kaipan_fetch":
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
                            reusable = (isinstance(quality, dict) and quality.get("status") == "READY"
                                        and int(manifest.get("row_count", 0)) > 0)
                        if reusable:
                            continue
                    except (OSError, ValueError, TypeError):
                        pass
                job_opts = SimpleNamespace(job=job, write=opts.write,
                                            simulate_clock=getattr(opts, "simulate_clock", False))
                cmd = build_handler_command("kaipan_job", event, job_opts, root, day, py, repo)
                result = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                                        timeout=event.timeout_seconds, check=False)
                if result.returncode:
                    raise _command_failure(result)
            return "Core Kaipan jobs checked; existing date versions reused"
        elif name in {"premarket_data", "intraday_data", "postmarket_data", "auction_report",
                      "open_report", "close_report", "kaipan_hot_capture", "baostock_probe", "night_report"}:
            cmd = build_handler_command(name, event, opts, root, day, py, repo)
        else:
            raise RuntimeError("handler_not_implemented:" + name)
        result = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                                timeout=event.timeout_seconds, check=False)
        if result.returncode:
            raise _command_failure(result)
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
    def notifying_handler(event: EventSpec, now: datetime, context, degraded: bool):
        event_id = getattr(event, "event_id", name)
        try:
            return handler(event, now, context, degraded)
        except Exception as exc:
            reason = type(exc).__name__ + ":" + str(exc)
            if name == "calendar_refresh":
                reason = str(exc).split(":", 2)[-1]
                should_alert = _calendar_alert_once(root, now.date().isoformat(), reason)
            else:
                alert_key = (now.date().isoformat(), event_id, reason)
                should_alert = alert_key not in _ALERTED
                _ALERTED.add(alert_key)
            if getattr(opts, "email_self", False) and should_alert:
                try:
                    from engine_core.report_mailer import build_alert_message, load_smtp_config, send_message
                    config = load_smtp_config() if opts.run and not opts.simulate_clock else None
                    alert = build_alert_message(now.date().isoformat(), event_id, exc,
                                                "412826313@qq.com",
                                                config.from_addr if config else "reports@localhost")
                    receipt = send_message(config, alert, dry_run=not opts.run or opts.simulate_clock,
                                           allow_recipients=["412826313@qq.com"])
                    print(json.dumps({"event": event_id, "alert_status": receipt.status,
                                      "error_type": type(exc).__name__, "alert_subject": alert["Subject"]},
                                     ensure_ascii=False), flush=True)
                except Exception as alert_exc:
                    print(json.dumps({"event": event_id, "alert_status": "failed",
                                      "error_type": type(alert_exc).__name__}, ensure_ascii=False), flush=True)
            if name == "calendar_refresh":
                return "DEGRADED:calendar_refresh:" + reason
            raise
    return notifying_handler


def _notify_gate_missed(rows, events, opts, now):
    if not getattr(opts, "email_self", False):
        return
    by_id = {event.event_id: event for event in events}
    for row in rows:
        event_status = row.get("status")
        if event_status not in {"MISSED", "SUCCEEDED_DEGRADED"} or row.get("event") not in by_id:
            continue
        event = by_id[row["event"]]
        if event_status == "SUCCEEDED_DEGRADED" and not event.degraded_at_deadline:
            continue
        alert_key = (now.date().isoformat(), event.event_id, "GateBlocked:" + event.gate)
        if alert_key in _ALERTED:
            continue
        _ALERTED.add(alert_key)
        try:
            from engine_core.report_mailer import build_alert_message, load_smtp_config, send_message
            config = load_smtp_config() if opts.run and not opts.simulate_clock else None
            error = RuntimeError(f"gate_blocked:{event.gate}; final status={event_status}")
            alert = build_alert_message(now.date().isoformat(), event.event_id, error,
                                        "412826313@qq.com",
                                        config.from_addr if config else "reports@localhost")
            receipt = send_message(config, alert, dry_run=not opts.run or opts.simulate_clock,
                                   allow_recipients=["412826313@qq.com"])
            print(json.dumps({"event": event.event_id, "alert_status": receipt.status,
                              "error_type": "GateBlocked", "alert_subject": alert["Subject"]},
                             ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"event": event.event_id, "alert_status": "failed",
                              "error_type": type(exc).__name__}, ensure_ascii=False), flush=True)


def _simulate(guardian: Guardian, trade_date: str, clock_range: str, tick_seconds: int,
              *, opts=None, events=()):
    start_text, sep, end_text = clock_range.partition("..")
    if not sep:
        start_text, sep, end_text = clock_range.partition("-")
    if not sep:
        raise ValueError("--simulate-clock must be HH:MM[:SS]..HH:MM[:SS]")
    def parse(value):
        if len(value.split(":")) == 2:
            value += ":00"
        return datetime.fromisoformat(f"{trade_date}T{value}+08:00")
    cursor, end = parse(start_text), parse(end_text)
    while cursor <= end:
        result = guardian.tick(cursor)
        if opts is not None:
            _notify_gate_missed(result, events, opts, cursor)
        for row in result:
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
    parser.add_argument("--email-self", action="store_true", help="send reports only to self")
    args = parser.parse_args(argv)
    if args.td_write and not args.write:
        parser.error("--td-write requires --write")
    if args.email_self and not args.run:
        parser.error("--email-self requires --run")
    if args.run and (args.once_now or args.list):
        parser.error("--run cannot be combined with --once-now or --list")
    if args.tick_seconds <= 0:
        parser.error("--tick-seconds must be positive")
    root = Path(args.artifact_root).expanduser()
    execute = args.run and not args.simulate_clock
    if execute:
        print(json.dumps({"calendar_startup_self_check":
                          TradeCalendarStore(root).refresh_if_needed()}, ensure_ascii=False))
    if execute:
        state_root = root / "guardian"
    else:
        # Simulations and inspection commands must never touch the live guardian
        # state or contend for its process lock.
        state_root = Path(tempfile.mkdtemp(prefix="core-guardian-dry-run-"))
        atexit.register(shutil.rmtree, state_root, ignore_errors=True)
    trade_date = args.simulate_date or date.today().isoformat()
    schedule_path = Path(args.schedule).expanduser() if args.schedule else root / "guardian" / "schedule.json"
    packaged_schedule = Path(__file__).resolve().parents[1] / "src" / "engine_core" / "guardian" / "schedule.json"
    try:
        if schedule_path.exists():
            configured, schedule_config = load_schedule(schedule_path, create_default=False)
        elif execute:
            configured, schedule_config = load_schedule(schedule_path, create_default=True)
            append_record(schedule_path.parent, {"type": "CONFIG_APPLIED", "revision": schedule_config["revision"],
                                                 "new_hash": __import__("engine_core.guardian.management", fromlist=["schedule_hash"]).schedule_hash(schedule_config),
                                                 "result": "INITIALIZED"})
        else:
            configured, schedule_config = load_schedule(packaged_schedule, create_default=False)
    except (OSError, ValueError, TypeError) as exc:
        print("schedule_invalid:" + str(exc), file=sys.stderr)
        return 2
    if not any(event.event_id == "calendar_refresh_0800" for event in configured):
        refresh_event = next(event for event in default_events()
                             if event.event_id == "calendar_refresh_0800")
        configured = tuple(sorted((*configured, refresh_event), key=lambda item: (item.start, item.event_id)))
    disabled = [event.event_id for event in configured if event.handler == "close_capture"]
    if disabled:
        print(json.dumps({"status": "SKIPPED", "handler": "close_capture",
                          "reason": "DISABLED_NOT_IMPLEMENTED", "events": disabled}, ensure_ascii=False), flush=True)
    selected = [event for event in configured
                if event.handler != "close_capture" and
                (not args.events or event.event_id in set(args.events.split(",")))]
    gates_data = _build_gate_status(args.gate_status)
    if args.simulate_clock and args.simulate_date and not args.gate_status:
        # A simulation can exercise the time sequence without data connections.
        gates_data = {event.gate: True for event in selected}
    gates = default_gates()
    gates.update({name: (lambda _event, _now, _ctx, value=value: bool(value))
                  for name, value in gates_data.items()})
    gates.setdefault("always", lambda *_: True)
    gates.setdefault("calendar_ready", lambda *_: True)
    handlers = {name: _command_handler(name, args, root) for name in
                {event.handler for event in selected}}
    if args.adapter:
        adapter = _module_callable(args.adapter)()
        gates.update(adapter.get("gates", {}))
        handlers.update(adapter.get("handlers", {}))
    guardian = Guardian(selected, calendar_dates=_load_dates(root, args.calendar),
                        state_root=state_root, gates=gates, handlers=handlers,
                        dry_run=not execute, context={"write": args.write, "td_write": args.td_write},
                        schedule_path=schedule_path if schedule_path.exists() else None,
                        config_revision=schedule_config["revision"], config_payload=schedule_config)
    global _ACTIVE_GUARDIAN
    _ACTIVE_GUARDIAN = guardian
    if args.list:
        for event in selected:
            print(json.dumps({"id": event.event_id, "phase": event.phase, "window": [event.start, event.deadline],
                              "gate": event.gate, "handler": event.handler,
                              "status": guardian.status(trade_date, event.event_id)}, ensure_ascii=False))
        return 0
    if args.simulate_clock:
        if not args.simulate_date:
            parser.error("--simulate-clock requires --simulate-date")
        _simulate(guardian, trade_date, args.simulate_clock, max(1, int(args.tick_seconds)),
                  opts=args, events=selected)
        return 0
    if args.once_now:
        lock = FileInstanceLock(state_root / "guardian.lock")
        try:
            lock.acquire()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            now = datetime.now().astimezone()
            actions = guardian.tick(now)
            _notify_gate_missed(actions, selected, args, now)
            guardian.write_heartbeat(now)
            for row in actions:
                print(json.dumps(row, ensure_ascii=False, sort_keys=True))
            return 0
        finally:
            lock.release()
    lock = FileInstanceLock(state_root / "guardian.lock")
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
                _notify_gate_missed([row], selected, args, now)
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
