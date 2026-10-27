#!/usr/bin/env python3
"""Fake Baostock/wencai/q2_final handler for the guardian trigger check.

Standard library only; never touches the network or any production path.
The behaviour of one ladder tier is read from that tier's ``args``:

- ``provider``: label written to the call log (``baostock``/``wencai``/``q2_final``).
- ``script``: list of outcomes, one per call of this provider within the same
  event. The last outcome repeats once the list is exhausted.

Outcomes:
- ``ok``       exit 0, status ok
- ``degraded`` exit 10, status degraded
- ``retry30``  exit 30, retryable BAOSTOCK_TIMEOUT
- ``retry75``  exit 75, retryable BAOSTOCK_RATE_LIMITED
- ``det40``    exit 40, deterministic CONTRACT_VIOLATION (not TIER_NO_DATA)
- ``nodata40`` exit 40, TIER_NO_DATA
- ``wait_retry30`` wait for ``release.json``, then return retryable BAOSTOCK_TIMEOUT
- ``hang``     sleeps until the runtime kills the process group (timeout)

Idempotency: once a call succeeds (ok/degraded) a marker is written for the
data date; later triggers for the same data date answer exit 20
ALREADY_SATISFIED without calling the provider again.

Environment: ``BAOSTOCK_FAKE_DIR`` must point at the scenario directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _dir() -> Path:
    return Path(os.environ["BAOSTOCK_FAKE_DIR"])


def _append(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line)


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _reply(value: dict) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


OUTCOMES = {
    "ok": (0, {"status": "ok", "error_code": "", "retryable": False}),
    "degraded": (10, {"status": "degraded", "error_code": "", "retryable": False}),
    "retry30": (30, {"status": "failed", "error_code": "BAOSTOCK_TIMEOUT", "retryable": True}),
    "retry75": (75, {"status": "failed", "error_code": "BAOSTOCK_RATE_LIMITED", "retryable": True}),
    "det40": (40, {"status": "failed", "error_code": "CONTRACT_VIOLATION", "retryable": False}),
    "nodata40": (40, {"status": "failed", "error_code": "TIER_NO_DATA", "retryable": False}),
}


def _run(request: dict) -> int:
    root = _dir()
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    provider = str(args.get("provider", "unknown"))
    script = [str(item) for item in args.get("script", ["ok"])] or ["ok"]
    event = str(request.get("event", ""))
    data_date = str(request.get("data_date", ""))
    now = request.get("now")
    marker = root / "satisfied" / f"{data_date}.json"
    calls = root / "calls.jsonl"

    prior = [row for row in _rows(calls) if row.get("provider") == provider and row.get("event") == event]
    index = len(prior) + 1

    if marker.is_file() and args.get("idempotent", True):
        outcome = "satisfied"
        exit_code = 20
        _append(calls, {"now": now, "event": event, "provider": provider, "index": index,
                        "attempt": request.get("attempt"), "tier": request.get("tier"),
                        "outcome": outcome, "exit": exit_code})
        _reply({"contract": 1, "status": "not_applicable", "error_code": "ALREADY_SATISFIED",
                "retryable": False, "artifacts": [], "detail": "data date already satisfied"})
        return exit_code

    outcome = script[min(index, len(script)) - 1]
    released_at = ""
    if outcome.startswith("wait_"):
        release_path = root / "release.json"
        release_deadline = time.monotonic() + 15.0
        while not release_path.is_file() and time.monotonic() < release_deadline:
            time.sleep(0.01)
        if not release_path.is_file():
            _append(calls, {"now": now, "event": event, "provider": provider, "index": index,
                            "attempt": request.get("attempt"), "tier": request.get("tier"),
                            "outcome": outcome, "exit": 64})
            _reply({"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                    "retryable": False, "artifacts": [], "detail": "timed out waiting for release.json"})
            return 64
        try:
            release = json.loads(release_path.read_text(encoding="utf-8"))
            released_at = str(release.get("now", ""))
        except (OSError, json.JSONDecodeError):
            released_at = ""
        outcome = outcome.removeprefix("wait_")
    if outcome == "hang":
        _append(calls, {"now": now, "event": event, "provider": provider, "index": index,
                        "attempt": request.get("attempt"), "tier": request.get("tier"),
                        "outcome": outcome, "released_at": released_at, "exit": "HANG"})
        time.sleep(3600)
        return 0  # not reached: the runtime kills the process group first

    if outcome not in OUTCOMES:
        _append(calls, {"now": now, "event": event, "provider": provider, "index": index,
                        "attempt": request.get("attempt"), "tier": request.get("tier"),
                        "outcome": outcome, "exit": 64})
        _reply({"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR",
                "retryable": False, "artifacts": [], "detail": f"unknown outcome {outcome}"})
        return 64

    exit_code, body = OUTCOMES[outcome]
    _append(calls, {"now": now, "event": event, "provider": provider, "index": index,
                    "attempt": request.get("attempt"), "tier": request.get("tier"),
                    "outcome": outcome, "released_at": released_at, "exit": exit_code})
    if exit_code in (0, 10):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"provider": provider, "now": now}) + "\n", encoding="utf-8")
    _reply({"contract": 1, **body, "artifacts": [], "detail": f"fake {provider} {outcome}"})
    return exit_code


def _alert(request: dict) -> int:
    _append(_dir() / "alerts.jsonl", {"reason": request.get("reason"), "request": request})
    _reply({"status": "sent"})
    return 0


def _gate(request: dict) -> int:
    gate = request.get("gate") if isinstance(request.get("gate"), dict) else {}
    name = str(gate.get("ref") or "")
    path = _dir() / "gates" / name
    ready = path.is_file() and path.read_text(encoding="utf-8").strip() == "ready"
    _reply({"status": "ready" if ready else "not_ready", "reason": name})
    return 0 if ready else 75


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "gate", "alert"))
    parsed = parser.parse_args(argv)
    try:
        value = json.load(sys.stdin)
        request = value if isinstance(value, dict) else {}
        if parsed.command == "run":
            return _run(request)
        if parsed.command == "gate":
            return _gate(request)
        return _alert(request)
    except Exception as exc:  # report, never crash silently
        if parsed.command == "run":
            _reply({"contract": 1, "status": "failed", "error_code": "INTERNAL_ERROR",
                    "retryable": True, "artifacts": [], "detail": f"{type(exc).__name__}: {exc}"})
            return 75
        _reply({"status": "failed", "reason": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
