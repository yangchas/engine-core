"""Read degraded instances and their final attempt replies from a native ledger."""
from __future__ import annotations

import argparse
from datetime import date as date_type
import json
import os
from pathlib import Path
from typing import Any


def degraded_report(ledger_path: Path) -> list[dict[str, Any]]:
    """Return one row per latest degraded instance; absent old replies remain unknown."""
    states: dict[str, dict[str, Any]] = {}
    replies: dict[tuple[str, int], dict[str, Any]] = {}
    with ledger_path.open(encoding="utf-8") as ledger:
        for line in ledger:
            record = json.loads(line)
            data = record.get("data") or {}
            instance = data.get("instance")
            if not instance:
                continue
            if record.get("type") == "ATTEMPT_END":
                replies[(instance, data.get("attempt"))] = data.get("reply") or {}
            elif record.get("type") == "INSTANCE_STATE":
                states[instance] = data
    rows = []
    for instance, data in states.items():
        state = data.get("state") or {}
        if state.get("status") != "SUCCEEDED_DEGRADED":
            continue
        attempt = state.get("attempts")
        reply = replies.get((instance, attempt), {})
        reason = reply.get("reason")
        rows.append({"event": data.get("event_id"), "instance": instance, "attempt": attempt,
                     "reason": reason, "reason_missing": not bool(reason),
                     "error_code": reply.get("error_code"), "detail": reply.get("detail")})
    return sorted(rows, key=lambda row: (row["event"] or "", row["instance"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date_type.fromisoformat)
    parser.add_argument("--state-root", type=Path, default=Path(os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser() / "guardian/native-state")
    args = parser.parse_args()
    print(json.dumps(degraded_report(args.state_root / "ledger" / f"{args.date}.jsonl"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
