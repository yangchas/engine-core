"""Daily-kline fill: TD is the only state. One symbol is one atomic write; a rerun resumes from what TD holds.

The loop reads each day's coverage from TD, then fetches the missing symbols one by one (one range request per
symbol) and writes each symbol to TD at once. It stops as soon as every day in the window reaches the coverage
threshold (partial gaps are allowed) and fails fast when the source is clearly not answering.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence

from ..errors import CoreError
from .published_daily_kline import find_published_baostock_manifest
from .td_sink import (BAOSTOCK_PROGRESS_VERSION, DailyKlineTdSink, has_baostock_date_evidence, map_rows,
                      pick_best, read_watermarks)

MIN_FORMAL_COVERAGE = 0.98  # the floor hard-coded in registry.daily_kline.classify_daily_kline
_MANIFEST = {"provider": "baostock", "source": "baostock", "field_units": {"volume": "shares"}}


def _abort(reason: str, detail: Mapping[str, Any]) -> CoreError:
    parts = ",".join(f"{key}={value}" for key, value in detail.items())
    return CoreError("BAOSTOCK_GAP_UNRESOLVED", f"{reason}:{parts}")


def _marker_is_current(current: Mapping[str, Any] | None, label: str, day: str, row_count: int) -> bool:
    """A Baostock completion marker with date evidence whose row count still matches the stored rows."""
    return (current is not None and label == "baostock" and has_baostock_date_evidence(day, current) and
            int(current.get("row_count") or 0) == row_count)


def listed_symbols(static_rows: Sequence[Mapping[str, Any]], day: str) -> set[str]:
    """Symbols of ``stock_static`` already listed on ``day`` (``listing_date <= day``).

    The expected set for a day older than every ``symbol_universe`` partition. It only knows the stocks listed today,
    so delisted ones are not expected (nor fetched); a stock without a listing date is not expected either.
    """
    return {str(row.get("symbol", "")).zfill(6) for row in static_rows
            if str(row.get("listing_date") or "") and str(row["listing_date"]) <= day and row.get("symbol")}


def run_daily_kline_fill(target: str, *, store: Any, client: Any, trading_dates: Sequence[str],
                         expected_symbols: Callable[[str], set[str]], lease: Any = None, depth: int = 10,
                         coverage_threshold: float = 0.995, max_consecutive_failures: int = 5,
                         max_failure_ratio: float = 0.2, min_failure_samples: int = 20,
                         max_consecutive_empty: int = 30, total_timeout_seconds: float = 43200.0,
                         no_progress_timeout_seconds: float = 300.0,
                         on_complete: Callable[[dict[str, Any]], None] | None = None,
                         needs_publish: Callable[[str], bool] | None = None,
                         clock: Callable[[], float] = time.monotonic, watermark_mode: str = "window",
                         sleep: Callable[[float], None] = time.sleep,
                         baostock_min_request_interval_seconds: float = 0.0, require_all: bool = False) -> dict[str, Any]:
    import math
    from .daily_kline_gapfill import _wait_for_range_start

    if not math.isfinite(baostock_min_request_interval_seconds) or baostock_min_request_interval_seconds < 0:
        raise ValueError("baostock_min_request_interval_seconds_must_be_nonnegative")
    if not 0 < coverage_threshold <= 1:
        raise ValueError("coverage_threshold_must_be_in_0_1")
    sessions = sorted(day for day in trading_dates if day <= target)
    if target not in sessions:
        raise ValueError("target_not_in_trade_calendar:" + target)
    if lease is not None:
        try:
            acquired = lease.acquire()
        except Exception as exc:  # Redis down is transient: retry later, never a final failure
            raise _abort("lease_unavailable", {"error": type(exc).__name__}) from exc
        if not acquired:
            return {"status": "BUSY", "target": target, "reason": "another_fill_is_running"}
    started = clock()
    logged_in = False
    stats = {"written_symbols": 0, "empty": 0, "failed": 0, "attempted": 0}
    planned_days = planned_symbols = rows_written = 0
    days: list[str] = []
    missing: dict[str, list[str]] = {}
    empty_symbols: dict[str, list[str]] = {}
    try:
        days = sessions[-max(1, int(depth)):]
        if not store.schema_exists():
            store.ensure_schema()
        sink = DailyKlineTdSink(store, ".")
        watermarks = read_watermarks(store, days, watermark_mode)
        expected = {day: set(expected_symbols(day)) for day in days}
        present: dict[str, set[str]] = {}
        work: list[str] = []
        unpublished: list[str] = []
        deferred: set[str] = set()
        for day in days:
            if not expected[day]:
                raise _abort("symbol_universe_missing", {"day": day})
            all_rows = store.read_day_rows(day)
            stored = set(all_rows) & expected[day]
            versions = store.read_versions(day)
            current = pick_best(versions)
            label = str((current or {}).get("provider") or (current or {}).get("source") or "").lower()
            untrusted = current is not None and label != "baostock"
            present[day] = set() if untrusted else stored
            if untrusted or len(stored) / len(expected[day]) < (1.0 if require_all else coverage_threshold):
                work.append(day)
                if (_marker_is_current(current, label, day, len(all_rows)) and
                        needs_publish is not None and not needs_publish(day)):
                    deferred.add(day)
            elif not _marker_is_current(current, label, day, len(all_rows)) or \
                    (needs_publish is not None and needs_publish(day)):
                # Enough rows but no usable completion marker (a run killed before publishing, or a manifest that
                # never reached the catalog): publish it again instead of reporting "nothing to do" forever.
                unpublished.append(day)
        if not work and not unpublished:
            return {"status": "NO_GAPS", "target": target, "days": days,
                    "coverage": {day: round(len(present[day]) / len(expected[day]), 4) for day in days},
                    "elapsed_seconds": round(clock() - started, 3),
                    "planned_days": 0, "planned_symbols": 0, "requested": 0, "empty_returns": 0,
                    "rows_written": 0, "incomplete_total": 0, "incomplete": [],
                    "upstream_empty_total": 0, "upstream_empty": [],
                    "published_partitions": 0, "publish_skipped": True}
        prepared = set(work) - deferred
        for day in prepared:  # invalidate markers that cannot be safely retained
            present[day] = set(sink.prepare_progress(day, sorted(expected[day]), latest_watermarks=watermarks))

        def coverage(day: str) -> float:
            return len(present[day]) / len(expected[day])

        def done() -> bool:
            return all(coverage(day) >= (1.0 if require_all else coverage_threshold) for day in work)

        missing: dict[str, list[str]] = {}
        for day in work:
            for symbol in expected[day] - present[day]:
                missing.setdefault(symbol, []).append(day)
        planned_days = len({day for values in missing.values() for day in values})
        planned_symbols = len(missing)
        rows_written = 0
        order = sorted(missing, key=lambda symbol: (target not in missing[symbol], symbol))
        consecutive_failed = consecutive_empty = 0
        if order:
            client.login()
            logged_in = True
        last_progress = clock()
        last_request = None
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
            last_request = _wait_for_range_start(last_request, baostock_min_request_interval_seconds,
                                                 started + total_timeout_seconds, clock, sleep)
            if last_request - started >= total_timeout_seconds:
                raise _abort("total_budget_exhausted", stats)
            if last_request - last_progress >= no_progress_timeout_seconds:
                raise _abort("no_progress", stats)
            stats["attempted"] += 1
            try:
                last_request = clock()
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
                empty_symbols[symbol] = wanted
                stats["empty"] += 1
                consecutive_empty += 1
                consecutive_failed = 0
                # Nothing delivered yet (e.g. the day is not published): do not walk the whole universe to learn it.
                if consecutive_empty >= max_consecutive_empty and stats["written_symbols"] == 0:
                    raise _abort("source_not_delivered", stats)
                continue
            for day in by_day.keys() & deferred:
                present[day] = set(sink.prepare_progress(day, sorted(expected[day]), latest_watermarks=watermarks))
                deferred.remove(day)
                prepared.add(day)
            store.upsert_symbol(symbol, BAOSTOCK_PROGRESS_VERSION, by_day)
            rows_written += len(by_day)
            for day in by_day:
                present[day].add(symbol)
            stats["written_symbols"] += 1
            consecutive_failed = consecutive_empty = 0
            last_progress = clock()
        incomplete = []
        upstream_empty: dict[str, list[str]] = {}
        if require_all:
            read_watermarks(store, days, watermark_mode)
            for day in days:
                actual = set(store.read_day_rows(day))
                for symbol in sorted(expected[day] - actual):
                    if day in empty_symbols.get(symbol, ()) and actual - {symbol}:
                        upstream_empty.setdefault(symbol, []).append(day)
                    else:
                        incomplete.append({"date": day, "symbol": symbol})
        empty_details = [{"symbol": symbol, "days": item_days}
                         for symbol, item_days in sorted(upstream_empty.items())]
        status = ("INCOMPLETE" if incomplete else "COMPLETE") if require_all else (
            "COMPLETE" if done() else "INCOMPLETE")
        result = {"status": status, "target": target, "days": days,
                  "work_days": work, "publish_days": sorted(prepared | set(unpublished)), **stats,
                  "published_partitions": 0, "publish_skipped": False,
                  "planned_days": planned_days, "planned_symbols": planned_symbols,
                  "requested": stats["attempted"], "empty_returns": stats["empty"],
                  "rows_written": rows_written, "incomplete_total": len(incomplete),
                  "incomplete": incomplete[:20], "upstream_empty_total": len(empty_details),
                  "upstream_empty": empty_details[:20],
                  "coverage": {day: round(coverage(day), 4) for day in days if day in present},
                  "elapsed_seconds": round(clock() - started, 3)}
        if result["status"] == "INCOMPLETE" and require_all:
            return result
        if result["status"] == "INCOMPLETE":
            raise _abort("coverage_below_threshold",
                         {**stats, "coverage": result["coverage"], "threshold": coverage_threshold})
        if on_complete is not None and result["publish_days"]:  # lease remains held
            on_complete(result)
            result["published_partitions"] = len(result.get("partitions", result["publish_days"]))
        else:
            result["publish_skipped"] = not result["publish_days"]
        return result
    except Exception as exc:
        if require_all:
            try:
                unresolved = [{"date": day, "symbol": symbol}
                              for symbol, item_days in sorted(missing.items()) for day in item_days
                              if symbol not in store.read_day_rows(day)]
                total = len(unresolved)
            except Exception:
                unresolved, total = [], None
            exc.daily_kline_detail = {
                "planned_days": planned_days, "planned_symbols": planned_symbols,
                "requested": stats["attempted"], "empty_returns": stats["empty"],
                "rows_written": rows_written, "incomplete_total": total,
                "incomplete": unresolved[:20], "upstream_empty_total": 0, "upstream_empty": []}
        raise
    finally:
        if logged_in and callable(getattr(client, "logout", None)):
            try:
                client.logout()
            except Exception:
                pass
        if lease is not None:
            lease.release()


def publish_filled_days(root: Any, store: Any, days: Sequence[str], expected_symbols: Callable[[str], set[str]],
                        coverage_threshold: float) -> list[dict[str, Any]]:
    """Write the completion marker (TD version + catalog manifest) for days whose rows reached the threshold.

    Completion is judged on coverage alone: every missing symbol counts as source-empty and the allowed
    number of those equals the universe size, so FORMAL means coverage >= threshold.
    """
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from .jobs import JobRunner, RequestBudget
    from .registry.daily_kline import DAILY_KLINE_QUALITY_FIELDS

    if coverage_threshold < MIN_FORMAL_COVERAGE:
        # classify_daily_kline downgrades anything below this to PARTIAL/PROVISIONAL whatever the policy says.
        raise ValueError(f"coverage_threshold_below_formal_floor:{MIN_FORMAL_COVERAGE}")
    base = Path(root).expanduser()
    sink = DailyKlineTdSink(store, base)
    runner = JobRunner(artifact_root=base, td_adapter=sink,
                       budget=RequestBudget(max_requests=max(1, len(days)), min_interval_seconds=0))
    quality_fields = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")
    outcomes = []
    for day in days:
        expected = set(expected_symbols(day))
        rows = sorted(sink.read_progress_rows(day, sorted(expected)).values(), key=lambda row: row["symbol"])
        completeness = {field: ("COMPLETE" if all(row.get(field) is not None for row in rows)
                                else "SUSPENDED_ONLY" if all(row.get("suspended") for row in rows)
                                else "PARTIAL") for field in quality_fields}
        meta = {"rows": rows, "source": "baostock", "provider": "baostock", "expected_count": len(expected),
                "field_completeness": completeness, "expected_symbols": sorted(expected),
                "source_symbols": sorted(expected), "source_universe_status": "NOT_REQUIRED",
                "field_units": {"volume": "shares"},
                "source_dates": {field: day for field in DAILY_KLINE_QUALITY_FIELDS},
                "source_date_values": [day], "source_dates_consistent": True,
                "provider_attempts": [{"provider": "baostock", "result": "ok", "reason": "per_symbol_fill"}],
                "available_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "as_of": day}
        result = runner.run("daily_kline", day, fetch=(lambda payload: lambda _day: payload)(meta), td_write=True,
                            source="baostock", expected_count=len(expected),
                            baostock_policy={"formal_coverage_threshold": coverage_threshold,
                                             "source_empty_max": len(expected)})
        outcomes.append({"trade_date": day, "row_count": len(result.rows),
                         "version": result.manifest.get("version"),
                         "business_status": result.manifest.get("business_status"),
                         "mode": result.manifest.get("mode"), "td_publish": result.manifest.get("td_publish"),
                         "error_code": result.manifest.get("error_code")})
    bad = [item for item in outcomes if item["td_publish"] != "READY" or item["mode"] == "RETRYABLE_INCOMPLETE"]
    if bad:
        raise _abort("publish_incomplete", {"days": ",".join(item["trade_date"] for item in bad)})
    return outcomes


def load_listing_snapshot(days: Sequence[str]) -> Mapping[str, Any] | None:
    from .stock_listing import ReferenceSnapshotStore, listed_on, read_active

    snapshot_store = None
    try:
        snapshot_store = ReferenceSnapshotStore()
        snapshot = read_active(snapshot_store)
        if snapshot is None:
            return None
        if not snapshot["version"]:
            return None
        for day in days:
            listed_on(snapshot["rows"], day)
        return snapshot
    except Exception:
        return None
    finally:
        if snapshot_store is not None:
            try:
                snapshot_store.close()
            except Exception:
                pass


def listing_detail(listing: Mapping[str, Any] | None, days: Sequence[str]) -> dict[str, Any]:
    from .stock_listing import listed_on

    return {"listing_source": "snapshot" if listing is not None else "fallback",
            "listing_version": listing["version"] if listing is not None else None,
            "skipped_not_listed": sum(len(listing["rows"]) - len(listed_on(listing["rows"], day))
                                      for day in days) if listing is not None else 0}


def make_expected_symbols(catalog: Any, *, listing: Mapping[str, Any] | None = None) -> Callable[[str], set[str]]:
    """``day -> symbols the day must have``: the newest symbol_universe partition at or before the day (tradable ones);
    for days older than every partition (a deep history load) the stock_static listing dates."""
    universe_dates = sorted((catalog.read().get("datasets", {}).get("symbol_universe", {}).get("dates", {})))
    memo: dict[str, set[str]] = {}

    def expected_symbols(day: str) -> set[str]:
        if day not in memo:
            if listing is not None:
                from .stock_listing import listed_on
                memo[day] = {symbol.rsplit(".", 1)[-1].zfill(6)
                             for symbol in listed_on(listing["rows"], day)}
                return memo[day]
            for candidate in reversed([item for item in universe_dates if item <= day]):
                path = catalog.locate("symbol_universe", candidate)
                if path is not None:
                    memo[day] = {str(row.get("symbol", row.get("code", ""))).zfill(6)
                                 for row in catalog.read_rows(path)
                                 if str(row.get("trade_status", "")).strip() != "0"}
                    break
            else:
                static_path = catalog.locate("stock_static")
                memo[day] = listed_symbols(catalog.read_rows(static_path), day) if static_path is not None else set()
        return memo[day]

    return expected_symbols


def run_daily_kline_fill_from_root(root: Any, target: str, *, args: Mapping[str, Any] | None = None,
                                   store: Any = None, client: Any = None, lease: Any = None,
                                   trading_dates: Sequence[str] | None = None) -> dict[str, Any]:
    """Production entry: wires the real TD store, calendar, local symbol universe, Baostock client and Redis lease."""
    import os
    from pathlib import Path

    options = dict(args or {})
    base = Path(root).expanduser()
    own_store = store is None
    if store is None:
        from ..guardian.daily_kline_coverage import make_store, td_settings
        settings = td_settings()
        if settings is None:
            raise RuntimeError("TD_DAILY_KLINE_BACKFILL_NOT_CONFIGURED")
        store = make_store(settings)
    if trading_dates is None:
        from .trade_calendar import TradeCalendarStore
        trading_dates = TradeCalendarStore(base).all_trading_dates()
    from .storage import ArtifactCatalog
    catalog = ArtifactCatalog(base)
    days = sorted(day for day in trading_dates if day <= target)[-max(1, int(options.get("history_min_trading_days", 10))):]
    listing = load_listing_snapshot(days)
    detail = listing_detail(listing, days)
    expected_symbols = make_expected_symbols(catalog, listing=listing)
    no_progress = float(options.get("baostock_no_progress_seconds", 300))
    if client is None:
        import baostock as bs
        from ..daily_history import BaostockRangeClient
        client = BaostockRangeClient(bs, read_timeout=min(30.0, no_progress))
    if lease is None and str(options.get("fill_lease", "redis")).lower() != "off":
        # "off" exists for read-only rehearsals; production schedules never set it.
        import redis
        from .lease import RedisLease
        url = os.getenv("CORE_REDIS_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
        lease = RedisLease(redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5),
                           "core:lease:daily_kline_fill", ttl_seconds=int(options.get("lease_ttl_seconds", 300)))
    threshold = float((options.get("daily_kline_policy") or {}).get("formal_coverage_threshold", 0.995))
    if threshold < MIN_FORMAL_COVERAGE:
        raise ValueError(f"coverage_threshold_below_formal_floor:{MIN_FORMAL_COVERAGE}")
    def needs_publish(day: str) -> bool:
        manifest = find_published_baostock_manifest(catalog, day, threshold)
        current = pick_best(store.read_versions(day))
        return (manifest is None or current is None or not manifest.get("version") or
                manifest.get("version") != current.get("version"))

    try:
        result = run_daily_kline_fill(
            target, store=store, client=client, trading_dates=trading_dates, expected_symbols=expected_symbols,
            lease=lease, depth=int(options.get("history_min_trading_days", 10)), coverage_threshold=threshold,
            max_consecutive_failures=int(options.get("fill_max_consecutive_failures", 5)),
            max_failure_ratio=float(options.get("fill_max_failure_ratio", 0.2)),
            min_failure_samples=int(options.get("fill_min_failure_samples", 20)),
            max_consecutive_empty=int(options.get("fill_max_consecutive_empty", 30)),
            total_timeout_seconds=float(options.get("total_timeout_seconds", 43200)),
            no_progress_timeout_seconds=no_progress,
            baostock_min_request_interval_seconds=float(options.get("baostock_min_request_interval_seconds", 0.0)),
            require_all=True,
            needs_publish=needs_publish,
            on_complete=lambda done: done.__setitem__(
                "partitions", publish_filled_days(base, store, done["publish_days"], expected_symbols, threshold)))
        result.update(detail)
        if result.get("status") == "INCOMPLETE":
            exc = _abort("planned_gaps_remain", {"incomplete_total": result["incomplete_total"]})
            exc.daily_kline_detail = result
            raise exc
        return result
    except Exception as exc:
        if hasattr(exc, "daily_kline_detail"):
            exc.daily_kline_detail.update(detail)
        raise
    finally:
        if own_store:
            store.close()
