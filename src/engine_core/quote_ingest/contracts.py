"""Canonical records for the controlled legacy shared-output transition."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class QuoteCursor:
    """A polling round marker, not a lossless upstream offset."""
    round_id: int = 0
    event_time_ms: int | None = None


@dataclass(frozen=True)
class QuoteState:
    symbol: str
    ts: datetime | None
    read_at: datetime
    available_at: datetime | None
    source: str
    price_milli: int | None = None
    pre_close_milli: int | None = None
    high_milli: int | None = None
    low_milli: int | None = None
    open_milli: int | None = None
    amount_yuan: int | None = None
    volume_source_units: int | None = None
    a20_milli: int | None = None
    a24_milli: int | None = None
    a25_milli: int | None = None
    limit_state: int | None = None
    quality: tuple[str, ...] = field(default_factory=tuple)
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class QuoteRead:
    rows: tuple[QuoteState, ...]
    cursor: QuoteCursor
    read_start: datetime
    read_end: datetime
    expected_symbols: int | None
    missing_symbols: int
    partial: bool
    source: str
    incremental_completeness: str = "UNKNOWN"

