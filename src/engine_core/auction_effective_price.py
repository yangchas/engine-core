"""Pure auction tick price selection helpers."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Mapping


def _value(row: Any, *names: str) -> Any:
    for name in names:
        if isinstance(row, Mapping) and name in row:
            return row[name]
        if hasattr(row, name):
            return getattr(row, name)
    return None


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def _time(value: Any) -> time | None:
    if isinstance(value, datetime):
        return value.timetz().replace(tzinfo=None)
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(value) / 1000).time()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).replace("T", " ").split()[-1]
    try:
        return time.fromisoformat(text)
    except ValueError:
        return None


def effective_price(tick_row_or_dict: Any, *, phase_cutoff_time: Any,
                    tick_ts: Any) -> tuple[int | None, str, tuple[str, ...]]:
    """Return price in milli-yuan, its source, and timing flags for an auction tick."""
    tick_time = _time(tick_ts)
    final_time = time(9, 25)
    px = _positive_int(_value(tick_row_or_dict, "px_milli", "px"))
    bp1 = _positive_int(_value(tick_row_or_dict, "bp1_milli", "bp1"))
    ap1 = _positive_int(_value(tick_row_or_dict, "ap1_milli", "ap1"))
    is_0925 = (_time(phase_cutoff_time) or time.min) >= final_time
    pre_final = is_0925 and (tick_time is None or tick_time < final_time)
    flags = ("PRE_FINAL_INDICATIVE",) if pre_final else ()

    if tick_time is not None and tick_time >= final_time and px is not None:
        return px, "TICK_PX_FINAL", ()
    if bp1 is not None and bp1 == ap1:
        return bp1, "TICK_BOOK1_INDICATIVE", flags
    return None, "NO_EFFECTIVE_PRICE", ()
