"""TD-watermark-driven Baostock range repair for recent daily bars."""
from __future__ import annotations

import gzip
import json
import multiprocessing
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
    """Read the source universe for C594 classification, not gap discovery."""
    return _all_baostock_symbols_with_count(module, target)[0]


def _all_baostock_symbols_with_count(module: Any, target: str) -> tuple[list[str], int]:
    from ..daily_kline_capture import to_baostock_code
    result = module.query_all_stock(day=target)
    if getattr(result, "error_code", "0") != "0":
        raise RuntimeError("BAOSTOCK_UNIVERSE_QUERY_FAILED")
    fields = list(getattr(result, "fields", ()))
    code_index = fields.index("code") if "code" in fields else 0
    status_index = fields.index("tradeStatus") if "tradeStatus" in fields else None
    symbols = set()
    row_count = 0
    while result.next():
        row_count += 1
        row = result.get_row_data()
        if status_index is not None and status_index < len(row) and str(row[status_index]).strip() == "0":
            continue
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
    return sorted(symbols), row_count


def _baostock_universe_worker(target: str, result_queue: Any) -> None:
    """Fetch classification data in a disposable process so a stalled query cannot poison range IO."""
    module = None
    try:
        import baostock as module
        login = module.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError("BAOSTOCK_LOGIN_FAILED")
        try:
            symbols, row_count = _all_baostock_symbols_with_count(module, target)
            result_queue.put({"symbols": symbols, "row_count": row_count})
        finally:
            module.logout()
    except BaseException as exc:
        try:
            result_queue.put({"error": type(exc).__name__})
        except Exception:
            pass


def _fetch_baostock_universe_isolated(target: str, timeout_seconds: float) -> tuple[list[str], int]:
    context = multiprocessing.get_context("spawn")
    results = context.Queue(maxsize=1)
    process = context.Process(target=_baostock_universe_worker, args=(target, results), daemon=True)
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(2)
        raise TimeoutError("BAOSTOCK_TIMEOUT")
    try:
        result = results.get(timeout=1)
    except Exception as exc:
        raise RuntimeError("BAOSTOCK_UNIVERSE_WORKER_FAILED") from exc
    finally:
        results.close()
    if "error" in result:
        raise RuntimeError("BAOSTOCK_UNIVERSE_WORKER_FAILED:" + str(result["error"]))
    return list(result["symbols"]), int(result["row_count"])


def _source_universe_cache_path(root: Path, target: str) -> Path:
    return root / ".checkpoints" / "baostock-query-all-stock" / f"{target}.json"


def _load_source_universe_cache(path: Path, target: str) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    symbols = value.get("symbols")
    if (value.get("schema") != "BaostockSourceUniverseCacheV1" or
            value.get("provider") != "baostock.query_all_stock" or
            value.get("trade_date") != target or not isinstance(symbols, list) or
            not symbols or any(not _SYMBOL.fullmatch(str(item)) for item in symbols) or
            len(set(symbols)) != len(symbols) or
            type(value.get("row_count")) is not int or value.get("row_count") < len(symbols) or
            value.get("symbol_count") != len(symbols) or
            not value.get("fetched_at")):
        return None
    return value


def _save_source_universe_cache(path: Path, target: str,
                                symbols: list[str], row_count: int) -> dict[str, Any]:
    value = {"schema": "BaostockSourceUniverseCacheV1", "provider": "baostock.query_all_stock",
             "trade_date": target, "fetched_at": datetime.now(SHANGHAI).isoformat(),
             "row_count": row_count, "symbol_count": len(symbols), "symbols": sorted(set(symbols))}
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, canonical_json(value) + b"\n")
    return value


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


def _baostock_gapfill_worker(tasks: list[dict[str, Any]], checkpoint_root: str,
                            read_timeout: float, events: Any) -> None:
    """Run an isolated, serial Baostock stream for one bounded share of gap symbols."""
    from ..daily_history import BaostockRangeClient
    import baostock as bs

    client = BaostockRangeClient(bs, read_timeout=read_timeout)
    try:
        client.login()
        for item in tasks:
            symbol, start, end = item["symbol"], item["start_date"], item["end_date"]
            events.put({"kind": "requested", "symbol": symbol})
            rows = client.query(symbol, start, end)
            _save_checkpoint(Path(checkpoint_root) / f"range-{start}-{end}" / f"{symbol}.json.gz",
                             symbol, start, end, rows)
            events.put({"kind": "result", "symbol": symbol, "row_count": len(rows)})
    except BaseException as exc:
        try:
            events.put({"kind": "error", "error": type(exc).__name__,
                        "message": str(exc)[:160]})
        except Exception:
            pass
    finally:
        client.logout()


