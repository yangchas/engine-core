#!/usr/bin/env python3
"""Python adapter for the guardian-native line-oriented handler contract."""
from __future__ import annotations

import argparse
import json
import os
import re
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


def _premarket_owner_is_open(request):
    active = request.get("active_events")
    if not isinstance(active, dict):
        return False
    event_id = str(request.get("event") or "")
    # These owner events run the same broad prep_trade_day command. Avoid a
    # second command while another same-date premarket owner is still active.
    return any(owner != event_id and owner in active
               for owner in ("startup_0830", "premarket_slice_0850"))


def _send_load_defer_alert(request, result, dry_run):
    detail = result.get("detail") if isinstance(result, dict) else None
    pending = detail.get("load_defer_alert") if isinstance(detail, dict) else None
    if not isinstance(pending, dict):
        return
    alert = _send_alert({"event": pending.get("event", "daily_kline_load_deferred"),
                         "trade_date": request.get("trade_date"), "now": request.get("now"),
                         "reason": pending.get("reason", "daily_kline_deferred_by_load"),
                         "error": (f"partition={pending.get('partition')} "
                                   f"windows={pending.get('consecutive_windows')} "
                                   f"next_window={pending.get('next_window')} "
                                   f"load5={pending.get('load5')} "
                                   f"threshold={pending.get('load_threshold')}"),
                         }, dry_run)
    pending["delivery"] = alert


def _send_td_coverage_alert(request, result, dry_run):
    detail = result.get("detail") if isinstance(result, dict) else None
    pending = detail.get("td_coverage_alert") if isinstance(detail, dict) else None
    if not isinstance(pending, dict):
        return
    pending["delivery"] = _send_alert({"event": pending.get("event", "daily_kline_td_unreadable"),
                                       "trade_date": request.get("trade_date"), "now": request.get("now"),
                                       "reason": pending.get("reason", "daily_kline_td_selfcheck_unknown"),
                                       "error": pending.get("error")}, dry_run)


def _failure_classes(request):
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    configured = args.get("failure_classes")
    if isinstance(configured, dict):
        return configured
    return {
        "retryable": {
            "exit_codes": [30, 75],
            "exception_types": ["TimeoutError", "TimeoutExpired", "ConnectionError"],
            "error_codes": ["STALL_NO_PROGRESS", "BAOSTOCK_TIMEOUT", "BAOSTOCK_CONNECTION_ERROR",
                            "BAOSTOCK_LOGIN_FAILED", "BAOSTOCK_LOGIN_ERROR",
                            "BAOSTOCK_RATE_LIMITED", "BAOSTOCK_RECEIVE_EXCEPTION",
                            "FETCH_TIMEOUT", "RATE_LIMITED"],
            "message_patterns": ["receive exception", "connection error", "connection_error",
                                 "rate limit", "too many requests", "login failed",
                                 "login_error", "baostock_login_failed"],
        },
        "deterministic": {
            "exit_codes": [2, 40, 64],
            "exception_types": ["AssertionError", "AttributeError", "IndexError", "KeyError",
                                "NameError", "SyntaxError", "TypeError", "ValueError",
                                "JSONDecodeError", "ContractError"],
            "error_codes": ["COMMAND_ARGUMENT_ERROR", "CONFIG_ERROR", "CONTRACT_ERROR",
                            "CONTRACT_INVALID", "CONTRACT_VIOLATION", "POSTCONDITION_FAILED"],
            "message_patterns": [],
        },
        "default": "deterministic",
    }


