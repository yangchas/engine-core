"""TD-watermark-driven Baostock range repair for recent daily bars."""
from __future__ import annotations

import gzip
import json
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .storage import _atomic_write, canonical_json

SHANGHAI = ZoneInfo("Asia/Shanghai")
_SYMBOL = re.compile(r"^[0-9]{6}$")
_QUALITY_FIELDS = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")


def _all_baostock_symbols(module: Any, target: str) -> list[str]:
    """Ask Baostock for its current stock universe; this is one upstream request, never a TD per-symbol read."""
    from ..daily_kline_capture import to_baostock_code
    result = module.query_all_stock(day=target)
    if getattr(result, "error_code", "0") != "0":
        raise RuntimeError("BAOSTOCK_UNIVERSE_QUERY_FAILED")
    fields = list(getattr(result, "fields", ()))
    code_index = fields.index("code") if "code" in fields else 0
    symbols = set()
    while result.next():
        row = result.get_row_data()
        raw = str(row[code_index] if code_index < len(row) else "")
        symbol = raw.rsplit(".", 1)[-1]
        if _SYMBOL.fullmatch(symbol):
            try:
                to_baostock_code(symbol)
            except ValueError:
                continue
            symbols.add(symbol)
    if not symbols:
        raise RuntimeError("BAOSTOCK_UNIVERSE_EMPTY")
    return sorted(symbols)


def _load_checkpoint(path: Path, symbol: str, start: str, end: str) -> list[dict[str, Any]] | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError):
        return None
    if (payload.get("provider") != "baostock" or payload.get("symbol") != symbol or
            payload.get("start_date") != start or payload.get("end_date") != end):
        return None
    return payload.get("rows", [])


