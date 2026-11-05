#!/usr/bin/env python3
"""Run data functions by themselves for one date and time slice - no daemon, no driver, no full test run (C692).

    run_function.py --date 2026-10-23 --event kaipan_hot_092110
    run_function.py --date 2026-10-23 --from 09:20:00 --until 09:30:00 [--handler kaipan_hot_capture]

The driver (guardiand) only decides WHEN a function runs and what to do with its answer; a function is the handler
behind ``examples/guardian_handler.py run --handler NAME`` that takes the request guardiand would send on stdin and
answers with the contract reply. This runs exactly that, once per selected event in schedule order, inside the sealed
replay environment (recorded Kaipan responses only; Baostock / WenCai / Redis / TD are recorded gaps; all internet
connections are gaps) with a throw-away data root. Each answer is checked against the reply contract and the gaps it
hit are listed. Gate checks, dependency order, retries and the clock are the driver's business and are not run here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))
HANDLER = ROOT / "examples" / "replay_handler.py"
SCHEDULE = ROOT / "src" / "engine_core" / "guardian" / "schedule.json"


def _clock(text: str) -> str:
    return text if text.count(":") == 2 else text + ":00"


def _at(event: dict[str, Any]) -> str | None:
    """The clock time an event is triggered at; boot-time events have none and only run by name."""
    value = event.get("at") or event.get("start")
    return _clock(value) if isinstance(value, str) else None


def _seconds(text: str) -> int:
    """A Go-style duration: 901s, 20m, 130m, 2h10m."""
    parts = re.findall(r"(\d+)([smh])", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError("unsupported duration " + text)
    return sum(int(n) * {"s": 1, "m": 60, "h": 3600}[u] for n, u in parts)


def _profile_rung(schedule: dict[str, Any], event: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
    """The first rung of the event's ladder: its name, its time budget and the args the rung adds to the request."""
    ladder = (schedule.get("profiles", {}).get(event.get("profile") or "") or {}).get("ladder") or [{}]
    return str(ladder[0].get("name") or "legacy"), _seconds(ladder[0].get("timeout") or "300s"), \
        dict(ladder[0].get("args") or {})


def _data_date(day: str, offset: int, dates: list[str]) -> str:
    if not offset:
        return day
    index = dates.index(day) + offset
    return dates[index] if 0 <= index < len(dates) else day


def request_for(event: dict[str, Any], day: str, dates: list[str], schedule: dict[str, Any]) -> dict[str, Any]:
    """The request guardiand sends a handler for this event on ``day`` (native/guardiand/runtime.go, buildRequest)."""
    tier, timeout, rung_args = _profile_rung(schedule, event)
    start = _at(event) or "00:00:00"
    return {"contract": 1, "event": event["event_id"], "instance": event["event_id"], "attempt": 1,
            "run_id": "function-" + event["event_id"], "idem_key": f"{day}/{event['event_id']}",
            "trade_date": day, "data_date": _data_date(day, int(event.get("data_date_offset") or 0), dates),
            "asof": f"{day}T{start}+08:00", "deadline_at": f"{day}T{_clock(event['deadline'])}+08:00",
            "degrade_requested": False, "tier": tier, "degraded": False, "skipped_tiers": [],
            "sandbox": {"on": False, "root": ""}, "args": {**(event.get("args") or {}), **rung_args},
            "timeout_seconds": timeout,
            "gate": {"ref": (event.get("gate") or ""), "params": {}, "probe": False}}


def select(schedule: dict[str, Any], *, event: str | None, start: str, until: str,
           handler: str | None) -> list[dict[str, Any]]:
    events = [e for e in schedule["events"] if e.get("enabled", True)]
    if event:
        chosen = [e for e in events if e["event_id"] == event]
        if not chosen:
            raise SystemExit(f"no event {event!r} in the schedule")
        return chosen
    chosen = [e for e in events if _at(e) and start <= _at(e) <= until and (not handler or e["handler"] == handler)]
    return sorted(chosen, key=lambda e: (_at(e), e["event_id"]))


def run_one(request: dict[str, Any], handler: str, work: Path) -> dict[str, Any]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(work / "home"),
           "CORE_DATA_ROOT": str(work / "core-data"), "CORE_REPLAY_DIR": str(work / "replay"),
           "CORE_PROVIDER_STATE": "memory", "REDIS_URL": "redis://replay.invalid:6379/0",
           "CORE_REDIS_URL": "redis://replay.invalid:6379/0", "TD_CONFIG": "replay-no-td",
           "PYTHONPATH": os.pathsep.join([str(ROOT / "examples" / "replay_site"), str(ROOT / "src"), str(ROOT)])}
    started = time.monotonic()
    done = subprocess.run([sys.executable, str(HANDLER), "run", "--handler", handler], input=json.dumps(request),
                          capture_output=True, text=True, env=env, cwd=str(ROOT),
                          timeout=max(60, int(request["timeout_seconds"]) + 60))
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    try:
        reply = json.loads(lines[-1]) if lines else None
    except ValueError:
        reply = None
    from engine_core.guardian.contract import check_reply
    return {"event": request["event"], "handler": handler, "exit": done.returncode, "reply": reply,
            "problems": check_reply(done.returncode, reply), "seconds": round(time.monotonic() - started, 1),
            "stderr_tail": done.stderr[-400:] if done.returncode not in (0, 10, 20) else ""}


def gaps_by_event(replay_dir: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    path = replay_dir / "unrecorded.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        row = json.loads(line)
        result.setdefault(row.get("instance") or "", []).append(f"{row['source']}.{row['call']}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True)
    parser.add_argument("--event")
    parser.add_argument("--from", dest="start", default="00:00:00")
    parser.add_argument("--until", default="23:59:59")
    parser.add_argument("--handler", help="with --from/--until: only events of this handler")
    parser.add_argument("--schedule-file", type=Path, default=SCHEDULE)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--json", action="store_true", help="print the full answers as JSON")
    args = parser.parse_args(argv)
    schedule = json.loads(args.schedule_file.read_text(encoding="utf-8"))
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="run-function-"))
    for sub in ("core-data", "replay", "home"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    from replay_day import _seed_calendar
    dates = _seed_calendar(work / "core-data")
    if args.date not in dates:
        raise SystemExit(f"{args.date} is not a trading day in the recorded calendar")
    events = select(schedule, event=args.event, start=_clock(args.start), until=_clock(args.until),
                    handler=args.handler)
    results = [run_one(request_for(event, args.date, dates, schedule), event["handler"], work) for event in events]
    gaps = gaps_by_event(work / "replay")
    bad = 0
    for item in results:
        reply = item["reply"] or {}
        item["gaps"] = sorted(set(gaps.get(item["event"], [])))
        bad += bool(item["problems"]) or item["reply"] is None
        if not args.json:
            print(f"{item['event']:34s} exit={item['exit']:<3} {reply.get('status', '?'):14s} "
                  f"{reply.get('error_code') or '-':28s} {item['seconds']:5.1f}s  "
                  + (("CONTRACT " + ",".join(item["problems"])) if item["problems"] else "contract ok")
                  + (f"  gaps={','.join(item['gaps'])}" if item["gaps"] else ""))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"{len(results)} function run(s), {bad} outside the contract; work dir {work}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
