"""Portable research contracts; values are percentages unless otherwise noted."""
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class Timepoint:
    trade_date: str
    time: str
    factors: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class FactorOutput:
    name: str
    values: dict[str, Any]

@dataclass(frozen=True)
class DayTimeline:
    trade_date: str
    timepoints: tuple[Timepoint, ...]

@dataclass(frozen=True)
class DatasetMeta:
    schema: str = "SentimentReviewV1"
    daily_days: int = 0
    intraday_days: int = 0
