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


def normalize_auction_change_bp_to_pct(value: Any) -> Optional[float]:
    """Convert the typed TD ``chg_bp`` field to percentage-point units.

    ``auction_snapshot_v2.chg_bp`` is a basis-point field: ``100`` means one
    percentage point.  This conversion is intentionally separate from
    :func:`normalize_auction_change_ratio`, whose input accepts ambiguous
    legacy ratio/percent/basis-point scales.  Opening transition facts require
    percentage-point units and therefore must use this typed adapter boundary.
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(raw):
        return None
    return raw / 100.0
