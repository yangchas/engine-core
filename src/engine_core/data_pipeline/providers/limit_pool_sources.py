"""I/O side of the ``limit_up_pool`` tiers (C580): WenCai query and read-only TD bars.  Clients are injectable.

Kaipan lives in ``kaipan_provider.KaipanProvider.limit_up_pool`` (rate limit, circuit breaker).  Nothing here
writes anything; the only external effects are the requests themselves (WenCai: one page of up to 100 rows per
call, shared throttle rules of C564/C569; TD: SELECT on ``market_data1.daily_kline`` only).
"""
from __future__ import annotations

import os
import re
import time
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..limit_pool import PoolSourceError

WENCAI_QUESTION = "{date}涨停;连板天数;去除st"
WENCAI_PAGE_SIZE = 100
TD_SOURCE_DATABASE = "market_data1"     # READ ONLY. Never written, never created.
TD_SOURCE_TABLE = "daily_kline"


def wencai_question(trade_date: str) -> str:
    return WENCAI_QUESTION.format(date=_date.fromisoformat(trade_date).isoformat())


def fetch_wencai_pool(trade_date: str, *, cookie_file: str | os.PathLike[str] | None,
                      query_fn: Callable[..., Any] | None = None, max_requests: int = 3,
                      cooldown_seconds: float = 60.0, cooldown_max_seconds: float = 300.0, max_cooldowns: int = 2,
                      sleep_fn: Callable[[float], None] = time.sleep,
                      clock_fn: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Page "{date}涨停;连板天数;去除st" with the shared WenCai run state (cookie, same-page cooldown resume).

    Returns {"records", "reported_total", "request_count", "cookie_status", "question"}.  Raises ``PoolSourceError``:
    TIER_PARTIAL (retryable) when pages are truncated / the budget ran out, TIER_NO_DATA when empty.
    """
    from .wencai_daily_adapter import _FORBIDDEN, _fetch_query, _read_cookie, _Run

    if query_fn is None:
        from pywencai.wencai import get_robot_data
        query_fn = get_robot_data
    question = wencai_question(trade_date)
    run = _Run(query_fn=query_fn, context=None, page_size=WENCAI_PAGE_SIZE, cookie=_read_cookie(cookie_file),
               cooldown_seconds=float(cooldown_seconds), cooldown_factor=2.0,
               cooldown_max_seconds=float(cooldown_max_seconds), max_cooldowns=int(max_cooldowns),
               max_requests=int(max_requests), sleep_fn=sleep_fn, clock_fn=clock_fn, wall_budget_seconds=None,
               checkpoint_path=None, trade_date=trade_date, question=question)
    try:
        query = _fetch_query(run, question, unit_prefix="pool", max_pages=10, first_unit=True)
    except Exception as exc:  # noqa: BLE001 - classified: first page failed outright
        text = str(exc)
        if run.cookie:
            text = text.replace(run.cookie, "<cookie>")
        blocked = bool(_FORBIDDEN.search(text))
        raise PoolSourceError("WENCAI_BLOCKED" if blocked else "UPSTREAM_ERROR",
                              f"{type(exc).__name__}:{text[:160]}", exit_code=30, retryable=True) from None
    records = query.rows
    if not records and not (query.stop_reason or query.truncated or query.error):
        raise PoolSourceError("TIER_NO_DATA", "empty_response", exit_code=40)
    if not query.complete:
        reason = query.stop_reason or ("pages_truncated" if query.truncated else type(query.error).__name__)
        raise PoolSourceError("TIER_PARTIAL", str(reason), exit_code=30, retryable=True)
    return {"records": records, "reported_total": query.total, "request_count": run.requests,
            "cookie_status": run.cookie_status, "question": question}


# ----------------------------------------------------------------------------------------------------------------
# TD read-only bars
# ----------------------------------------------------------------------------------------------------------------
_DATE_OK = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def td_connect() -> Any:
    from ..td_sink import TdUnavailable
    if not os.environ.get("TD_CONFIG"):
        raise TdUnavailable("TD_CONFIG_required")
    import taos
    return taos.connect(config=os.environ["TD_CONFIG"])


def read_td_bars(trade_date: str, *, lookback_calendar_days: int = 40,
                 connect: Callable[[], Any] = td_connect) -> dict[str, list[tuple]]:
    """symbol -> [(date, close, high, pct_chg)] for ``trade_date`` and the calendar window before it.

    One SELECT on ``market_data1.daily_kline`` (single-precision FLOAT columns; only close/high/pct_chg are used,
    volume/turnover are 0 in that legacy table).  Read-only.
    """
    from datetime import timedelta
    from ..td_sink import TdUnavailable
    if not _DATE_OK.match(trade_date):
        raise ValueError("bad_trade_date")
    day = _date.fromisoformat(trade_date)
    start = (day - timedelta(days=int(lookback_calendar_days))).isoformat()
    end = (day + timedelta(days=1)).isoformat()
    sql = (f"SELECT ts, symbol, close, high, pct_chg FROM {TD_SOURCE_DATABASE}.{TD_SOURCE_TABLE} "
           f"WHERE ts >= '{start} 00:00:00' AND ts < '{end} 00:00:00'")
    try:
        conn = connect()
    except TdUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TdUnavailable("td_connect_failed:" + type(exc).__name__ + ":" + str(exc)[:160]) from exc
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        fetched = cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        raise TdUnavailable("td_statement_failed:" + type(exc).__name__ + ":" + str(exc)[:160]) from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    bars: dict[str, list[tuple]] = {}
    for ts, symbol, close, high, pct in fetched:
        symbol = str(symbol).strip().zfill(6)
        bars.setdefault(symbol, []).append((ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10],
                                            close, high, pct))
    return bars


def universe_names(root: str | Path, trade_date: str) -> tuple[dict[str, str] | None, str | None]:
    """symbol -> name from the newest ``symbol_universe`` partition on or before ``trade_date`` (ST detection by name).

    Returns (None, None) when no universe partition exists; the caller then marks st_filter UNAVAILABLE.
    """
    from ..storage import ArtifactCatalog
    catalog = ArtifactCatalog(root)
    dates = (catalog.read().get("datasets", {}).get("symbol_universe", {}).get("dates") or {})
    usable = sorted(day for day in dates if day <= trade_date and dates[day])
    for day in reversed(usable):
        path = catalog.locate("symbol_universe", day)
        if path is None:
            continue
        try:
            rows = catalog.read_rows(path)
        except (OSError, ValueError, KeyError):
            continue
        names = {str(r.get("symbol", "")).zfill(6): str(r.get("name") or "") for r in rows if r.get("symbol")}
        if names:
            return names, f"symbol_universe@{day}"
    return None, None
