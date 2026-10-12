"""Recover the 0925 anchor from a late final-match snapshot (ph==0).

The producer sets ``a25`` only from snapshots stamped 09:25:00-09:25:20, yet
on 2026-10-09 336 Shenzhen symbols delivered their final-match snapshot
(``ph==0``, ``vol>0``, ``px`` = opening price) at 09:26:00-09:26:57.  This
pure, opt-in helper adds ``a25`` to such an update when the symbol has no
positive ``a25`` yet.  Every synthesized value is recorded with its snapshot
time; nothing is invented when the snapshot is absent.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Tuple

from .clock import local_datetime_ms


def _positive(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


class PhaseZeroFinalRecovery:
    def __init__(self, trade_date: str) -> None:
        self._lower_ms = local_datetime_ms(trade_date, "09:25:00")
        self._upper_ms = local_datetime_ms(trade_date, "09:30:00")
        self._has_anchor: set = set()
        self.recovered: dict = {}

    def apply(self, updates: Iterable[Mapping[str, Any]]) -> Tuple[Mapping[str, Any], ...]:
        out = []
        for update in updates:
            symbol = str(update.get("symbol", "")).strip()
            if _positive(update.get("a25")):
                self._has_anchor.add(symbol)
            elif (
                symbol
                and symbol not in self._has_anchor
                and symbol not in self.recovered
                and str(update.get("ph")).strip() == "0"
                and _positive(update.get("vol"))
                and _positive(update.get("px"))
                and self._lower_ms <= _positive(update.get("ts")) < self._upper_ms
            ):
                price = _positive(update.get("px"))
                self.recovered[symbol] = {
                    "price_milli": price,
                    "source_time_ms": _positive(update.get("ts")),
                }
                update = {**dict(update), "a25": price}
            out.append(update)
        return tuple(out)


__all__ = ["PhaseZeroFinalRecovery"]