def _classify_failure(request, *, error=None, result=None):
    classes = _failure_classes(request)
    details = result.get("detail") if isinstance(result, dict) else None
    texts = [str(error)] if error is not None else []
    codes = []
    exception_types = set()
    if error is not None:
        exception_types.update(cls.__name__ for cls in type(error).__mro__)
        code = getattr(error, "error_code", None)
        if code:
            codes.append(str(code))
        for line in str(error).splitlines():
            try:
                diagnostic = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(diagnostic, dict):
                continue
            code = diagnostic.get("error_code")
            if code:
                codes.append(str(code))
            for key in ("error", "detail", "message", "stderr_tail"):
                value = diagnostic.get(key)
                if value:
                    texts.append(str(value))
            detail = diagnostic.get("detail")
            if isinstance(detail, dict):
                for key in ("error_code", "error", "stderr_tail"):
                    value = detail.get(key)
                    if key == "error_code" and value:
                        codes.append(str(value))
                    elif value:
                        texts.append(str(value))
    if isinstance(result, dict):
        for key in ("error_code", "error", "detail", "stderr_tail"):
            value = result.get(key)
            if key == "error_code" and value:
                codes.append(str(value))
            elif value:
                texts.append(str(value))
        if isinstance(details, dict):
            for key in ("error_code", "error", "stderr_tail"):
                value = details.get(key)
                if key == "error_code" and value:
                    codes.append(str(value))
                elif value:
                    texts.append(str(value))
    combined = "\n".join(texts + codes).lower()
    exit_codes = {int(value) for value in re.findall(r"command_exit=(\d+)", combined)}
    exit_codes.update(int(value) for value in re.findall(r"fetch_exit_(\d+)", combined))
    normalized_codes = {value.upper() for value in codes}

    for category in ("deterministic", "retryable"):
        rules = classes.get(category)
        if not isinstance(rules, dict):
            continue
        configured_codes = {str(value).upper() for value in rules.get("error_codes", [])}
        configured_exceptions = {str(value) for value in rules.get("exception_types", [])}
        configured_exits = {int(value) for value in rules.get("exit_codes", [])}
        patterns = [str(value).lower() for value in rules.get("message_patterns", [])]
        if (normalized_codes & configured_codes or exit_codes & configured_exits
                or exception_types & configured_exceptions
                or any(pattern and pattern in combined for pattern in patterns)):
            return category
    default = classes.get("default", "deterministic")
    return default if default in {"deterministic", "retryable"} else "deterministic"


def _failure_response(request, *, error=None, result=None):
    category = _classify_failure(request, error=error, result=result)
    payload = dict(result) if isinstance(result, dict) else {
        "contract": 1, "status": "failed", "artifacts": [],
        "detail": str(error) if error is not None else "command_failed",
    }
    payload["contract"] = 1
    payload["status"] = "failed"
    if category == "deterministic":
        payload["error_code"] = "DETERMINISTIC_FAILURE"
        payload["retryable"] = False
        return 40, payload
    code = getattr(error, "error_code", None) if error is not None else None
    if not code and isinstance(result, dict):
        code = result.get("error_code")
        details = result.get("detail")
        if not code and isinstance(details, dict):
            code = details.get("error_code")
    if not code and error is not None:
        match = re.search(r"command_exit=(\d+)", str(error))
        code = "COMMAND_EXIT_" + match.group(1) if match else type(error).__name__.upper()
    payload["error_code"] = str(code or "RETRYABLE_FAILURE")
    payload["retryable"] = True
    return 75, payload


