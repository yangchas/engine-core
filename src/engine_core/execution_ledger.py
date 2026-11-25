"""Local append-only ledger of acknowledged timer firings (restart idempotency).

The minimum durable record the plan asks for: which Core-owned timers have
already been accepted for a trade date, so a restarted node does not run them
again.  It is a library over a caller-chosen file; it never touches production
stores, and writing it is not an effect on Redis/TD/Rabbit.

Format: one JSON object per line.  Appends take an exclusive ``flock`` and are
``fsync``ed.  An incomplete, unterminated tail is skipped and counted by
``unreadable_line_count`` because ``record`` has not returned to acknowledge
it; the next append truncates that tail.  Malformed complete lines and
parseable records missing required fields raise, since silently dropping an
acknowledged firing could cause duplicate dispatch.  An identical record is a
no-op; a record for the same
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
        lines = self._path.read_bytes().splitlines(keepends=True)
        for line_number, raw_line in enumerate(lines, 1):
            terminated = raw_line.endswith(b"\n")
            raw = raw_line[:-1] if terminated else raw_line
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                if line_number == len(lines) and not terminated:
                    unreadable += 1
                    continue
                raise ValueError("invalid ledger JSON at line %d" % line_number)
            if line_number == len(lines) and not terminated:
                unreadable += 1
                continue
            if not isinstance(record, dict):
                raise ValueError("ledger record at line %d must be an object" % line_number)
            required = {"contract", "trade_date", *_FIELDS, "content_hash"}
            missing = sorted(required.difference(record))
            if missing:
                raise ValueError(
                    "ledger record at line %d missing fields: %s"
                    % (line_number, ", ".join(missing))
                )
            if record.get("contract") != LEDGER_CONTRACT_VERSION:
                raise ValueError("unsupported ledger contract at line %d" % line_number)
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
            records, _ = self._read()
            content = self._path.read_bytes()
            if content and not content.endswith(b"\n"):
                os.ftruncate(fd, content.rfind(b"\n") + 1)
            existing_firings = []
            for record in records:
                if record["trade_date"] != trade_date:
                    continue
                existing = TimerFiring(**{name: record[name] for name in _FIELDS})
                if existing.content_hash != record["content_hash"]:
                    raise ValueError("ledger record hash mismatch for %s" % record["timer_id"])
                existing_firings.append(existing)
            for existing in existing_firings:
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
            os.write(fd, line)
            os.fsync(fd)
            return True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


__all__ = ["ExecutionLedger", "LEDGER_CONTRACT_VERSION"]
