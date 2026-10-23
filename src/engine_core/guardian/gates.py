"""Read-only, fail-closed readiness gates for the wall-clock guardian."""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)
_CACHE: dict[tuple[str, str, str], tuple[float, "GateResult"]] = {}
_LOCK = threading.Lock()

# Guardian startup scheduling thresholds are engineering thresholds, not market
# laws and not a verdict on report validity.
SNAPSHOT_COVERAGE_THRESHOLD = 0.90
AUCTION_COVERAGE_THRESHOLD = 0.85
MIN_CHECKPOINT_SYMBOLS = 4000
FALLBACK_UNIVERSE_COUNT = 4500
TD_QUERY_TIMEOUT_SECONDS = 15.0


class GateResult(dict):
    """JSON-ready diagnostics whose truth value remains the readiness result."""

    def __bool__(self) -> bool:
        return self.get("status") == "ready"


def _td():
    if not os.environ.get("TD_CONFIG"):
        raise RuntimeError("td_unavailable")
    import taos
    return taos.connect(database="market_data1", config=os.environ["TD_CONFIG"])


def _query(sql: str):
    connection = _td()
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        connection.close()


def _query_bounded(sql: str):
    try:
        timeout = max(0.01, float(os.environ.get("GUARDIAN_TD_QUERY_TIMEOUT_SECONDS",
                                                  TD_QUERY_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        timeout = TD_QUERY_TIMEOUT_SECONDS
    result: queue.Queue = queue.Queue(maxsize=1)

    def run():
        try:
            result.put((True, _query(sql)))
        except BaseException as exc:
            result.put((False, exc))

    threading.Thread(target=run, name="guardian-td-gate-query", daemon=True).start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError("TD_QUERY_TIMEOUT") from None
    if not ok:
        raise value
    return value, timeout


def _day(now: datetime) -> str:
    return now.date().strftime("%Y%m%d")


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def _cached(name: str, event: Any, now: datetime, identity: str, fn) -> GateResult:
    key = (now.date().isoformat(), now.replace(microsecond=0).isoformat(),
           f"{name}:{getattr(event, 'event_id', '')}:{identity}")
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 1.0:
            return cached[1]
    try:
        value = fn()
    except Exception as exc:
        LOG.warning("guardian gate failed gate=%s reason=%s", name, type(exc).__name__)
        value = GateResult(status="not_ready", reason=("TD_QUERY_TIMEOUT" if isinstance(exc, TimeoutError)
                                                       else "TD_QUERY_FAILED"),
                           exception_type=type(exc).__name__, elapsed_ms=0)
    with _LOCK:
        _CACHE[key] = (time.monotonic(), value)
        if len(_CACHE) > 256:
            _CACHE.clear()
    return value


def _tag(event: Any) -> str:
    return "0920" if "0920" in event.gate else "0924" if "0924" in event.gate else "0925"


def _checkpoint_cutoff(event: Any, trade_date) -> datetime:
    start = datetime.combine(trade_date, datetime.strptime(event.start, "%H:%M:%S").time())
    if event.handler == "open_report":
        anchor = start - timedelta(seconds=10)
        return anchor.replace(second=(anchor.second // 3) * 3, microsecond=0)
    return start.replace(second=0, microsecond=0)


def auction_snapshot_stats(now: datetime, tag: str, ctx: Any = None) -> dict[str, Any]:
    td = ctx.get("td") if isinstance(ctx, dict) else None
    if td is not None:
        count = int(td.snapshot_count(_day(now), tag))
    else:
        upper = _timestamp(now.replace(tzinfo=None))
        rows, _timeout = _query_bounded(f"SELECT symbol FROM market_data1.auction_snapshot_v2 WHERE trade_date='{_day(now)}' AND auction_tag='{tag}' AND ts <= '{upper}'")
        count = len({str(row[0]) for row in rows})
    universe = ctx.get("universe_count") if isinstance(ctx, dict) else None
    if not universe:
        universe = FALLBACK_UNIVERSE_COUNT
        LOG.info("guardian trusted universe unavailable; using fallback=%s trade_date=%s", FALLBACK_UNIVERSE_COUNT, now.date())
    return {"row_count": count, "universe_count": int(universe),
            "coverage": count / int(universe) if universe else 0.0}


def auction_snapshot_ready(event: Any, now: datetime, ctx: Any) -> GateResult:
    gate = event.gate
    tag = _tag(event)
    def check():
        threshold = AUCTION_COVERAGE_THRESHOLD if gate == "auction_coverage" else SNAPSHOT_COVERAGE_THRESHOLD
        started = time.monotonic()
        upper = _timestamp(now.replace(tzinfo=None))
        try:
            stats = auction_snapshot_stats(now, tag, ctx)
            return GateResult(status="ready" if stats["coverage"] >= threshold else "not_ready",
                              reason="coverage_threshold_met" if stats["coverage"] >= threshold else "coverage_below_threshold",
                              query_window={"trade_date": _day(now), "auction_tag": tag, "upper_ts": upper},
                              row_count=stats["row_count"], coverage=stats["coverage"],
                              threshold=threshold, elapsed_ms=round((time.monotonic() - started) * 1000))
        except Exception as exc:
            return GateResult(status="not_ready", reason="TD_QUERY_TIMEOUT" if isinstance(exc, TimeoutError) else "TD_QUERY_FAILED",
                              query_window={"trade_date": _day(now), "auction_tag": tag, "upper_ts": upper},
                              threshold=threshold, exception_type=type(exc).__name__,
                              elapsed_ms=round((time.monotonic() - started) * 1000))
    threshold = AUCTION_COVERAGE_THRESHOLD if gate == "auction_coverage" else SNAPSHOT_COVERAGE_THRESHOLD
    return _cached(gate, event, now, f"{tag}:{threshold}", check)


def checkpoint_ready(event: Any, now: datetime, ctx: Any) -> GateResult:
    def check():
        started = time.monotonic()
        cutoff = _checkpoint_cutoff(event, now.date())
        stamp = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        end = (cutoff + timedelta(seconds=3)).strftime("%Y-%m-%d %H:%M:%S")
        detail = {"query_window": [stamp, end], "threshold": MIN_CHECKPOINT_SYMBOLS}
        if isinstance(ctx, dict) and ctx.get("td_unavailable"):
            return GateResult(status="not_ready", reason="TD_UNAVAILABLE", row_count=0,
                              elapsed_ms=round((time.monotonic() - started) * 1000), **detail)
        if cutoff > now.replace(tzinfo=None):
            return GateResult(status="not_ready", reason="WINDOW_NOT_REACHED", row_count=0,
                              elapsed_ms=round((time.monotonic() - started) * 1000), **detail)
        if isinstance(ctx, dict) and "checkpoint_count" in ctx:
            tick_ts = str(ctx.get("checkpoint_ts", ""))
            if len(tick_ts) == 8:
                tick_ts = now.date().isoformat() + " " + tick_ts
            count = int(ctx["checkpoint_count"])
            ready = count >= MIN_CHECKPOINT_SYMBOLS and tick_ts[:19] >= stamp and tick_ts[:26] <= _timestamp(now.replace(tzinfo=None))
            return GateResult(status="ready" if ready else "not_ready",
                              reason="checkpoint_threshold_met" if ready else "checkpoint_below_threshold",
                              row_count=count, checkpoint_ts=tick_ts, elapsed_ms=round((time.monotonic() - started) * 1000),
                              **detail)
        if cutoff + timedelta(seconds=3) > now.replace(tzinfo=None):
            end = _timestamp(now.replace(tzinfo=None))
            if end <= stamp:
                return GateResult(status="not_ready", reason="WINDOW_NOT_REACHED", row_count=0,
                                  elapsed_ms=round((time.monotonic() - started) * 1000), **detail)
        detail["query_window"] = [stamp, end]
        try:
            symbols, timeout = _query_bounded(f"SELECT symbol, ts FROM market_data1.stock_tick_v2 WHERE ts >= '{stamp}' AND ts < '{end}' AND ts <= '{_timestamp(now.replace(tzinfo=None))}'")
        except Exception as exc:
            return GateResult(status="not_ready", reason="TD_QUERY_TIMEOUT" if isinstance(exc, TimeoutError) else "TD_QUERY_FAILED",
                              exception_type=type(exc).__name__, elapsed_ms=round((time.monotonic() - started) * 1000),
                              timeout_seconds=float(os.environ.get("GUARDIAN_TD_QUERY_TIMEOUT_SECONDS", TD_QUERY_TIMEOUT_SECONDS)),
                              **detail)
        # Any row in the cutoff window proves the day's latest timestamp is at
        # least the cutoff; avoid a second full-day scan.
        count = len({str(row[0]) for row in symbols})
        checkpoint_ts = max((str(row[1]) for row in symbols if len(row) > 1), default="")
        return GateResult(status="ready" if count >= MIN_CHECKPOINT_SYMBOLS else "not_ready",
                          reason="checkpoint_threshold_met" if count >= MIN_CHECKPOINT_SYMBOLS else "checkpoint_below_threshold",
                          row_count=count, checkpoint_ts=checkpoint_ts, timeout_seconds=timeout,
                          elapsed_ms=round((time.monotonic() - started) * 1000), **detail)
    return _cached("checkpoint_ready", event, now, f"{getattr(event, 'start', '')}:{getattr(event, 'handler', '')}", check)


def daily_kline_ready(event: Any, now: datetime, ctx: Any) -> bool:
    def check():
        root = Path(os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser()
        from engine_core.data_pipeline.storage import ArtifactCatalog
        path = ArtifactCatalog(root).locate("daily_kline", now.date().isoformat())
        return bool(path and path.is_file())
    return _cached("daily_kline_ready", event, now, "daily_kline", check)


def default_gates():
    return {"snapshot_0920_ready": auction_snapshot_ready,
            "snapshot_0924_ready": auction_snapshot_ready,
            "snapshot_0925_ready": auction_snapshot_ready,
            "auction_coverage": auction_snapshot_ready,
            "checkpoint_ready": checkpoint_ready,
            "daily_kline_ready": daily_kline_ready}