def _run(request):
    from examples.core_guardian import _command_handler

    base, event_id = _event_from_request(request)
    configured_handler = str(request.get("handler") or (base.handler if base else ""))
    if configured_handler == "premarket_data" and _premarket_owner_is_open(request):
        return 0, {"contract": 1, "status": "ok", "error_code": "",
                   "retryable": False, "artifacts": [],
                   "detail": "same_date_premarket_owner_still_running"}
    if configured_handler in {"boot_trade_day", "boot_data_health", "boot_reconcile", "boot_apply_plan", "fetch_item"}:
        from engine_core.guardian.startup import boot_apply_plan, boot_data_health, boot_reconcile, boot_trade_day, fetch_item
        trade_date = request.get("trade_date")
        if not trade_date:
            return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                        "retryable": False, "detail": "trade_date_required"}
        root = Path(os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser()
        dry = os.environ.get("GUARDIAN_DRY_RUN") == "1"
        now = request.get("now")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        reconcile_options = {
            "reconcile_max_load5": args.get("reconcile_max_load5", "cpus"),
            "daily_kline_load_defer_window_limit": args.get("daily_kline_load_defer_window_limit", 3),
        }
        if configured_handler == "boot_trade_day":
            result = boot_trade_day(str(trade_date), root)
        elif configured_handler == "boot_data_health":
            result = boot_data_health(str(trade_date), request.get("prev_trade_date"), root,
                                      now=now, dry_run=dry)
        elif configured_handler == "boot_reconcile":
            result = boot_reconcile(str(trade_date), str(request.get("phase") or ""),
                                    request.get("health"), root, now=now, **reconcile_options)
            _send_load_defer_alert(request, result, dry)
            _send_td_coverage_alert(request, result, dry)
        elif configured_handler == "boot_apply_plan":
            reconciled = boot_reconcile(str(trade_date), str(request.get("phase") or ""),
                                        request.get("health"), root, now=now, **reconcile_options)
            runtime_reconcile = str(request.get("event") or "").startswith("runtime_reconcile_")
            plan = reconciled["detail"].get("plan", [])
            only = args.get("only_items")      # acceptance runs (C577): restrict a real reconcile pass to named items
            if isinstance(only, list) and only:
                plan = [row for row in plan if row.get("id") in only]
            result = boot_apply_plan(str(trade_date), plan, root,
                                     now=now, phase=str(request.get("phase") or ""), dry_run=dry,
                                     enqueue_only=runtime_reconcile,
                                     active_events=request.get("active_events"),
                                     schedule_args=args)
            if reconciled["detail"].get("load_defer_alert"):
                result.setdefault("detail", {})["load_defer_alert"] = reconciled["detail"]["load_defer_alert"]
            if reconciled["detail"].get("td_coverage_alert"):
                result.setdefault("detail", {})["td_coverage_alert"] = reconciled["detail"]["td_coverage_alert"]
            _send_load_defer_alert(request, result, dry)
            _send_td_coverage_alert(request, result, dry)
        else:
            result = fetch_item(str(request.get("item_id") or ""), str(request.get("data_date") or trade_date),
                                root, dry_run=dry, now=now,
                                phase=str(request.get("phase") or "") or None,
                                schedule_args=args)
            upgrade = result.get("detail", {}).get("upgrade")
            if upgrade and upgrade.get("escalated_now"):
                result["detail"]["alert"] = _send_alert({
                    "event": "daily_kline_upgrade", "trade_date": str(trade_date),
                    "now": now, "reason": "GAP_ESCALATED"}, dry)
        if result["status"] == "failed":
            return _failure_response(request, result=result)
        return 0, result
    if not configured_handler:
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                    "retryable": False, "detail": "unknown_event:" + event_id}
    if base is not None and request.get("handler") and base.handler != configured_handler:
        return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                    "retryable": False, "detail": "handler_event_mismatch:" + event_id}
    name = configured_handler
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
    asof = str(request.get("asof") or f"{trade_date}T{getattr(base, 'start', '00:00:00')}+08:00")
    if name == "auction_report" and asof[11:19] > "09:26:00":
        asof = asof[:11] + "09:26:00" + asof[19:]  # panorama rejects an as-of after 09:26:00; the 09:26:30 start only delays the run
    start = datetime.fromisoformat(asof).strftime("%H:%M:%S")
    args =request.get("args") if isinstance(request.get("args"), dict) else {}
    if name == "daily_kline_backfill":
        from engine_core.guardian.daily_kline_backfill import run_daily_kline_backfill
        from engine_core.prep_trade_day import previous_trade_date
        target = target_day
        if args.get("target") == "prev_trade_day":
            target = previous_trade_date(root, str(trade_date)) or ""
        if not target:
            return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                        "retryable": False, "detail": "backfill_target_unavailable"}
        try:
            result = run_daily_kline_backfill(root, target, args=args)
        except Exception as exc:
            return _failure_response(request, error=exc)
        if result.get("status") == "WAITING":
            return 0, {"contract": 1, "status": "ok", "error_code": "", "retryable": False,
                       "artifacts": [], "detail": result}
        if result.get("status") == "NOT_ELIGIBLE":
            return 0, {"contract": 1, "status": "ok", "error_code": "",
                        "retryable": False, "artifacts": [], "detail": result}
        if result.get("status") not in {"OK", "NO_GAPS"}:
            return 75, {"contract": 1, "status": "failed", "error_code": "BAOSTOCK_GAPFILL_RETRY",
                        "retryable": True, "artifacts": [], "detail": result}
        return 0, {"contract": 1, "status": "ok", "error_code": "", "retryable": False,
                   "artifacts": [], "detail": result}
    if name == "baostock_probe" and isinstance(args.get("baostock_gapfill_policy"), dict) and not dry:
        from engine_core.guardian.daily_kline_backfill import run_daily_kline_backfill
        gap_policy = args["baostock_gapfill_policy"]
        try:
            result = run_daily_kline_backfill(
                root, target_day, args={**args, "minimum_coverage": gap_policy.get("minimum_coverage", 0.90)},
                only_source_empty=True)
        except Exception as exc:
            if "BAOSTOCK_GAP_UNRESOLVED" in str(exc):
                return 75, {"contract": 1, "status": "failed", "error_code": "TIER_PARTIAL",
                            "retryable": True, "artifacts": [], "detail": str(exc)}
            return _failure_response(request, error=exc)
        if result.get("status") in {"OK", "NO_GAPS"}:
            return 0, {"contract": 1, "status": "ok", "error_code": "", "retryable": False,
                       "artifacts": [], "detail": result}
        if result.get("status") == "WAITING":
            return 75, {"contract": 1, "status": "failed", "error_code": "BAOSTOCK_LOCAL_GAP_WAIT",
                        "retryable": True, "artifacts": [], "detail": result}
        if result.get("status") in {"NOT_ELIGIBLE", "NO_GAPS"}:
            return 20, {"contract": 1, "status": "not_applicable", "error_code": "",
                        "retryable": False, "artifacts": [], "detail": result}
        return 75, {"contract": 1, "status": "failed", "error_code": "BAOSTOCK_GAPFILL_RETRY",
                    "retryable": True, "artifacts": [], "detail": result}
    event = SimpleNamespace(event_id=event_id, handler=name, gate=getattr(base, "gate", "always"), start=start,
                            args=args, timeout_seconds=int(request.get("timeout_seconds") or getattr(base, "timeout_seconds", 300)),
                            data_date_offset=getattr(base, "data_date_offset", 0))
    now = datetime.fromisoformat(asof)
    if name == "intraday_data" and args.get("dataset") == "limit_up_pool_today":
        from engine_core.guardian.limit_up_today import run as run_limit_up_today
        today_request = {**request, "data_date": target_day, "args": args}
        if request.get("asof") or request.get("now"):
            today_request["as_of"] = request.get("asof") or request.get("now")
        return run_limit_up_today(today_request, root, dry_run=dry)
    if name == "daily_kline_close":
        from engine_core.guardian.daily_kline_close import run_close_tier
        return run_close_tier(request, root, dry_run=dry)
    if name == "kaipan_fetch" and args.get("dataset") == "limit_up_pool":
        # C580: D-1 limit-up pool ladder rung (kaipan / wencai / td per schedule.json); other kaipan_fetch events
        # (kaipan_refresh_0850: reasons, hot plates, members) keep the command path below.
        from engine_core.guardian.limit_pool_d1 import run_pool_tier
        return run_pool_tier({**request, "data_date": target_day}, root, dry_run=dry)
    checkpoint_reply = None
    if name == "intraday_data" and args.get("checkpoint") is not None:
        # C587: the time-critical part is the Redis q2 checkpoint (source order / tolerance in schedule.json). It runs
        # first; the legacy hot_plates refresh below is independent of ticks and may never fail the event, because a
        # retry could no longer reproduce the checkpoint window.
        from engine_core.guardian.intraday_checkpoint_run import run_checkpoint
        code, checkpoint_reply = run_checkpoint({**request, "start": start, "data_date": target_day}, root, dry_run=dry)
        if code not in (0, 10):
            return code, checkpoint_reply
    if name == "close_capture":
        return 0, {"contract": 1, "status": "not_applicable", "error_code": "",
                   "retryable": False, "artifacts": [], "detail": "close_capture_not_implemented"}
    try:
        result = _command_handler(name, opts, root)(event, now, {},
                                                    bool(request.get("degrade_requested")))
        if not dry:
            from engine_core.guardian.commands import validate_handler_postcondition
            ok, reason = validate_handler_postcondition(name, target_day, root, args.get("datasets"))
            if not ok:
                return _failure_response(request, result={
                    "contract": 1, "status": "failed", "error_code": "POSTCONDITION_FAILED",
                    "retryable": False, "artifacts": [], "detail": reason})
        degraded = isinstance(result, str) and result.startswith("DEGRADED:")
        if checkpoint_reply is not None:
            degraded = degraded or checkpoint_reply["status"] == "degraded"
            result = {"checkpoint": checkpoint_reply["detail"], "hot_plates": str(result)[-300:]}
        return 0, {"contract": 1, "status": "degraded" if degraded else "ok",
                   "error_code": "", "retryable": False, "artifacts": [], "detail": result}
    except Exception as exc:
        if checkpoint_reply is not None:
            return 0, {"contract": 1, "status": "degraded", "error_code": "", "retryable": False, "artifacts": [],
                       "detail": {"checkpoint": checkpoint_reply["detail"],
                                  "hot_plates_error": f"{type(exc).__name__}:{str(exc)[:200]}"}}
        return _failure_response(request, error=exc)


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
    asof = str(request.get("asof") or "")
    if len(asof) >= 19 and asof[10] == "T":
        # C587: the runtime's own start time and args are authoritative (same values as schedule.json in production);
        # this lets a shifted-trigger acceptance run gate on its real cut-off instead of the repository's.
        import dataclasses
        sent = request.get("args") if isinstance(request.get("args"), dict) and request.get("args") else None
        due = str(request.get("deadline_at") or "")
        deadline = due[11:19] if len(due) >= 19 and due[10] == "T" else max(asof[11:19], event.deadline)
        event = dataclasses.replace(event, start=asof[11:19], deadline=deadline,
                                    args=sent if sent is not None else event.args)
    try:
        now = datetime.fromisoformat(request.get("now")) if request.get("now") else None
        if now is not None and now.tzinfo is None:
            from zoneinfo import ZoneInfo
            now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except (TypeError, ValueError):
        now = None
    now = now or datetime.now().astimezone()
    try:
        result = fn(event, now, {})
        payload = dict(result) if isinstance(result, dict) else {
            "status": "ready" if bool(result) else "not_ready",
            "reason": "gate_satisfied" if bool(result) else "gate_condition_false"}
        ready = payload.get("status") == "ready"
        _json(payload)
        return 0 if ready else 75
    except Exception as exc:
        _json({"status": "not_ready", "reason": f"{type(exc).__name__}:{exc}"})
        return 75


