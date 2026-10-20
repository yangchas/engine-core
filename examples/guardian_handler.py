#!/usr/bin/env python3
"""Python adapter for the guardian-native line-oriented handler contract."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _json(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _event_from_request(request):
    from engine_core.guardian.scheduler import default_events

    raw = request.get("event")
    if isinstance(raw, dict):
        raw = raw.get("id") or raw.get("event_id")
    by_id = {item.event_id: item for item in default_events()}
    return by_id.get(str(raw)), str(raw)


def _run(request):
    from examples.core_guardian import _command_handler

    base, event_id = _event_from_request(request)
    if event_id in {"boot_trade_day", "boot_data_health", "boot_reconcile"}:
        from engine_core.guardian.startup import boot_data_health, boot_reconcile, boot_trade_day
        trade_date = request.get("trade_date")
        if not trade_date:
            return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                        "retryable": False, "detail": "trade_date_required"}
        root = Path(os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser()
        dry = os.environ.get("GUARDIAN_DRY_RUN") == "1"
        now = request.get("now")
        if event_id == "boot_trade_day":
            result = boot_trade_day(str(trade_date), root)
        elif event_id == "boot_data_health":
            result = boot_data_health(str(trade_date), request.get("prev_trade_date"), root,
                                      now=now, dry_run=dry)
        else:
            result = boot_reconcile(str(trade_date), str(request.get("phase") or ""),
                                    request.get("health"), root, now=now)
        return (75 if result["status"] == "failed" and result.get("retryable") else 0), result
    if base is None:
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                    "retryable": False, "detail": "unknown_event:" + event_id}
    name = base.handler
    trade_date = request.get("trade_date")
    if not trade_date:
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                    "retryable": False, "detail": "trade_date_required"}
    root = Path(os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser()
    dry = os.environ.get("GUARDIAN_DRY_RUN") == "1"
    target_day = str(request.get("data_date") or trade_date)
    opts = SimpleNamespace(write=not dry, td_write=False, simulate_clock=dry,
                           email_self=not dry and os.environ.get("GUARDIAN_EMAIL_SELF", "1") == "1",
                           run=not dry)
    event = SimpleNamespace(event_id=base.event_id, handler=name, gate=base.gate, start=base.start,
                            timeout_seconds=int(request.get("timeout_seconds") or base.timeout_seconds),
                            data_date_offset=base.data_date_offset)
    now = datetime.fromisoformat(f"{trade_date}T{base.start}+08:00")
    if name == "close_capture":
        return 0, {"contract": 1, "status": "not_applicable", "error_code": "",
                   "retryable": False, "artifacts": [], "detail": "close_capture_not_implemented"}
    try:
        result = _command_handler(name, opts, root)(event, now, {},
                                                    bool(request.get("degrade_requested")))
        if not dry:
            from engine_core.guardian.commands import validate_handler_postcondition
            ok, reason = validate_handler_postcondition(name, target_day, root)
            if not ok:
                return 0, {"contract": 1, "status": "failed", "error_code": "POSTCONDITION_FAILED",
                           "retryable": True, "artifacts": [], "detail": reason}
        degraded = isinstance(result, str) and result.startswith("DEGRADED:")
        return 0, {"contract": 1, "status": "degraded" if degraded else "ok",
                   "error_code": "", "retryable": False, "artifacts": [], "detail": result}
    except Exception as exc:
        return 75, {"contract": 1, "status": "failed", "error_code": type(exc).__name__.upper(),
                    "retryable": True, "artifacts": [], "detail": str(exc)}


def _gate(name, request=None):
    from engine_core.guardian.gates import default_gates
    from engine_core.guardian.scheduler import default_events

    request = request or {}
    name = name or (request.get("gate") or {}).get("ref") or ""

    if name in {"always", "calendar_ready"}:
        _json({"status": "ready", "reason": name})
        return 0
    fn = default_gates().get(name)
    event = next((item for item in default_events() if item.event_id == request.get("event")), None) \
        or next((item for item in default_events() if item.gate == name), None)
    if fn is None or event is None:
        _json({"status": "not_ready", "reason": "unknown_gate"})
        return 64
    now = datetime.now().astimezone()
    try:
        ready = bool(fn(event, now, {}))
        _json({"status": "ready" if ready else "not_ready",
               "reason": "gate_satisfied" if ready else "gate_condition_false"})
        return 0 if ready else 75
    except Exception as exc:
        _json({"status": "not_ready", "reason": f"{type(exc).__name__}:{exc}"})
        return 75


def _alert(request, dry_run):
    from engine_core.report_mailer import build_alert_message, load_smtp_config, send_message

    inner = request.get("request") if isinstance(request.get("request"), dict) else request
    reason = str(request.get("reason") or inner.get("reason") or "unspecified")
    day = inner.get("date") or request.get("trade_date") or datetime.now().date().isoformat()
    listed = inner.get("instances") or inner.get("events")
    if listed:
        events = [{"event": str(x), "reason": reason} for x in listed]
    else:
        events = [{"event": inner.get("event", "unknown"), "reason": reason,
                   "error": inner.get("error")}] if inner is not request or "event" in inner else [request]
    grouped = {}
    for item in events:
        key = (str(item.get("event", "unknown")), str(item.get("reason", "unspecified")))
        grouped[key] = grouped.get(key, 0) + 1
    lines = [f"{event}: {reason} ({count})" for (event, reason), count in sorted(grouped.items())]
    from_addr = "reports@localhost"
    config = None
    if not dry_run:
        config = load_smtp_config()
        from_addr = config.from_addr
    message = build_alert_message(day, "guardian", RuntimeError("\n".join(lines)),
                                  "412826313@qq.com", from_addr)
    message.set_content("Guardian 告警汇总\n" + "\n".join(lines))
    receipt = send_message(config, message, dry_run=dry_run,
                           allow_recipients=["412826313@qq.com"])
    _json({"status": receipt.status, "to": "412826313@qq.com", "events": len(events),
           "subject": message["Subject"], "body": "Guardian 告警汇总\n" + "\n".join(lines)})
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    gate = sub.add_parser("gate")
    gate.add_argument("name", nargs="?", default="")
    alert = sub.add_parser("alert")
    alert.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        request = json.load(sys.stdin) if args.command in {"run", "alert", "gate"} else {}
        if args.command == "run":
            code, result = _run(request)
            _json(result)
            return code
        if args.command == "gate":
            return _gate(args.name, request)
        return _alert(request, args.dry_run)
    except Exception as exc:
        if args.command == "run":
            _json({"contract": 1, "status": "failed", "error_code": "INTERNAL_ERROR",
                   "retryable": True, "artifacts": [], "detail": str(exc)})
            return 1
        _json({"status": "not_ready" if args.command == "gate" else "failed",
               "reason": f"{type(exc).__name__}:{exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