def _save_checkpoint(path: Path, symbol: str, start: str, end: str,
                     rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = gzip.compress(canonical_json({"provider": "baostock", "symbol": symbol,
                                         "start_date": start, "end_date": end, "rows": rows}), mtime=0)
    _atomic_write(path, body)


def _td_rows_to_daily(day: str, stored: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {symbol: {"symbol": symbol, "date": day, "open": row.get("open"), "high": row.get("high"),
                     "low": row.get("low"), "close": row.get("close"), "pre_close": row.get("pre_close"),
                     "pct_chg": row.get("pct_chg"), "volume": row.get("volume_shares"),
                     "amount_yuan": row.get("amount_yuan"), "turnover_pct": row.get("turnover_pct"),
                     "suspended": row.get("suspended")}
            for symbol, row in stored.items()}


def run_td_daily_kline_gapfill(root: str | Path, target: str, *, store: Any,
                               history_min_trading_days: int = 10,
                               total_timeout_seconds: float = 6900.0,
                               client: Any = None) -> dict[str, Any]:
    """Fetch one interval per lagging symbol, merge TD rows by day, and commit catalog+TD in one task."""
    from ..daily_history import BaostockRangeClient
    from ..guardian.daily_kline_coverage import build_gap_fill_plan
    from .trade_calendar import TradeCalendarStore
    from .jobs import JobRunner, RequestBudget
    from .td_sink import DailyKlineTdSink, TdUnavailable

    if total_timeout_seconds <= 0:
        raise ValueError("total_timeout_seconds_must_be_positive")
    started = time.monotonic()
    deadline = started + float(total_timeout_seconds)
    base = Path(root).expanduser()
    sessions = [day for day in TradeCalendarStore(base).all_trading_dates() if day <= target]
    if target not in sessions:
        raise ValueError("target_not_in_trade_calendar:" + target)
    depth = max(1, int(history_min_trading_days))
    dates = sessions[-depth:]
    if not store.schema_exists():
        store.ensure_schema()
    # Exactly one stable-level aggregation supplies every current watermark.
    watermarks = store.latest_watermarks()
    from ..guardian.daily_kline_coverage import TdCoverageSource
    coverage = TdCoverageSource(store).coverage(dates, watermarks)
    if client is None:
        import baostock as bs
        range_client = BaostockRangeClient(bs, read_timeout=30.0)
    else:
        range_client = client
    from ..daily_kline_capture import call_with_timeout
    logout_error = None
    try:
        range_client.login()
        module = getattr(range_client, "module", None)
        if module is None:
            raise RuntimeError("baostock_range_client_missing_module")
        timeout = float(getattr(range_client, "read_timeout", 30.0))
        try:
            universe = call_with_timeout(_all_baostock_symbols, module, target, timeout_seconds=timeout)
        except TimeoutError:
            setattr(range_client, "poisoned", True)
            raise
        planner_states: dict[str, Any] = {}
        symbol_gap_dates: dict[str, list[str]] = defaultdict(list)
        for day, cov in coverage.items():
            if cov.state in {"MISSING", "LOW_QUALITY"}:
                planner_states[day] = cov
            elif cov.state == "INCOMPLETE" and cov.reason != "symbol_watermark_behind_target":
                held = store.read_day_rows(day)
                missing_symbols = set(universe) - set(held)
                if missing_symbols:
                    for symbol in missing_symbols:
                        symbol_gap_dates[symbol].append(day)
                else:
                    # Counts/metadata disagree even though the current universe is present; rebuild the day.
                    planner_states[day] = cov
        # A lagging/new symbol gets one range request, with C561 serial pacing and no extra sessions.
        # New listings without a watermark are treated as missing for the configured recent window.
        gap_plan = build_gap_fill_plan(watermarks, target, sessions, depth,
                                       day_states=planner_states, symbols=universe,
                                       symbol_gap_dates=symbol_gap_dates)
        fetched_by_day: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        checkpoint_root = base / ".checkpoints" / f"daily-kline-td-gap-{target}"
        actual_queries = 0
        for item in gap_plan:  # already sorted by newest start date first
            if time.monotonic() >= deadline:
                raise TimeoutError("BAOSTOCK_TOTAL_BUDGET_EXHAUSTED")
            symbol, start, end = item["symbol"], item["start_date"], item["end_date"]
            checkpoint = checkpoint_root / f"range-{start}-{end}" / f"{symbol}.json.gz"
            rows = _load_checkpoint(checkpoint, symbol, start, end)
            if rows is not None and not any(str(row.get("date")) == target for row in rows):
                rows = None
            if rows is None:
                rows = range_client.query(symbol, start, end)
                actual_queries += 1
                # An empty/target-incomplete response is not a completed range. Keep it retryable.
                if any(str(row.get("date")) == target for row in rows):
                    _save_checkpoint(checkpoint, symbol, start, end, rows)
            for row in rows:
                fetched_by_day[str(row["date"])][symbol] = {**row, "date": str(row["date"]), "symbol": symbol}
    finally:
        module = getattr(range_client, "module", None)
        if getattr(range_client, "poisoned", False):
            logout_error = "skipped_poisoned_session"
        elif callable(getattr(range_client, "logout", None)):
            range_client.logout()
            logout_error = getattr(range_client, "logout_error", None)
        elif hasattr(module, "logout"):
            try:
                call_with_timeout(module.logout, timeout_seconds=float(getattr(range_client, "read_timeout", 30.0)))
            except Exception as exc:  # cleanup failure does not invalidate rows/checkpoints already obtained
                logout_error = type(exc).__name__ + ":" + str(exc)[:160]

    if not gap_plan:
        return {"status": "NO_GAPS", "target": target, "watermark_symbols": len(watermarks),
                "range_symbols": 0, "range_queries": 0, "partitions": [],
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "baostock_logout_error": logout_error}

    affected_dates = sorted({day for item in gap_plan for day in item["trade_dates"]}, reverse=True)
    runner = JobRunner(artifact_root=base,
                       td_adapter=DailyKlineTdSink(store, base),
                       budget=RequestBudget(max_requests=max(1, len(gap_plan)), min_interval_seconds=0))
    outcomes = []
    target_gap_symbols = {str(item["symbol"]) for item in gap_plan if target in item["trade_dates"]}
    for day in affected_dates:
        versions = store.read_versions(day)
        baostock_versions = {str(item.get("version")) for item in versions
                             if str(item.get("provider", "")).lower() == "baostock"}
        stored = store.read_day_rows(day) if baostock_versions else {}
        existing = _td_rows_to_daily(day, {symbol: row for symbol, row in stored.items()
                                           if str(row.get("version") or "") in baostock_versions})
        merged = {**existing, **fetched_by_day.get(day, {})}
        if not merged:
            continue
        if day == target:
            missing_symbols = sorted(target_gap_symbols - set(merged))
            if missing_symbols:
                raise RuntimeError("BAOSTOCK_GAP_UNRESOLVED:target_symbols_missing:" +
                                   ",".join(missing_symbols[:12]))
        rows = sorted(merged.values(), key=lambda row: row["symbol"])
        completeness = {field: ("COMPLETE" if all(row.get(field) is not None for row in rows)
                                else "SUSPENDED_ONLY" if all(row.get("suspended") for row in rows)
                                else "PARTIAL") for field in _QUALITY_FIELDS}
        meta = {"rows": rows, "source": "baostock", "provider": "baostock",
                "expected_count": len(universe), "field_completeness": completeness,
                "provider_attempts": [{"provider": "baostock", "result": "ok",
                                       "reason": "td_watermark_range_fill"}],
                "available_at": datetime.now(SHANGHAI).isoformat(),
                "as_of": day, "request_count": len([x for x in gap_plan if day in x["trade_dates"]])}
        result = runner.run("daily_kline", day, fetch=lambda _day: meta,
                            td_write=True, source="baostock", expected_count=len(universe))
        outcomes.append({"trade_date": day, "row_count": len(result.rows), "version": result.manifest["version"],
                         "sha256": result.manifest["sha256"], "td_publish": result.manifest.get("td_publish")})
    if target not in {str(item["trade_date"]) for item in outcomes}:
        raise RuntimeError("BAOSTOCK_GAP_UNRESOLVED:target_partition_missing:" + target)
    return {"status": "OK", "target": target, "watermark_symbols": len(watermarks),
            "range_symbols": len(gap_plan), "range_queries": actual_queries, "partitions": outcomes,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "baostock_logout_error": logout_error}
