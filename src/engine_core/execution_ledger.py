"""Local append-only ledger of acknowledged timer firings (restart idempotency).

The minimum durable record the plan asks for: which Core-owned timers have
already been accepted for a trade date, so a restarted node does not run them
again.  It is a library over a caller-chosen file; it never touches production
stores, and writing it is not an effect on Redis/TD/Rabbit.

Format: one JSON object per line.  Appends take an exclusive ``flock`` and are
``fsync``ed.  A line that cannot be parsed (crash mid-write) is skipped and
counted by ``unreadable_line_count``, never repaired; a skipped record means
its timer may run once more, which is the safe direction for deterministic
facts.  An identical record is a no-op; a record for the same
``(trade_date, timer_id)`` with a different firing hash raises.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Iterable, List, Tuple

from .timers import TimerFiring

LEDGER_CONTRACT_VERSION = "TimerExecutionLedgerV1"
_FIELDS = (
    "timer_id", "scheduled_time_ms", "fired_time_ms", "trigger_basis",
    "origin", "late_by_ms", "session_plan_hash",
)


class ExecutionLedger:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def _read(self) -> Tuple[List[dict], int]:
        if not self._path.exists():
            return [], 0
        records: List[dict] = []
        unreadable = 0
        for raw in self._path.read_bytes().split(b"\n"):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
                if not isinstance(record, dict) or record.get("contract") != LEDGER_CONTRACT_VERSION:
                    raise ValueError
            except ValueError:
                unreadable += 1
                continue
            records.append(record)
        return records, unreadable

    def unreadable_line_count(self) -> int:
        return self._read()[1]

    def firings(self, trade_date: str) -> Tuple[TimerFiring, ...]:
        """Acknowledged firings for ``trade_date``, rebuilt with their hashes."""

        out = []
        for record in self._read()[0]:
            if record["trade_date"] != trade_date:
                continue
            firing = TimerFiring(**{name: record[name] for name in _FIELDS})
            if firing.content_hash != record["content_hash"]:
                raise ValueError("ledger record hash mismatch for %s" % record["timer_id"])
            out.append(firing)
        return tuple(out)

    def completed_timer_ids(self, trade_date: str) -> Tuple[str, ...]:
        return tuple(sorted(f.timer_id for f in self.firings(trade_date)))

    def record(self, trade_date: str, firing: TimerFiring) -> bool:
        """Append an acknowledged firing; ``False`` when it was already recorded."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for existing in self.firings(trade_date):
                if existing.timer_id == firing.timer_id:
                    if existing.content_hash == firing.content_hash:
                        return False
                    raise ValueError("conflicting firing already recorded for %s" % firing.timer_id)
            line = json.dumps(
                {
                    "contract": LEDGER_CONTRACT_VERSION,
                    "trade_date": trade_date,
                    **{name: getattr(firing, name) for name in _FIELDS},
                    "content_hash": firing.content_hash,
                },
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            # a torn tail from an earlier crash must not glue onto this record
            if self._path.stat().st_size and not self._path.read_bytes().endswith(b"\n"):
                os.write(fd, b"\n")
            os.write(fd, line)
            os.fsync(fd)
            return True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


__all__ = ["ExecutionLedger", "LEDGER_CONTRACT_VERSION"]
