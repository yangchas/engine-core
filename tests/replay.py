"""Replay of RECORDED TD results (tests/fixtures/real/td/*.json.gz) through the cursor interface the code uses.

A recording is made by ``examples/record_td_scenario.py``: the real function runs against the real TD through a
recording wrapper, every statement with its real result (or real error) is stored with provenance. Tests then run the
same function over ``ReplayTd``. Nothing in a recording is written by hand, and a statement that was not recorded is an
error (``UnrecordedQuery``), never an empty result.
"""
from __future__ import annotations

import gzip
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "real" / "td"


class UnrecordedQuery(AssertionError):
    """The code issued a statement that no recording contains."""


class RecordedError(RuntimeError):
    """A real error TD returned for a recorded statement."""


def normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).rstrip(";").rstrip()


def encode_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"$dt": value.isoformat(sep=" ")}
    if isinstance(value, bytes):
        return {"$b": value.hex()}
    return value


def decode_value(value: Any) -> Any:
    if isinstance(value, dict) and "$dt" in value:
        return datetime.fromisoformat(value["$dt"])
    if isinstance(value, dict) and "$b" in value:
        return bytes.fromhex(value["$b"])
    return value


def load_recording(name: str) -> dict[str, Any]:
    with gzip.open(FIXTURES / f"{name}.json.gz", "rt", encoding="utf-8") as handle:
        return json.load(handle)


class ReplayCursor:
    def __init__(self, entries: dict[str, dict[str, Any]], log: list[str]):
        self._entries, self._log = entries, log
        self._rows: list[tuple] = []
        self.description: list[tuple] | None = None

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> int:
        key = normalize(sql)
        self._log.append(key)
        entry = self._entries.get(key)
        if entry is None:
            raise UnrecordedQuery(f"no recording for statement: {key[:240]}")
        if "error" in entry:
            raise RecordedError(entry["error"]["message"])
        self._rows = [tuple(decode_value(v) for v in row) for row in entry["rows"]]
        self.description = [(column,) + (None,) * 6 for column in entry["columns"]]
        return len(self._rows)

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        pass


class ReplayTd:
    """Connection-like object: ``cursor()`` -> ReplayCursor; ``statements`` lists what the code asked, in order."""

    def __init__(self, *names: str):
        self.entries: dict[str, dict[str, Any]] = {}
        for name in names:
            for entry in load_recording(name)["statements"]:
                self.entries[normalize(entry["sql"])] = entry
        self.statements: list[str] = []

    def cursor(self) -> ReplayCursor:
        return ReplayCursor(self.entries, self.statements)

    def close(self) -> None:
        pass
