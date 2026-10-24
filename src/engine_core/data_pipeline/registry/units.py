"""Closed vocabulary for source-unit and source-encoding declarations."""
from __future__ import annotations

from dataclasses import dataclass


UNITS = frozenset({
    "yuan", "milli_yuan", "percent", "bp", "shares", "hands", "epoch_ms",
    "yyyymmdd", "iso_date", "local_datetime_cst", "none", "category", "count",
})
ENCODINGS = frozenset({"float", "int", "numeric_string", "str", "bool"})
SPECIAL_CODES = frozenset({"UNKNOWN", "ABSENT"})


@dataclass(frozen=True)
class SourceUnit:
    unit: str | None = None
    encoding: str | None = None
    special: str | None = None


def parse_source_code(source_code: str) -> SourceUnit:
    """Parse one closed source code, or return its explicit special value."""
    if not isinstance(source_code, str):
        raise ValueError(f"source unit code must be text: {source_code!r}")
    if source_code in SPECIAL_CODES:
        return SourceUnit(special=source_code)
    parts = source_code.split(":")
    if len(parts) != 2 or parts[0] not in UNITS or parts[1] not in ENCODINGS:
        raise ValueError(f"invalid source unit code: {source_code!r}")
    return SourceUnit(unit=parts[0], encoding=parts[1])