def _fetch_gap_ranges_parallel(gap_plan: list[dict[str, Any]], checkpoint_root: Path, *,
                               concurrency: int, read_timeout: float, deadline: float,
                               no_progress_timeout: float, last_progress_at: float) -> tuple[int, float]:
    """Fetch sorted per-symbol ranges in at most two private sessions, honoring both budgets."""
    context = multiprocessing.get_context("spawn")
    events = context.Queue()
    shares = [gap_plan[index::concurrency] for index in range(concurrency)]
    processes = [context.Process(target=_baostock_gapfill_worker,
                                 args=(share, str(checkpoint_root), read_timeout, events), daemon=True)
                 for share in shares if share]
    requested = 0
    error = None
    for process in processes:
        process.start()
    try:
        while any(process.is_alive() for process in processes):
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("BAOSTOCK_TOTAL_BUDGET_EXHAUSTED")
            if now - last_progress_at >= no_progress_timeout:
                raise TimeoutError("BAOSTOCK_NO_PROGRESS_TIMEOUT")
            try:
                event = events.get(timeout=min(0.1, deadline - now))
            except Exception:
                event = None
            if event is None:
                continue
            if event.get("kind") == "requested":
                requested += 1
            elif event.get("kind") == "result" and event.get("row_count", 0):
                last_progress_at = time.monotonic()
            elif event.get("kind") == "error":
                error = event
                break
        for process in processes:
            process.join(timeout=0.2)
            if process.exitcode not in (0, None) and error is None:
                error = {"error": "BaostockRangeWorkerExit", "message": str(process.exitcode)}
        while True:
            try:
                event = events.get_nowait()
            except Exception:
                break
            if event.get("kind") == "requested":
                requested += 1
            elif event.get("kind") == "result" and event.get("row_count", 0):
                last_progress_at = time.monotonic()
            elif event.get("kind") == "error" and error is None:
                error = event
        if error is not None:
            raise RuntimeError("BAOSTOCK_RANGE_WORKER_FAILED:" + str(error.get("error")) + ":" +
                               str(error.get("message", "")))
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(2)
        events.close()
    return requested, last_progress_at


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
                               no_progress_timeout_seconds: float = 300.0,
                               source_universe_timeout_seconds: float = 300.0,
                               range_concurrency: int = 1,
                               prefetch_source_universe: bool = False,
                               client: Any = None, only_source_empty: bool = False,
                               minimum_coverage: float = 0.90,
                               completion_policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Use local symbol partitions for gaps; query Baostock's source list only for C594 classification."""
    from ..daily_history import BaostockRangeClient
    from ..guardian.daily_kline_coverage import build_gap_fill_plan
    from .trade_calendar import TradeCalendarStore
    from .jobs import JobRunner, RequestBudget
    from .td_sink import DailyKlineTdSink, TdUnavailable

    if total_timeout_seconds <= 0:
        raise ValueError("total_timeout_seconds_must_be_positive")
    if no_progress_timeout_seconds <= 0:
        raise ValueError("no_progress_timeout_seconds_must_be_positive")
    if source_universe_timeout_seconds <= 0:
        raise ValueError("source_universe_timeout_seconds_must_be_positive")
    if type(range_concurrency) is not int or not 1 <= range_concurrency <= 2:
        raise ValueError("range_concurrency_must_be_1_or_2")
    if client is not None and range_concurrency != 1:
        raise ValueError("range_concurrency_requires_isolated_baostock_client")
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
    from .storage import ArtifactCatalog
    catalog = ArtifactCatalog(base)
    manifest_path = catalog.locate("daily_kline", target)
    partition_rows: list[dict[str, Any]] = []
    partition_manifest: dict[str, Any] = {}
    known_missing = {}
    if manifest_path is not None:
        try:
            current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            partition_manifest = current_manifest
            partition_rows = catalog.read_rows(manifest_path)
            current_quality = current_manifest.get("quality") or {}
            value = current_quality.get("known_missing")
            if isinstance(value, Mapping):
                known_missing = value
        except (OSError, ValueError, TypeError):
            known_missing = {}
    artifact_symbols_by_day = {
        target: {str(row.get("symbol", row.get("code", ""))).zfill(6) for row in partition_rows}
    }
    td_symbols_by_day: dict[str, set[str]] = {}

    def artifact_symbols(day: str) -> set[str]:
        if day not in artifact_symbols_by_day:
            day_manifest = catalog.locate("daily_kline", day)
            rows = catalog.read_rows(day_manifest) if day_manifest is not None else []
            artifact_symbols_by_day[day] = {
                str(row.get("symbol", row.get("code", ""))).zfill(6) for row in rows
            }
        return artifact_symbols_by_day[day]

    def td_symbols(day: str) -> set[str]:
        if day not in td_symbols_by_day:
            td_symbols_by_day[day] = set(store.read_day_rows(day))
        return td_symbols_by_day[day]

    coverage = TdCoverageSource(store).coverage(dates, watermarks, known_missing)
    if client is None:
        import baostock as bs
        range_client = BaostockRangeClient(bs, read_timeout=min(30.0, float(no_progress_timeout_seconds)))
    else:
        range_client = client
    from ..daily_kline_capture import call_with_timeout
    logout_error = None
    source_universe_status = "NOT_REQUIRED"
    source_universe_error = None
    source_universe_symbols: set[str] | None = None
    source_universe_attempted = False
    source_universe_cache = _source_universe_cache_path(base, target)
    source_universe_provenance: dict[str, Any] | None = None
    source_cache = _load_source_universe_cache(source_universe_cache, target)
    if source_cache is not None:
        source_universe_symbols = set(source_cache["symbols"])
        source_universe_status = "READY"
        source_universe_provenance = {"fetched_at": source_cache["fetched_at"],
                                      "row_count": source_cache["row_count"],
                                      "symbol_count": source_cache["symbol_count"], "cache": "HIT"}

    expected_symbols_by_day: dict[str, set[str] | None] = {}
    universe_source_day_by_day: dict[str, str | None] = {}

    def active_symbols_for_day(day: str) -> set[str] | None:
        if day not in expected_symbols_by_day:
            candidates = [candidate for candidate in sessions if candidate <= day]
            for candidate in reversed(candidates):
                universe_path = catalog.locate("symbol_universe", candidate)
                if universe_path is None:
                    continue
                rows = catalog.read_rows(universe_path)
                expected_symbols_by_day[day] = {
                    str(row.get("symbol", row.get("code", ""))).zfill(6)
                    for row in rows if str(row.get("trade_status", "")).strip() != "0"
                }
                universe_source_day_by_day[day] = candidate
                break
            else:
                expected_symbols_by_day[day] = None
                universe_source_day_by_day[day] = None
        return expected_symbols_by_day[day]

    def get_source_universe() -> set[str] | None:
        nonlocal source_universe_symbols, source_universe_status, source_universe_attempted
        nonlocal source_universe_error, source_universe_provenance
        if source_universe_symbols is not None:
            return source_universe_symbols
        if source_universe_attempted:
            return None
        source_universe_attempted = True
        try:
            if client is None:
                symbols, row_count = _fetch_baostock_universe_isolated(
                    target, float(source_universe_timeout_seconds))
            else:
                module = getattr(range_client, "module", None)
                if module is None:
                    raise RuntimeError("baostock_range_client_missing_module")
                symbols, row_count = call_with_timeout(
                    _all_baostock_symbols_with_count, module, target,
                    timeout_seconds=float(source_universe_timeout_seconds))
            source_cache = _save_source_universe_cache(
                source_universe_cache, target, symbols, row_count)
            source_universe_symbols = set(source_cache["symbols"])
            source_universe_status = "READY"
            source_universe_provenance = {"fetched_at": source_cache["fetched_at"],
                                          "row_count": row_count,
                                          "symbol_count": len(source_universe_symbols), "cache": "MISS"}
            return source_universe_symbols
        except Exception as exc:
            source_universe_status = "UNAVAILABLE"
            source_universe_error = "BAOSTOCK_TIMEOUT" if isinstance(exc, TimeoutError) else type(exc).__name__
            return None

    # Explicit night prefetch is opt-in. Daytime runs make this request only when classification is needed.
    now_local = datetime.now(SHANGHAI)
    in_night_window = ((now_local.hour, now_local.minute) >= (22, 30) or now_local.hour < 8)
    if (prefetch_source_universe and in_night_window and
            source_universe_symbols is None):
        get_source_universe()

    gap_plan: list[dict[str, Any]] = []
    eligibility_status = "NO_GAPS"
    target_universe = active_symbols_for_day(target)
    target_missing_categories: dict[str, str] = {}
    target_classification_symbols: set[str] | None = None
    logged_in = False
    try:
        if only_source_empty:
            if (manifest_path is None or target_universe is None or
                    str(partition_manifest.get("provider", "")).lower() != "baostock"):
                gap_plan = []
                eligibility_status = "NOT_ELIGIBLE"
            else:
                partition_symbols = {str(row.get("symbol", row.get("code", ""))).zfill(6)
                                     for row in partition_rows}
                from .baostock_completion import (classify_missing_symbols,
                                                  evaluate_baostock_completion,
                                                  should_wait_for_baostock_gap)
                policy = dict(completion_policy or {})
                if "formal_coverage_threshold" not in policy or "source_empty_max" not in policy:
                    raise ValueError("baostock_gapfill_completion_policy_required")
                missing_symbols = target_universe - partition_symbols
                if missing_symbols:
                    source_universe = get_source_universe()
                    target_classification_symbols = (source_universe if source_universe is not None
                                                     else set(target_universe))
                else:
                    target_classification_symbols = source_universe_symbols
                missing = classify_missing_symbols(target_universe, partition_symbols,
                                                   target_classification_symbols)
                target_missing_categories = missing
                completion = evaluate_baostock_completion(
                    target_universe, partition_symbols, missing,
                    coverage_threshold=policy["formal_coverage_threshold"],
                    max_source_empty=policy["source_empty_max"])
                if not missing:
                    gap_plan = []
                    eligibility_status = "NO_GAPS"
                elif not should_wait_for_baostock_gap(completion, minimum_coverage):
                    gap_plan = []
                    eligibility_status = "NOT_ELIGIBLE"
                else:
                    gap_plan = [{"symbol": symbol, "start_date": target, "end_date": target,
                                 "trade_dates": [target], "gap_dates": [target],
                                 "watermark": watermarks.get(symbol)}
                                for symbol, category in sorted(missing.items())
                                if category == "SOURCE_EMPTY"]
                    eligibility_status = "ELIGIBLE"
        else:
            if (manifest_path is not None and target_universe is not None and
                    str(partition_manifest.get("provider", "")).lower() == "baostock"):
                from .baostock_completion import (classify_missing_symbols,
                                                  evaluate_baostock_completion,
                                                  should_wait_for_baostock_gap)
                from .registry.daily_kline import BAOSTOCK_COMPLETION_POLICY
                policy = dict(BAOSTOCK_COMPLETION_POLICY)
                policy.update(completion_policy or {})
                target_symbols = {str(row.get("symbol", row.get("code", ""))).zfill(6)
                                  for row in partition_rows}
                if target_universe - target_symbols:
                    source_universe = get_source_universe()
                    target_classification_symbols = (source_universe if source_universe is not None
                                                     else set(target_universe))
                else:
                    target_classification_symbols = source_universe_symbols
                target_missing_categories = classify_missing_symbols(
                    target_universe, target_symbols, target_classification_symbols)
                target_completion = evaluate_baostock_completion(
                    target_universe, target_symbols, target_missing_categories,
                    coverage_threshold=policy["formal_coverage_threshold"],
                    max_source_empty=policy["source_empty_max"])
                if (target_missing_categories and
                        not should_wait_for_baostock_gap(target_completion, minimum_coverage)):
                    gap_plan = []
                    eligibility_status = "NOT_ELIGIBLE"
                else:
                    gap_plan = None
            else:
                gap_plan = None
            if gap_plan is None:
                symbol_gap_dates: dict[str, list[str]] = defaultdict(list)
                for day, cov in coverage.items():
                    if cov.state == "OK":
                        continue
                    # Catalog rows count as present while repairing TD. Otherwise a missing TD version
                    # would turn an existing 5k-row catalog partition into a full-market Baostock request.
                    present = artifact_symbols(day) | td_symbols(day)
                    day_universe = active_symbols_for_day(day)
                    if day_universe is None:
                        raise RuntimeError("DAILY_KLINE_LOCAL_SYMBOL_UNIVERSE_MISSING:" + day)
                    missing_for_day = day_universe - present
                    if day == target:
                        missing_for_day -= {symbol for symbol, category in target_missing_categories.items()
                                             if category == "SOURCE_UNAVAILABLE"}
                    for symbol in missing_for_day:
                        symbol_gap_dates[symbol].append(day)
                # A lagging/new symbol gets one range request per missing symbol, newest date first.
                gap_plan = build_gap_fill_plan(watermarks, target, sessions, depth,
                                               symbols=sorted(symbol_gap_dates),
                                               symbol_gap_dates=symbol_gap_dates)
                eligibility_status = "ELIGIBLE" if gap_plan else "NO_GAPS"
        if gap_plan and range_concurrency == 1:
            range_client.login()
            logged_in = True
        fetched_by_day: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        checkpoint_root = base / ".checkpoints" / f"daily-kline-td-gap-{target}"
        actual_queries = 0
        last_progress_at = time.monotonic()
        if range_concurrency == 1:
            for item in gap_plan:  # already sorted by newest start date first
                if time.monotonic() >= deadline:
                    raise TimeoutError("BAOSTOCK_TOTAL_BUDGET_EXHAUSTED")
                if time.monotonic() - last_progress_at >= no_progress_timeout_seconds:
                    raise TimeoutError("BAOSTOCK_NO_PROGRESS_TIMEOUT")
                symbol, start, end = item["symbol"], item["start_date"], item["end_date"]
                missing_days = [day for day in item["trade_dates"]
                                if symbol not in artifact_symbols(day) and symbol not in td_symbols(day)]
                if not missing_days:
                    continue  # The catalog already has the rows; this range is only a TD rehydrate.
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
                if rows:
                    last_progress_at = time.monotonic()
                for row in rows:
                    fetched_by_day[str(row["date"])][symbol] = {
                        **row, "date": str(row["date"]), "symbol": symbol}
        else:
            parallel_tasks = []
            cached_rows: dict[str, list[dict[str, Any]]] = {}
            for item in gap_plan:
                if time.monotonic() >= deadline:
                    raise TimeoutError("BAOSTOCK_TOTAL_BUDGET_EXHAUSTED")
                symbol, start, end = item["symbol"], item["start_date"], item["end_date"]
                missing_days = [day for day in item["trade_dates"]
                                if symbol not in artifact_symbols(day) and symbol not in td_symbols(day)]
                if not missing_days:
                    continue
                checkpoint = checkpoint_root / f"range-{start}-{end}" / f"{symbol}.json.gz"
                rows = _load_checkpoint(checkpoint, symbol, start, end)
                if rows is not None and not any(str(row.get("date")) == target for row in rows):
                    rows = None
                if rows is None:
                    parallel_tasks.append(item)
                else:
                    cached_rows[symbol] = rows
            if parallel_tasks:
                actual_queries, last_progress_at = _fetch_gap_ranges_parallel(
                    parallel_tasks, checkpoint_root, concurrency=range_concurrency,
                    read_timeout=min(30.0, float(no_progress_timeout_seconds)), deadline=deadline,
                    no_progress_timeout=float(no_progress_timeout_seconds),
                    last_progress_at=last_progress_at)
            for item in gap_plan:
                symbol, start, end = item["symbol"], item["start_date"], item["end_date"]
                rows = cached_rows.get(symbol)
                if rows is None and item in parallel_tasks:
                    rows = _load_checkpoint(checkpoint_root / f"range-{start}-{end}" / f"{symbol}.json.gz",
                                             symbol, start, end)
                for row in rows or []:
                    fetched_by_day[str(row["date"])][symbol] = {
                        **row, "date": str(row["date"]), "symbol": symbol}
    finally:
        module = getattr(range_client, "module", None)
        if not logged_in:
            pass
        elif getattr(range_client, "poisoned", False):
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
        return {"status": eligibility_status, "target": target, "watermark_symbols": len(watermarks),
                "range_symbols": 0, "range_queries": 0, "partitions": [],
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "baostock_logout_error": logout_error,
                "source_universe_status": source_universe_status,
                "source_universe_error": source_universe_error,
                "source_universe_provenance": source_universe_provenance}

    affected_dates = sorted({day for item in gap_plan for day in item["trade_dates"]}, reverse=True)
    runner = JobRunner(artifact_root=base,
                       td_adapter=DailyKlineTdSink(store, base),
                       budget=RequestBudget(max_requests=max(1, len(gap_plan)), min_interval_seconds=0))
    outcomes = []
    for day in affected_dates:
        versions = store.read_versions(day)
        baostock_versions = {str(item.get("version")) for item in versions
                             if str(item.get("provider", "")).lower() == "baostock"}
        stored = store.read_day_rows(day) if baostock_versions else {}
        existing = _td_rows_to_daily(day, {symbol: row for symbol, row in stored.items()
                                           if str(row.get("version") or "") in baostock_versions})
        day_manifest = catalog.locate("daily_kline", day)
        artifact_rows = (catalog.read_rows(day_manifest)
                         if day_manifest is not None and day == target else [])
        artifact_existing = {str(row.get("symbol", row.get("code", ""))).zfill(6):
                             {**row, "symbol": str(row.get("symbol", row.get("code", ""))).zfill(6),
                              "date": day} for row in artifact_rows}
        merged = {**artifact_existing, **existing, **fetched_by_day.get(day, {})}
        if not merged:
            continue
        rows = sorted(merged.values(), key=lambda row: row["symbol"])
        completeness = {field: ("COMPLETE" if all(row.get(field) is not None for row in rows)
                                else "SUSPENDED_ONLY" if all(row.get("suspended") for row in rows)
                                else "PARTIAL") for field in _QUALITY_FIELDS}
        day_expected_symbols = active_symbols_for_day(day)
        if day_expected_symbols is None:
            raise RuntimeError("DAILY_KLINE_LOCAL_SYMBOL_UNIVERSE_MISSING:" + day)
        day_expected_count = len(day_expected_symbols)
        meta = {"rows": rows, "source": "baostock", "provider": "baostock",
                "expected_count": day_expected_count, "field_completeness": completeness,
                "universe_trade_date": universe_source_day_by_day[day],
                "provider_attempts": [{"provider": "baostock", "result": "ok",
                                       "reason": "td_watermark_range_fill"}],
                "available_at": datetime.now(SHANGHAI).isoformat(),
                "as_of": day, "request_count": len([x for x in gap_plan if day in x["trade_dates"]])}
        if day == target:
            classified_source_symbols = (source_universe_symbols if source_universe_status == "READY"
                                         else target_classification_symbols)
            meta.update({"expected_symbols": sorted(day_expected_symbols),
                         "source_symbols": sorted(classified_source_symbols)
                         if classified_source_symbols is not None else None,
                         "source_universe_status": source_universe_status,
                         "source_universe_error": source_universe_error,
                         "source_universe_provenance": source_universe_provenance})
        result = runner.run("daily_kline", day, fetch=lambda _day: meta,
                            td_write=True, source="baostock", expected_count=day_expected_count,
                            baostock_policy=completion_policy if day == target else None)
        outcome = {"trade_date": day, "row_count": len(result.rows), "version": result.manifest["version"],
                   "sha256": result.manifest["sha256"], "td_publish": result.manifest.get("td_publish")}
        if day == target:
            quality = dict(result.manifest.get("quality") or {})
            business_status = result.manifest.get("business_status")
            from .baostock_completion import classify_missing_symbols, evaluate_baostock_completion
            from .registry.daily_kline import BAOSTOCK_COMPLETION_POLICY
            policy = dict(BAOSTOCK_COMPLETION_POLICY)
            policy.update(completion_policy or {})
            bar_symbols = {str(row.get("symbol", row.get("code", ""))).zfill(6)
                           for row in result.rows}
            missing = classify_missing_symbols(day_expected_symbols, bar_symbols,
                                               target_classification_symbols)
            completion = evaluate_baostock_completion(
                day_expected_symbols, bar_symbols, missing,
                coverage_threshold=policy["formal_coverage_threshold"],
                max_source_empty=policy["source_empty_max"])
            business_status = completion["business_status"]
            quality.update(completion["quality"])
            outcome.update({"business_status": business_status, "quality": quality})
        outcomes.append(outcome)
    if target not in {str(item["trade_date"]) for item in outcomes}:
        raise RuntimeError("BAOSTOCK_GAP_UNRESOLVED:target_partition_missing:" + target)
    target_result = next(item for item in outcomes if str(item["trade_date"]) == target)
    complete = target_result.get("business_status") == "FORMAL" and target_result.get("td_publish") == "READY"
    quality = target_result.get("quality") or {}
    def count_quality(name: str) -> int:
        return int(quality.get(name) or 0)
    return {"status": "OK" if complete else "WAITING", "target": target, "watermark_symbols": len(watermarks),
            "range_symbols": len(gap_plan), "range_queries": actual_queries, "partitions": outcomes,
            "remaining_missing": count_quality("missing"),
            "remaining_source_empty": count_quality("source_empty_count"),
            "remaining_unexpected": count_quality("unexpected_count"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "baostock_logout_error": logout_error,
            "source_universe_status": source_universe_status,
            "source_universe_error": source_universe_error,
            "source_universe_provenance": source_universe_provenance}