def _send_alert(request, dry_run):
    from engine_core.report_mailer import build_alert_message, load_smtp_config, send_message

    inner = request.get("request") if isinstance(request.get("request"), dict) else request
    reason = str(request.get("reason") or inner.get("reason") or "unspecified")
    try:
        now_day = datetime.fromisoformat(request.get("now")).date().isoformat() if request.get("now") else None
    except (TypeError, ValueError):
        now_day = None
    day = inner.get("date") or request.get("trade_date") or now_day or datetime.now().date().isoformat()
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
    if reason == "TIER_FALLBACK" and inner.get("error"):
        # failed tier(s) -> succeeded tier, e.g. "wencai:DETERMINISTIC_FAILURE -> q2_final"
        lines = [f"{line} {inner['error']}" for line in lines]
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
    return {"status": receipt.status, "to": "412826313@qq.com", "events": len(events),
            "subject": message["Subject"], "body": "Guardian 告警汇总\n" + "\n".join(lines)}


def _alert(request, dry_run):
    _json(_send_alert(request, dry_run))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--handler", default="")
    gate = sub.add_parser("gate")
    gate.add_argument("name", nargs="?", default="")
    alert = sub.add_parser("alert")
    alert.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        request = json.load(sys.stdin) if args.command in {"run", "alert", "gate"} else {}
        if args.command == "run":
            if args.handler:
                request["handler"] = args.handler
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
