"""Daily-kline fill: TD is the only state. One symbol is one atomic write; a rerun resumes from what TD holds.

The loop reads each day's coverage from TD, then fetches the missing symbols one by one (one range request per
symbol) and writes each symbol to TD at once. It stops as soon as every day in the window reaches the coverage
threshold (partial gaps are allowed) and fails fast when the source is clearly not answering.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence

from .td_sink import BAOSTOCK_PROGRESS_VERSION, DailyKlineTdSink, map_rows, pick_best

_MANIFEST = {"provider": "baostock", "source": "baostock", "field_units": {"volume": "shares"}}


def _abort(reason: str, detail: Mapping[str, Any]) -> RuntimeError:
    # "BAOSTOCK_GAP_UNRESOLVED" is what the guardian already classifies as retryable.
    parts = ",".join(f"{key}={value}" for key, value in detail.items())
    return RuntimeError(f"BAOSTOCK_GAP_UNRESOLVED:{reason}:{parts}")


def run_daily_kline_fill(target: str, *, store: Any, client: Any, trading_dates: Sequence[str],
                         expected_symbols: Callable[[str], set[str]], lease: Any = None, depth: int = 10,
                         coverage_threshold: float = 0.995, max_consecutive_failures: int = 5,
                         max_failure_ratio: float = 0.2, min_failure_samples: int = 20,
                         max_consecutive_empty: int = 30, total_timeout_seconds: float = 43200.0,
                         no_progress_timeout_seconds: float = 300.0,
                         clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    if not 0 < coverage_threshold <= 1:
        raise ValueError("coverage_threshold_must_be_in_0_1")
    sessions = sorted(day for day in trading_dates if day <= target)
    if target not in sessions:
        raise ValueError("target_not_in_trade_calendar:" + target)
    if lease is not None and not lease.acquire():
        return {"status": "BUSY", "target": target, "reason": "another_fill_is_running"}
    started = clock()
    logged_in = False
    try:
        days = sessions[-max(1, int(depth)):]
        if not store.schema_exists():
            store.ensure_schema()
        sink = DailyKlineTdSink(store, ".")
        watermarks = store.latest_watermarks()
        expected = {day: set(expected_symbols(day)) for day in days}
        present: dict[str, set[str]] = {}
        work: list[str] = []
        for day in days:
            if not expected[day]:
                raise _abort("symbol_universe_missing", {"day": day})
            stored = set(store.read_day_rows(day)) & expected[day]
            versions = store.read_versions(day)
            current = pick_best(versions)
            label = str((current or {}).get("provider") or (current or {}).get("source") or "").lower()
            untrusted = current is not None and label != "baostock"
            present[day] = set() if untrusted else stored
            if untrusted or len(stored) / len(expected[day]) < coverage_threshold:
                work.append(day)
        if not work:
            return {"status": "NO_GAPS", "target": target, "days": days,
                    "coverage": {day: round(len(present[day]) / len(expected[day]), 4) for day in days},
                    "elapsed_seconds": round(clock() - started, 3)}
        for day in work:  # drops a stale completion marker / untrusted legacy rows, keeps TD-owned progress
            present[day] = set(sink.prepare_progress(day, sorted(expected[day]), latest_watermarks=watermarks))

        def coverage(day: str) -> float:
            return len(present[day]) / len(expected[day])

        def done() -> bool:
            return all(coverage(day) >= coverage_threshold for day in work)

        missing: dict[str, list[str]] = {}
        for day in work:
            for symbol in expected[day] - present[day]:
                missing.setdefault(symbol, []).append(day)
        order = sorted(missing, key=lambda symbol: (target not in missing[symbol], symbol))
        stats = {"written_symbols": 0, "empty": 0, "failed": 0, "attempted": 0}
        consecutive_failed = consecutive_empty = 0
        if order:
            client.login()
            logged_in = True
        last_progress = clock()
        for symbol in order:
            if done():
                break
            if clock() - started >= total_timeout_seconds:
                raise _abort("total_budget_exhausted", stats)
            if clock() - last_progress >= no_progress_timeout_seconds:
                raise _abort("no_progress", stats)
            if lease is not None and not lease.renew():
                raise _abort("lease_lost", stats)
            wanted = sorted(missing[symbol])
            stats["attempted"] += 1
            try:
                rows = client.query(symbol, wanted[0], target)
            except Exception as exc:
                stats["failed"] += 1
                consecutive_failed += 1
                consecutive_empty = 0
                if getattr(client, "poisoned", False):
                    raise _abort("session_poisoned", {**stats, "error": type(exc).__name__}) from exc
                if consecutive_failed >= max_consecutive_failures:
                    raise _abort("consecutive_failures", {**stats, "error": type(exc).__name__}) from exc
                if (stats["attempted"] >= min_failure_samples and
                        stats["failed"] / stats["attempted"] > max_failure_ratio):
                    raise _abort("failure_ratio", {**stats, "error": type(exc).__name__}) from exc
                continue
            by_day: dict[str, dict[str, Any]] = {}
            for row in rows:
                day = str(row.get("date") or "")[:10]
                if day in wanted:
                    mapped, dropped = map_rows([{**row, "date": day, "symbol": symbol}], _MANIFEST)
                    if dropped or symbol not in mapped:
                        raise _abort("invalid_source_row", {"symbol": symbol, "day": day})
                    by_day[day] = mapped[symbol]
            if not by_day:
                stats["empty"] += 1
                consecutive_empty += 1
                consecutive_failed = 0
                # Nothing delivered yet (e.g. the day is not published): do not walk the whole universe to learn it.
                if consecutive_empty >= max_consecutive_empty and stats["written_symbols"] == 0:
                    raise _abort("source_not_delivered", stats)
                continue
            store.upsert_symbol(symbol, BAOSTOCK_PROGRESS_VERSION, by_day)
            for day in by_day:
                present[day].add(symbol)
            stats["written_symbols"] += 1
            consecutive_failed = consecutive_empty = 0
            last_progress = clock()
        result = {"status": "COMPLETE" if done() else "INCOMPLETE", "target": target, "days": days,
                  "work_days": work, **stats,
                  "coverage": {day: round(coverage(day), 4) for day in days if day in present},
                  "elapsed_seconds": round(clock() - started, 3)}
        if result["status"] == "INCOMPLETE":
            raise _abort("coverage_below_threshold",
                         {**stats, "coverage": result["coverage"], "threshold": coverage_threshold})
        return result
    finally:
        if logged_in and callable(getattr(client, "logout", None)):
            try:
                client.logout()
            except Exception:
                pass
        if lease is not None:
            lease.release()
