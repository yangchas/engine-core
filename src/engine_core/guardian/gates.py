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


def _td_checkpoint_ready(event: Any, now: datetime, ctx: Any) -> GateResult:
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
    return _cached("checkpoint_ready_td", event, now, f"{getattr(event, 'start', '')}:{getattr(event, 'handler', '')}", check)


def checkpoint_ready(event: Any, now: datetime, ctx: Any) -> GateResult:
    """Config-driven checkpoint gate (C587).

    An event whose ``args.checkpoint`` is set (schedule.json) walks ``sources`` in the configured order: ``redis_q2``
    reads the live ``q2:{symbol}`` hashes and needs ``min_symbols`` rows whose own ``ts`` lies in
    ``[cutoff, cutoff + tolerance_seconds]``; ``td`` is the legacy stock_tick_v2 check and only counts up to ``td_until``.
    An event without that block keeps the TD-only behaviour (open_report events).
    """
    args = getattr(event, "args", None)
    if not isinstance(args, dict) or args.get("checkpoint") is None:
        return _td_checkpoint_ready(event, now, ctx)

    def check():
        from . import intraday_checkpoint_run as run
        from ..data_pipeline.intraday_checkpoint import CheckpointError, parse_config
        started = time.monotonic()
        try:
            cfg = parse_config(args)
        except CheckpointError as exc:
            return GateResult(status="not_ready", reason="CHECKPOINT_CONFIG_ERROR", detail=exc.detail, elapsed_ms=0)
        cutoff = _checkpoint_cutoff(event, now.date())
        stamp = cutoff.strftime("%H:%M:%S")
        window = [cutoff.strftime("%Y-%m-%d %H:%M:%S"),
                  (cutoff + timedelta(seconds=cfg["tolerance_seconds"])).strftime("%Y-%m-%d %H:%M:%S")]
        detail = {"query_window": window, "threshold": cfg["min_symbols"], "sources": cfg["sources"]}
        if cutoff > now.replace(tzinfo=None):
            return GateResult(status="not_ready", reason="WINDOW_NOT_REACHED", row_count=0,
                              elapsed_ms=round((time.monotonic() - started) * 1000), **detail)
        tried = []
        for source in cfg["sources"]:
            if source == "redis_q2":
                try:
                    if isinstance(ctx, dict) and "q2_redis" in ctx:
                        client = ctx["q2_redis"]
                    else:
                        client = run.redis_client(cfg["redis_timeout_seconds"])
                    probe = run.redis_probe(client, now.date().isoformat(), stamp, cfg)
                except Exception as exc:
                    tried.append({"source": "redis_q2", "state": "error", "reason": type(exc).__name__})
                    continue
                tried.append(probe)
                if probe["state"] == "ready":
                    return GateResult(status="ready", reason="checkpoint_threshold_met", source="redis_q2",
                                      row_count=probe["in_window"], probes=tried,
                                      elapsed_ms=round((time.monotonic() - started) * 1000), **detail)
            else:
                if stamp > cfg["td_until"]:
                    tried.append({"source": "td", "state": "skipped", "reason": "td_has_no_ticks_after_" + cfg["td_until"]})
                    continue
                td_result = _td_checkpoint_ready(event, now, ctx)
                tried.append({"source": "td", "state": td_result.get("status"), "reason": td_result.get("reason"),
                              "row_count": td_result.get("row_count")})
                if td_result.get("status") == "ready":
                    return GateResult(status="ready", reason="checkpoint_threshold_met", source="td",
                                      row_count=td_result.get("row_count"), probes=tried,
                                      checkpoint_ts=td_result.get("checkpoint_ts"),
                                      elapsed_ms=round((time.monotonic() - started) * 1000), **detail)
        primary = tried[0] if tried else {}
        reason = ("REDIS_UNREADABLE" if primary.get("state") == "error" else
                  "CHECKPOINT_WINDOW_PASSED" if primary.get("state") == "passed" else
                  "checkpoint_below_threshold")
        return GateResult(status="not_ready", reason=reason, row_count=primary.get("in_window", 0), probes=tried,
                          elapsed_ms=round((time.monotonic() - started) * 1000), **detail)
    return _cached("checkpoint_ready", event, now, f"{getattr(event, 'start', '')}:{getattr(event, 'handler', '')}", check)


def daily_kline_ready(event: Any, now: datetime, ctx: Any) -> GateResult:
    def check():
        started = time.monotonic()
        root = Path(os.environ.get("CORE_DATA_ROOT", "~/core-data")).expanduser()
        from engine_core.data_pipeline.storage import ArtifactCatalog
        catalog = ArtifactCatalog(root)
        day = now.date().isoformat()
        path = catalog.locate("daily_kline", day)
        if not path or not path.is_file():
            return GateResult(status="not_ready", reason="DAILY_KLINE_MISSING",
                              trade_date=day, elapsed_ms=round((time.monotonic() - started) * 1000))
        import json
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            row_count = int(manifest.get("row_count", 0))
            available_at = manifest.get("available_at")
            available_dt = datetime.fromisoformat(str(available_at).replace("Z", "+00:00"))
        except (OSError, ValueError, TypeError):
            return GateResult(status="not_ready", reason="DAILY_KLINE_MANIFEST_INVALID",
                              trade_date=day, elapsed_ms=round((time.monotonic() - started) * 1000))
        if manifest.get("trade_date") != day:
            reason = "DAILY_KLINE_DATE_MISMATCH"
            universe_count = None
        else:
            universe_count = None
            try:
                entry = catalog.read().get("datasets", {}).get("symbol_universe", {})
                versions = entry.get("dates", {}).get(day, [])
                if versions:
                    universe_path = Path(versions[-1]["manifest"])
                    universe_manifest = json.loads(universe_path.read_text(encoding="utf-8"))
                    universe_count = int(universe_manifest.get("row_count", 0))
            except (OSError, ValueError, TypeError, KeyError):
                universe_count = None
            reason = ""
        fallback = universe_count is None or universe_count <= 0
        denominator = 4000 if fallback else universe_count
        threshold = 0.95
        coverage = row_count / denominator if denominator else 0.0
        ready = (not reason and available_dt.tzinfo is not None and
                 available_dt <= (now if now.tzinfo else now.replace(tzinfo=available_dt.tzinfo)) and
                 coverage >= threshold)
        if not reason:
            reason = ("DAILY_KLINE_AVAILABLE_AT_INVALID" if available_dt.tzinfo is None else
                      "DAILY_KLINE_NOT_YET_AVAILABLE" if available_dt > (now if now.tzinfo else now.replace(tzinfo=available_dt.tzinfo)) else
                      "DAILY_KLINE_COVERAGE_BELOW_THRESHOLD" if coverage < threshold else "DAILY_KLINE_READY")
        return GateResult(status="ready" if ready else "not_ready", reason=reason,
                          trade_date=day, row_count=row_count, universe_count=denominator,
                          universe_fallback=("missing_or_invalid_symbol_universe" if fallback else None),
                          coverage=coverage, threshold=threshold, available_at=available_at,
                          version=manifest.get("version"), elapsed_ms=round((time.monotonic() - started) * 1000))
    return _cached("daily_kline_ready", event, now, "daily_kline", check)


def default_gates():
    return {"snapshot_0920_ready": auction_snapshot_ready,
            "snapshot_0924_ready": auction_snapshot_ready,
            "snapshot_0925_ready": auction_snapshot_ready,
            "auction_coverage": auction_snapshot_ready,
            "checkpoint_ready": checkpoint_ready,
            "daily_kline_ready": daily_kline_ready}
