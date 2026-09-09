"""Small auction-field transformations extracted from the legacy read path."""

from __future__ import annotations

import math
from typing import Any, Optional


def normalize_auction_change_ratio(value: Any) -> Optional[float]:
    """Normalize a valid auction change value to ratio units.

    The deployed legacy reader accepts ratio values (``0.0997``), percentage
    points (``9.97``), and basis points (``997``) using the same thresholds.
    This wheel preserves that formula for valid finite numbers while keeping
    missing or malformed input as ``None``; the core contract never converts
    an unknown value into a factual zero.
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(raw):
        return None
    magnitude = abs(raw)
    if magnitude <= 0.35:
        return raw
    if magnitude <= 30.0:
        return raw / 100.0
    return raw / 10000.0
