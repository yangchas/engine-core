"""Trade-day context: the one place that answers "which day is today / yesterday / tomorrow / the previous trade day".

Everything that needs a neighbouring trade date asks this module (backed by the Core trading calendar) instead of
deriving it from catalog partitions, weekdays or ``date.today() - 1``. The context for the market day is also
persisted (``<root>/trade_calendar/day_context.json``) by the daily calendar refresh and by every guardian start, so
other processes and operators can read it without importing Python; readers never trust the file blindly — they
recompute when its day or calendar version no longer matches.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .data_pipeline.trade_calendar import TradeCalendarStore

SCHEMA = "CoreTradeDayContextV1"
TZ = ZoneInfo("Asia/Shanghai")
FILENAME = "day_context.json"


def market_today(now: datetime | None = None) -> date:
    return (now.astimezone(TZ) if now is not None and now.tzinfo else now or datetime.now(TZ)).date()


def _root(root: str | Path | None) -> Path:
    return Path(root or os.getenv("CORE_DATA_ROOT", "~/core-data")).expanduser()


def _trade_neighbours(store: TradeCalendarStore, day: str, count: int) -> tuple[list[str | None], list[str | None]]:
    """``count`` trade days before/after ``day``; the first of each is required, deeper ones are None when uncovered."""
    previous: list[str | None] = []
    following: list[str | None] = []
    for walk, out in ((store.previous_trade_date, previous), (store.next_trade_date, following)):
        cursor: str | None = day
        for index in range(count):
            try:
                cursor = walk(cursor).isoformat() if cursor else None
            except ValueError:
                if index == 0:
                    raise
                cursor = None
            out.append(cursor)
    return previous, following


def _flag(store: TradeCalendarStore, day: str) -> bool | None:
    try:
        return store.is_trading_day(day)
    except ValueError:
        return None


def build_context(root: str | Path | None = None, day: str | date | None = None) -> dict[str, Any]:
    """Compute the context for ``day`` (default: the market day) from the calendar. Raises if the calendar does not cover it."""
    base = _root(root)
    store = TradeCalendarStore(base)
    parsed = date.fromisoformat(str(day)) if day else market_today()
    today = parsed.isoformat()
    yesterday = (parsed - timedelta(days=1)).isoformat()
    tomorrow = (parsed + timedelta(days=1)).isoformat()
    matrix = store.load() or {}
    previous, following = _trade_neighbours(store, today, 2)
    return {
        "schema": SCHEMA,
        "as_of_day": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calendar_version": matrix.get("last_ok_at"),
        "lit_from": matrix.get("lit_from"),
        "lit_to": matrix.get("lit_to"),
        "today": today,
        "today_is_trading_day": store.is_trading_day(today),
        # natural calendar neighbours with their trading flag (a weekend "yesterday" is real and flagged, not skipped)
        "yesterday": {"date": yesterday, "is_trading_day": _flag(store, yesterday)},
        "tomorrow": {"date": tomorrow, "is_trading_day": _flag(store, tomorrow)},
        # trade-day neighbours: what reports, data partitions and PIT lookbacks mean by "previous" / "next"
        "prev_trade_date": previous[0],
        "prev2_trade_date": previous[1],
        "next_trade_date": following[0],
        "next2_trade_date": following[1],
    }


def _path(root: Path) -> Path:
    return root / "trade_calendar" / FILENAME


def refresh_context(root: str | Path | None = None, day: str | date | None = None) -> dict[str, Any]:
    """Recompute and atomically persist the context. Called by the daily calendar refresh and at every guardian start."""
    base = _root(root)
    context = build_context(base, day)
    target = _path(base)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".day_context-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(context, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return context


def load_context(root: str | Path | None = None, day: str | date | None = None) -> dict[str, Any]:
    """The context for ``day``: the persisted one when it is for that day and the current calendar version, else recomputed."""
    base = _root(root)
    wanted = date.fromisoformat(str(day)).isoformat() if day else market_today().isoformat()
    try:
        stored = json.loads(_path(base).read_text(encoding="utf-8"))
        matrix = TradeCalendarStore(base).load() or {}
        if (stored.get("schema") == SCHEMA and stored.get("as_of_day") == wanted
                and stored.get("calendar_version") == matrix.get("last_ok_at")):
            return stored
    except (OSError, ValueError):
        pass
    return build_context(base, wanted)


def previous_trade_date(root: str | Path | None, day: str | date) -> str:
    """Trade day before ``day`` — the single sanctioned way to ask. Raises CalendarCoverageError when unknown."""
    return load_context(root, day)["prev_trade_date"]


def next_trade_date(root: str | Path | None, day: str | date) -> str:
    return load_context(root, day)["next_trade_date"]


def check_context(root: str | Path | None = None, day: str | date | None = None) -> dict[str, Any]:
    """Compare the persisted file with a fresh computation (used by health checks)."""
    base = _root(root)
    fresh = build_context(base, day)
    try:
        stored = json.loads(_path(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "MISSING", "fresh": fresh}
    keys = ("as_of_day", "calendar_version", "prev_trade_date", "prev2_trade_date", "next_trade_date", "next2_trade_date",
            "today_is_trading_day")
    differing = [key for key in keys if stored.get(key) != fresh.get(key)]
    return {"status": "OK" if not differing else "STALE", "differing": differing, "fresh": fresh}
