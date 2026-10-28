"""Intraday checkpoint: guardian gate probe + capture adapter (C587).

Source order and every threshold come from the event's ``args.checkpoint`` in schedule.json (``sources``,
``tolerance_seconds``, ``min_symbols``, ``td_until``); nothing here chooses a source by itself.

* ``redis_q2``: the live ``q2:{symbol}`` hashes (read-only).  Primary, available the whole session.
* ``td``: ``market_data1.stock_tick_v2`` (read-only), a check / fallback that exists only up to ``td_until``
  (TD stores no tick after 09:45:59; C377).

Exit codes of ``run_checkpoint``: 0 ok / 10 degraded (TD fallback) / 20 not applicable / 30 retryable
(``CHECKPOINT_NOT_YET``, ``SOURCE_UNREADABLE``) / 40 deterministic (``CHECKPOINT_WINDOW_PASSED``) / 64 config.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..data_pipeline import intraday_checkpoint as core
from ..data_pipeline.intraday_checkpoint import CheckpointError

SAMPLE_SIZE = 300


def redis_client(timeout: float = 5.0, env: Mapping[str, str] | None = None) -> Any:
    env = os.environ if env is None else env
    url = env.get("LEGACY_REDIS_URL") or env.get("REDIS_URL") or env.get("CORE_REDIS_URL")
    if not url:
        raise CheckpointError("SOURCE_UNREADABLE", "redis_url_missing", exit_code=30, retryable=True)
    import redis as redis_package
    return redis_package.Redis.from_url(url, decode_responses=True, socket_timeout=timeout,
                                        socket_connect_timeout=timeout)


def _sample_hashes(redis: Any, symbols: list[str]) -> dict[str, dict]:
    step = max(1, len(symbols) // SAMPLE_SIZE)
    picked = symbols[::step][:SAMPLE_SIZE]
    pipe = redis.pipeline(transaction=False)
    for symbol in picked:
        pipe.hgetall("q2:" + symbol)
    return {s: (h or {}) for s, h in zip(picked, pipe.execute())}


def redis_probe(redis: Any, trade_date: str, cutoff: str, cfg: Mapping[str, Any], *, sample_first: bool = True) -> dict:
    """Window state of the live Redis for ``cutoff``.  A cheap sample rules out clearly-not-yet polls."""
    started = time.monotonic()
    cut = core.cutoff_datetime(trade_date, cutoff)
    members = redis.smembers("q2:active:" + trade_date.replace("-", ""))
    symbols = sorted({str(m).zfill(6) for m in members})
    base = {"source": "redis_q2", "expected": len(symbols), "threshold": int(cfg["min_symbols"])}
    if not symbols:
        return {**base, "state": "not_yet", "reason": "Q2_ACTIVE_SET_EMPTY", "in_window": 0,
                "elapsed_ms": round((time.monotonic() - started) * 1000)}
    if sample_first and len(symbols) > SAMPLE_SIZE:
        _inside, sc = core.classify(_sample_hashes(redis, symbols), trade_date, cut, float(cfg["tolerance_seconds"]))
        sampled = sum(sc.values())
        estimate = sc["in_window"] / sampled * len(symbols) if sampled else 0
        if estimate < 0.5 * int(cfg["min_symbols"]):
            past = sc["after_window"] / sampled if sampled else 0
            state = "passed" if len(symbols) * (1 - past) < int(cfg["min_symbols"]) else "not_yet"
            return {**base, "state": state, "reason": "checkpoint_below_threshold", "in_window": int(estimate),
                    "sampled": sampled, "sample_counts": sc, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    symbols, raw = core.read_q2(redis, trade_date)
    _inside, counts = core.classify(raw, trade_date, cut, float(cfg["tolerance_seconds"]))
    state = core.window_state(counts, len(symbols), int(cfg["min_symbols"]))
    return {**base, "state": state, "in_window": counts["in_window"], "counts": counts,
            "reason": "checkpoint_threshold_met" if state == "ready" else "checkpoint_below_threshold",
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


def _td_rows(td: Any, trade_date: str, cut: datetime, tolerance: float) -> list[dict[str, Any]]:
    """Last tick per symbol inside [cut, cut+tolerance] from stock_tick_v2 (columns as live_session._sql_states)."""
    upper = cut + timedelta(seconds=tolerance)
    cursor = td.cursor() if callable(getattr(td, "cursor", None)) else td
    cursor.execute("select last(ts),last(px_milli),last(pc_milli),last(amt_yuan),last(vol_units),last(h_milli),"
                   "last(l_milli),symbol from stock_tick_v2 "
                   f"where ts>='{cut:%Y-%m-%d %H:%M:%S}' and ts<='{upper:%Y-%m-%d %H:%M:%S}' group by symbol")
    rows = []
    for ts, px, pc, amt, vol, high, low, symbol in cursor.fetchall():
        if symbol is None or ts is None:
            continue
        observed = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
        observed = observed.replace(tzinfo=core.SHANGHAI) if observed.tzinfo is None else observed.astimezone(core.SHANGHAI)
        rows.append((str(symbol).zfill(6), {"ts": int(observed.timestamp() * 1000), "px": px, "pc": pc, "amt": amt,
                                              "vol": vol, "mx": high, "mn": low}))
    return rows


def run_checkpoint(request: Mapping[str, Any], root: Path, *, dry_run: bool = False,
                   redis: Any = None, td: Any = None, now: datetime | None = None) -> tuple[int, dict]:
    """Capture one checkpoint right now; ``request['args']['checkpoint']`` is the configuration."""
    from ..data_pipeline.storage import ArtifactCatalog
    started = time.monotonic()
    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    trade_date = str(request.get("data_date") or request.get("trade_date") or "")
    start = str(request.get("start") or "")
    try:
        cfg = core.parse_config(args)
        if not trade_date or not start:
            raise CheckpointError("CONFIG_ERROR", "trade_date_and_start_required", exit_code=64)
        cutoff = start[:5] + ":00"          # same rule as gates._checkpoint_cutoff (minute floor)
    except CheckpointError as exc:
        return _reply(exc.exit_code, "failed", exc.code, exc.retryable, exc.detail)
    if dry_run:
        return _reply(0, "ok", "", False, {"dry_run": True, "checkpoint": cutoff})
    now = now or datetime.now(core.SHANGHAI)
    attempts: list[dict[str, Any]] = []
    cut = core.cutoff_datetime(trade_date, cutoff)
    last_error: CheckpointError | None = None
    for source in cfg["sources"]:
        t0 = time.monotonic()
        try:
            if source == "redis_q2":
                client = redis or redis_client(cfg["redis_timeout_seconds"])
                symbols, raw = core.read_q2(client, trade_date)
                if not symbols:
                    raise CheckpointError("SOURCE_UNREADABLE", "q2_active_set_empty", exit_code=30, retryable=True)
                part = core.build_intraday_checkpoint({
                    "trade_date": trade_date, "cutoff": cutoff, "tolerance_seconds": cfg["tolerance_seconds"],
                    "min_symbols": cfg["min_symbols"], "symbols": symbols, "raw": raw,
                    "read_at": datetime.now(timezone.utc).isoformat(), "provider_chain": cfg["sources"],
                    "provider_attempts": attempts})
            else:
                if cutoff > cfg["td_until"]:
                    raise CheckpointError("SOURCE_NOT_AVAILABLE", f"td_has_no_ticks_after_{cfg['td_until']}", exit_code=30,
                                          retryable=False)
                connection = td or _td_connection()
                pairs = _td_rows(connection, trade_date, cut, cfg["tolerance_seconds"])
                part = core.build_intraday_checkpoint({
                    "trade_date": trade_date, "cutoff": cutoff, "tolerance_seconds": cfg["tolerance_seconds"],
                    "min_symbols": cfg["min_symbols"], "symbols": [s for s, _ in pairs], "raw": dict(pairs),
                    "read_at": datetime.now(timezone.utc).isoformat(), "provider_chain": cfg["sources"],
                    "provider_attempts": attempts})
                part.manifest.update({"source": "td_stock_tick_v2_checkpoint", "provider": "td_stock_tick_v2_checkpoint",
                                      "degraded": True, "source_dependency": "TD market_data1.stock_tick_v2 (read-only)"})
        except CheckpointError as exc:
            attempts.append({"provider": source, "result": exc.code, "elapsed_ms": round((time.monotonic() - t0) * 1000),
                             "reason": exc.detail[:200], "row_count": 0})
            last_error = exc
            continue
        except Exception as exc:                                    # connection errors etc. -> retryable
            attempts.append({"provider": source, "result": "SOURCE_UNREADABLE",
                             "elapsed_ms": round((time.monotonic() - t0) * 1000),
                             "reason": f"{type(exc).__name__}:{str(exc)[:160]}", "row_count": 0})
            last_error = CheckpointError("SOURCE_UNREADABLE", type(exc).__name__, exit_code=30, retryable=True)
            continue
        attempts.append({"provider": source, "result": "ok" if part.notes["state"] == "ready" else part.notes["state"],
                         "elapsed_ms": round((time.monotonic() - t0) * 1000), "reason": None,
                         "row_count": len(part.rows), "dropped": part.notes["counts"]})
        part.manifest["provider_attempts"] = list(attempts)
        if part.notes["state"] == "ready":
            catalog = ArtifactCatalog(root)
            result = catalog.commit(core.DATASET, trade_date, part.rows, part.manifest,
                                    source=part.manifest["source"], authority="LIVE")
            degraded = bool(part.manifest["degraded"])
            return _reply(10 if degraded else 0, "degraded" if degraded else "ok", "", False, {
                "checkpoint": cutoff, "source": part.manifest["source"], "row_count": len(part.rows),
                "coverage": part.manifest["quality"]["coverage"], "version": result["version"],
                "manifest": result["manifest_uri"], "dropped": part.notes["counts"],
                "elapsed_ms": round((time.monotonic() - started) * 1000)})
        last_error = (CheckpointError("CHECKPOINT_WINDOW_PASSED", str(part.notes["counts"]), exit_code=40)
                      if part.notes["state"] == "passed" else
                      CheckpointError("CHECKPOINT_NOT_YET", str(part.notes["counts"]), exit_code=30, retryable=True))
    code = last_error.code if last_error else "SOURCE_UNREADABLE"
    exit_code = last_error.exit_code if last_error else 30
    retryable = last_error.retryable if last_error else True
    return _reply(exit_code, "failed", code, retryable,
                  {"checkpoint": cutoff, "attempts": attempts, "cutoff": cut.isoformat()})


def _td_connection():
    if not os.environ.get("TD_CONFIG"):
        raise CheckpointError("SOURCE_UNREADABLE", "td_unavailable", exit_code=30, retryable=True)
    import taos
    return taos.connect(database="market_data1", config=os.environ["TD_CONFIG"])


def _reply(exit_code: int, status: str, code: str, retryable: bool, detail: Any) -> tuple[int, dict]:
    return exit_code, {"contract": 1, "status": status, "error_code": code, "retryable": retryable,
                       "artifacts": [], "detail": detail}
