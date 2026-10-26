#!/usr/bin/env python3
"""Local, standard-library-only handler used by guardian_selftest.py."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")


def _append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _read_request() -> dict:
    value = json.load(sys.stdin)
    return value if isinstance(value, dict) else {}


def _reply(value: dict) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _run(request: dict) -> int:
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    behavior = str(args.get("behavior", "ok"))
    calls_path = Path(os.environ["SELFTEST_DIR"]) / "calls.jsonl"
    prior_rows = []
    if calls_path.is_file():
        for line in calls_path.read_text(encoding="utf-8").splitlines():
            try:
                prior_rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    already_satisfied = behavior == "already_satisfied" and any(row.get("event") == request.get("event") for row in prior_rows)
    fail_first_instance = (
        behavior == "fail_first_instance_then_ok"
        and request.get("instance") == args.get("fail_instance")
    )
    record = {
        "event": request.get("event"),
        "instance": request.get("instance"),
        "attempt": request.get("attempt"),
        "wall_clock": datetime.now(TZ).isoformat(timespec="milliseconds"),
        "trade_date": request.get("trade_date"),
        "data_date": request.get("data_date"),
        "asof": request.get("asof"),
        "degrade_requested": bool(request.get("degrade_requested", False)),
        "tier": request.get("tier", ""),
        "degraded": bool(request.get("degraded", False)),
        "skipped_tiers": request.get("skipped_tiers", []),
        "args": args,
    }
    _append_jsonl(calls_path, record)

    if args.get("record_interval") is True:
        intervals = Path(os.environ["SELFTEST_DIR"]) / "attempt_intervals.jsonl"
        base = {"event": request.get("event"), "pid": os.getpid()}
        _append_jsonl(intervals, {**base, "edge": "start", "wall_clock": datetime.now(TZ).isoformat(timespec="milliseconds")})
        atexit.register(lambda: _append_jsonl(
            intervals,
            {**base, "edge": "end", "wall_clock": datetime.now(TZ).isoformat(timespec="milliseconds")},
        ))

    if behavior == "ready_gate":
        gate_ref = str(args.get("gate_ref") or "")
        if gate_ref:
            gate_path = Path(os.environ["SELFTEST_DIR"]) / "gates" / gate_ref
            gate_path.parent.mkdir(parents=True, exist_ok=True)
            gate_path.write_text("ready\n", encoding="utf-8")

    attempt = request.get("attempt", 1)
    try:
        attempt = int(attempt)
    except (TypeError, ValueError):
        attempt = 1

    if behavior.startswith("sleep:"):
        try:
            delay = max(0.0, float(behavior.partition(":")[2]))
        except ValueError:
            delay = 0.0
        time.sleep(delay)
        behavior = "ok"

    if behavior.startswith("sleep_once:"):
        try:
            delay = max(0.0, float(behavior.partition(":")[2]))
        except ValueError:
            delay = 0.0
        if attempt <= 1:
            time.sleep(delay)
        behavior = "ok"

    if behavior == "flaky_then_ok":
        if attempt <= 1:
            _reply({"contract": 1, "status": "failed", "error_code": "SELFTEST_RETRY", "retryable": True,
                    "artifacts": [], "detail": "selftest retryable first attempt"})
            return 30
        behavior = "ok"

    if fail_first_instance:
        _reply({"contract": 1, "status": "failed", "error_code": "SELFTEST_RETRY", "retryable": True,
                "artifacts": [], "detail": "selftest first backfill trigger fails"})
        return 30
    if behavior == "fail_first_instance_then_ok":
        behavior = "ok"

    if behavior == "available_after":
        available_after = datetime.fromisoformat(str(args.get("available_after", "")))
        if datetime.now(TZ) < available_after:
            _reply({"contract": 1, "status": "failed", "error_code": "TIER_NO_DATA", "retryable": False,
                    "artifacts": [], "detail": "selftest data is not available yet"})
            return 40
        behavior = "ok"

    if behavior == "no_data":
        _reply({"contract": 1, "status": "failed", "error_code": "TIER_NO_DATA", "retryable": False,
                "artifacts": [], "detail": "no data at configured tier"})
        return 40

    if behavior == "already_satisfied" and not already_satisfied:
        behavior = "ok"

    if already_satisfied:
        _reply({"contract": 1, "status": "not_applicable", "error_code": "ALREADY_SATISFIED", "retryable": False,
                "artifacts": [], "detail": "target was satisfied by an earlier trigger"})
        return 20

    if behavior.startswith("fail:"):
        try:
            fail_count = max(0, int(behavior.partition(":")[2]))
        except ValueError:
            fail_count = 0
        behavior = "fail_always" if attempt <= fail_count else "ok"

    if behavior in {"fail_always", "need_degrade"} and not (
        behavior == "need_degrade" and request.get("degrade_requested") is True
    ):
        _reply({
            "contract": 1,
            "status": "failed",
            "error_code": "INTERNAL_ERROR",
            "retryable": True,
            "artifacts": [],
            "detail": "selftest requested a retryable failure",
        })
        return 75

    if behavior == "permanent_fail":
        _reply({
            "contract": 1,
            "status": "failed",
            "error_code": "SELFTEST_PERMANENT_ERROR",
            "retryable": False,
            "artifacts": [],
            "detail": "selftest requested a permanent failure",
        })
        return 0

    if behavior == "degraded":
        status = "degraded"
    elif behavior == "not_applicable":
        status = "not_applicable"
    elif behavior in {"ok", "need_degrade", "ready_gate"}:
        status = "ok"
    else:
        _reply({
            "contract": 1,
            "status": "failed",
            "error_code": "CONFIG_ERROR",
            "retryable": False,
            "artifacts": [],
            "detail": f"unknown selftest behavior: {behavior}",
        })
        return 64

    _reply({
        "contract": 1,
        "status": status,
        "error_code": "",
        "retryable": False,
        "artifacts": [],
        "detail": "selftest handler completed",
    })
    return 0


def _gate(request: dict) -> int:
    gate = request.get("gate") if isinstance(request.get("gate"), dict) else {}
    name = str(gate.get("ref") or request.get("name") or "")
    path = Path(os.environ["SELFTEST_DIR"]) / "gates" / name
    ready = path.is_file() and path.read_text(encoding="utf-8").strip() == "ready"
    _reply({"status": "ready" if ready else "not_ready", "reason": name or "missing_gate_name"})
    return 0 if ready else 75


def _alert(request: dict) -> int:
    record = {
        "wall_clock": datetime.now(TZ).isoformat(timespec="milliseconds"),
        "request": request,
    }
    _append_jsonl(Path(os.environ["SELFTEST_DIR"]) / "alerts.jsonl", record)
    _reply({"status": "sent"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "gate", "alert"))
    args = parser.parse_args(argv)
    try:
        request = _read_request()
        if args.command == "run":
            return _run(request)
        if args.command == "gate":
            return _gate(request)
        return _alert(request)
    except Exception as exc:
        if args.command == "run":
            _reply({
                "contract": 1,
                "status": "failed",
                "error_code": "INTERNAL_ERROR",
                "retryable": True,
                "artifacts": [],
                "detail": f"{type(exc).__name__}: {exc}",
            })
            return 75
        _reply({"status": "not_ready" if args.command == "gate" else "failed",
                "reason": f"{type(exc).__name__}: {exc}"})
        return 75 if args.command == "gate" else 1


if __name__ == "__main__":
    raise SystemExit(main())
