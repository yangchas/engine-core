"""Read a Baostock adjustment factor without inventing missing history."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation


def factor_for(events, day):
    """Return the most recent event's factor and its quality for a date.

    An empty response does not establish that the security never had an event.
    Invalid or missing event fields remain missing rather than becoming one.
    """
    try:
        target = date.fromisoformat(str(day))
        if not events:
            return None, 'NO_EVENTS'
        candidates = [(date.fromisoformat(str(row['dividOperateDate'])), row)
                      for row in events]
        eligible = [item for item in candidates if item[0] <= target]
        if not eligible:
            return None, 'BEFORE_FIRST_EVENT'
        _, row = max(eligible, key=lambda item: item[0])
        value = Decimal(str(row['foreAdjustFactor']))
        if not value.is_finite() or value <= 0:
            return None, 'INVALID_FACTOR'
        result = float(value)
        if result == float('inf') or result == 0:
            return None, 'INVALID_FACTOR'
        return result, 'FORMAL'
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        return None, 'INVALID_FACTOR'
