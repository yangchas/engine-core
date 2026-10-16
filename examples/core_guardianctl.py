#!/usr/bin/env python3
"""Offline local controls for the Core guardian."""
import argparse
from collections import deque
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from engine_core.guardian.management import append_record, load_schedule
from engine_core.guardian.scheduler import TZ


def _root(value=None):
    return Path(value or os.environ.get("CORE_DATA_ROOT", Path.home() / "core-data")).expanduser() / "guardian"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "list", "trigger", "retry", "skip", "pause", "resume", "history", "health"))
    parser.add_argument("event", nargs="?")
    parser.add_argument("--date", default=None)
    parser.add_argument("--reason")
    parser.add_argument("--root")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    root = _root(args.root)
    day = args.date or datetime.now(TZ).date().isoformat()
    try:
        if args.action in {"trigger", "retry", "skip", "pause", "resume"}:
            if not args.reason or len(args.reason) > 500:
                raise ValueError("reason_required")
            if args.action in {"trigger", "retry", "skip"} and not args.event:
                raise ValueError("event_required")
            if args.action in {"trigger", "retry", "skip"} and not args.date:
                raise ValueError("date_required")
            schedule, schedule_config = load_schedule(root / "schedule.json", create_default=False)
            by_id = {event.event_id: event for event in schedule}
            ids = set(by_id)
            if args.event and args.event not in ids:
                raise ValueError("unknown_event")
            heartbeat = _read(root / "heartbeat.json")
            age = (datetime.now(TZ) - datetime.fromisoformat(heartbeat["last_tick_at"])).total_seconds()
            if age > 15 or heartbeat.get("health") != "READY":
                raise ValueError("guardian_unavailable")
            if args.action in {"trigger", "retry", "skip"}:
                state = _read(root / day / "state.json")
                row = state.get("events", {}).get(args.event, {"status": "PENDING"})
                status = row.get("status", "PENDING")
                now = datetime.now(TZ)
                event = by_id[args.event]
                start = datetime.combine(now.date(), datetime.strptime(event.start, "%H:%M:%S").time(), TZ)
                deadline = datetime.combine(now.date(), datetime.strptime(event.deadline, "%H:%M:%S").time(), TZ)
                if args.action == "trigger" and (now < start or now > deadline or status not in {"PENDING", "WAITING"}):
                    raise ValueError("trigger_not_available")
                if args.action == "retry" and (now < start or now > deadline or status not in {"RETRY_WAIT", "CIRCUIT_OPEN"} or row.get("worker_pid")):
                    raise ValueError("retry_not_available")
                if args.action == "retry" and (root / "control.jsonl").exists():
                    prior = [json.loads(line) for line in (root / "control.jsonl").read_text(encoding="utf-8").splitlines()
                             if line.strip()]
                    if any(item.get("type") == "REQUEST" and item.get("verb") == "retry" and
                           item.get("event_id") == args.event and item.get("business_date") == day for item in prior):
                        raise ValueError("retry_daily_grant_used")
                if args.action == "skip" and status not in {"PENDING", "WAITING", "RETRY_WAIT"}:
                    raise ValueError("skip_not_available")
            append_record(root, {"type": "REQUEST", "request_id": str(uuid.uuid4()), "actor_uid": os.getuid(),
                                 "verb": args.action, "business_date": day, "event_id": args.event,
                                 "requested_at": datetime.now(TZ).isoformat(), "reason": args.reason,
                                 "expected_revision": schedule_config["revision"],
                                 "config_hash": __import__("engine_core.guardian.management", fromlist=["schedule_hash"]).schedule_hash(schedule_config),
                                 "before": status if args.event else None, "after": None,
                                 "result": "REQUESTED"})
            print(json.dumps({"result": "REQUESTED", "event": args.event}, ensure_ascii=False))
            return 0
        if args.action == "history":
            path = root / "control.jsonl"
            rows = []
            if path.exists():
                tail = deque(maxlen=max(1, min(args.limit, 500)))
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        if line.strip():
                            tail.append(json.loads(line))
                rows = list(tail)
            if args.date:
                rows = [row for row in rows if row.get("business_date") == day]
            for row in rows:
                print(json.dumps(row, ensure_ascii=False, sort_keys=True))
            return 0
        heartbeat = _read(root / "heartbeat.json")
        state_path = root / day / "state.json"
        state = _read(state_path)
        if args.action == "status":
            age = (datetime.now(TZ) - datetime.fromisoformat(heartbeat["last_tick_at"])).total_seconds()
            if age > 15 or heartbeat.get("health") != "READY":
                print(json.dumps({"health": "DEGRADED", "heartbeat_age_seconds": round(age, 1)}))
                return 2
            print(json.dumps({"date": day, "phase": heartbeat.get("phase", "UNKNOWN"),
                              "config_revision": heartbeat.get("config_revision"), "paused": heartbeat.get("paused", False),
                              "active_event": heartbeat.get("active_event"), "last_heartbeat": heartbeat.get("last_tick_at"),
                              "health": heartbeat.get("health", "UNKNOWN")}, ensure_ascii=False, sort_keys=True))
        elif args.action in {"health", "list"}:
            age = (datetime.now(TZ) - datetime.fromisoformat(heartbeat["last_tick_at"])).total_seconds()
            if age > 15 or heartbeat.get("health") != "READY":
                print(json.dumps({"health": "DEGRADED", "heartbeat_age_seconds": round(age, 1)}))
                return 2
            if args.action == "health":
                print(json.dumps({"health": "READY", "heartbeat_age_seconds": round(age, 1)}))
            else:
                schedule, _ = load_schedule(root / "schedule.json", create_default=False)
                for event in schedule:
                    row = state.get("events", {}).get(event.event_id, {})
                    print(json.dumps({"event_id": event.event_id, "window": [event.start, event.deadline],
                                      "gate": event.gate, "status": row.get("status", "PENDING"),
                                      "attempts": row.get("attempts", 0), "retry_at": row.get("next_attempt_at"),
                                      "reason": row.get("last_error")}, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc) or type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
