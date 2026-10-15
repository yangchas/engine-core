"""Exact exchange price-band helpers using cent precision and half-up rounding."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


CENT = Decimal("0.01")


def limit_percent(symbol: str) -> int:
    code = str(symbol).strip().lower().removeprefix("sh.").removeprefix("sz.").removeprefix("bj.")
    if code.startswith(("688", "689", "30")):
        return 20
    if code.startswith(("8", "4", "92")):
        return 30
    return 10


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _cents(value: Any) -> Decimal | None:
    number = _decimal(value)
    if number is None:
        return None
    return number.quantize(CENT, rounding=ROUND_HALF_UP)


def limit_prices(pre_close: Any, symbol: str, *, is_st: bool = False,
                 unlimited: bool = False) -> tuple[float, float] | None:
    """Return (limit_up, limit_down); unlimited/new-listing rows have no band."""
    del is_st  # ST stocks follow the same bands for this project's supported market.
    base = _decimal(pre_close)
    if unlimited or base is None or base <= 0:
        return None
    fraction = Decimal(limit_percent(symbol)) / Decimal(100)
    up = (base * (Decimal(1) + fraction)).quantize(CENT, rounding=ROUND_HALF_UP)
    down = (base * (Decimal(1) - fraction)).quantize(CENT, rounding=ROUND_HALF_UP)
    return float(up), float(down)


def is_limit_up(close: Any, pre_close: Any, symbol: str, *, is_st: bool = False,
                unlimited: bool = False) -> bool:
    prices = limit_prices(pre_close, symbol, is_st=is_st, unlimited=unlimited)
    value = _cents(close)
    return prices is not None and value is not None and value == _cents(prices[0])


def is_limit_down(close: Any, pre_close: Any, symbol: str, *, is_st: bool = False,
                  unlimited: bool = False) -> bool:
    prices = limit_prices(pre_close, symbol, is_st=is_st, unlimited=unlimited)
    value = _cents(close)
    return prices is not None and value is not None and value == _cents(prices[1])


def touched_limit(high: Any, pre_close: Any, symbol: str, *, is_st: bool = False,
                  unlimited: bool = False) -> bool:
    prices = limit_prices(pre_close, symbol, is_st=is_st, unlimited=unlimited)
    value = _cents(high)
    return prices is not None and value is not None and value == _cents(prices[0])


def touched_limit_down(low: Any, pre_close: Any, symbol: str, *, is_st: bool = False,
                       unlimited: bool = False) -> bool:
    prices = limit_prices(pre_close, symbol, is_st=is_st, unlimited=unlimited)
    value = _cents(low)
    return prices is not None and value is not None and value == _cents(prices[1])


def state_at_close(close: Any, high: Any, low: Any, pre_close: Any, symbol: str, *,
                    is_st: bool = False, unlimited: bool = False) -> str:
    """Describe exact close/touch status; price-based seal is explicitly a proxy."""
    values = tuple(_cents(value) for value in (close, high, low))
    if unlimited:
        return "UNLIMITED"
    if any(value is None for value in values) or limit_prices(pre_close, symbol, is_st=is_st) is None:
        return "缺价格字段"
    if any(value <= 0 for value in values):
        return "无"
    if is_limit_up(close, pre_close, symbol, is_st=is_st):
        return "封(价格代理)"
    if touched_limit(high, pre_close, symbol, is_st=is_st):
        return "触"
    if is_limit_down(close, pre_close, symbol, is_st=is_st):
        return "跌停价"
    if touched_limit_down(low, pre_close, symbol, is_st=is_st):
        return "跌触"
    return "无"
