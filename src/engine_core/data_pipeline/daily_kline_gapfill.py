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
    status_index = fields.index("tradeStatus") if "tradeStatus" in fields else None
    symbols = set()
    while result.next():
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
                               no_progress_timeout_seconds: float = 300.0,
                               client: Any = None, only_source_empty: bool = False,
                               minimum_coverage: float = 0.90,
                               completion_policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Fetch one interval per lagging symbol, merge TD rows by day, and commit catalog+TD in one task."""
    from ..daily_history import BaostockRangeClient
    from ..guardian.daily_kline_coverage import build_gap_fill_plan
    from .trade_calendar import TradeCalendarStore
    from .jobs import JobRunner, RequestBudget
    from .td_sink import DailyKlineTdSink, TdUnavailable

    if total_timeout_seconds <= 0:
        raise ValueError("total_timeout_seconds_must_be_positive")
    if no_progress_timeout_seconds <= 0:
        raise ValueError("no_progress_timeout_seconds_must_be_positive")
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
        expected_symbols: set[str] = set()
        expected_symbols_by_day: dict[str, set[str] | None] = {}

        def active_symbols_for_day(day: str) -> set[str] | None:
            if day not in expected_symbols_by_day:
                universe_path = catalog.locate("symbol_universe", day)
                if universe_path is None:
                    expected_symbols_by_day[day] = None
                else:
                    rows = catalog.read_rows(universe_path)
                    expected_symbols_by_day[day] = {
                        str(row.get("symbol", row.get("code", ""))).zfill(6)
                        for row in rows if str(row.get("trade_status", "")).strip() != "0"
                    }
            return expected_symbols_by_day[day]

        if only_source_empty:
            universe_path = catalog.locate("symbol_universe", target)
            if (manifest_path is None or universe_path is None or
                    str(partition_manifest.get("provider", "")).lower() != "baostock"):
                gap_plan = []
                eligibility_status = "NOT_ELIGIBLE"
            else:
                expected_symbols = active_symbols_for_day(target) or set()
                partition_symbols = {str(row.get("symbol", row.get("code", ""))).zfill(6)
                                     for row in partition_rows}
                from .baostock_completion import (classify_missing_symbols,
                                                  evaluate_baostock_completion,
                                                  should_wait_for_baostock_gap)
                policy = dict(completion_policy or {})
                if "formal_coverage_threshold" not in policy or "source_empty_max" not in policy:
                    raise ValueError("baostock_gapfill_completion_policy_required")
                missing = classify_missing_symbols(expected_symbols, partition_symbols, universe)
                completion = evaluate_baostock_completion(
                    expected_symbols, partition_symbols, missing,
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
                                if category == "SOURCE_EMPTY" and symbol in universe]
                    eligibility_status = "ELIGIBLE"
        else:
            target_universe = active_symbols_for_day(target)
            if manifest_path is not None and target_universe is not None and partition_manifest.get("provider") == "baostock":
                from .baostock_completion import (classify_missing_symbols,
                                                  evaluate_baostock_completion,
                                                  should_wait_for_baostock_gap)
                from .registry.daily_kline import BAOSTOCK_COMPLETION_POLICY
                policy = dict(BAOSTOCK_COMPLETION_POLICY)
                policy.update(completion_policy or {})
                target_symbols = {str(row.get("symbol", row.get("code", ""))).zfill(6)
                                  for row in partition_rows}
                target_missing = classify_missing_symbols(target_universe, target_symbols, universe)
                target_completion = evaluate_baostock_completion(
                    target_universe, target_symbols, target_missing,
                    coverage_threshold=policy["formal_coverage_threshold"],
                    max_source_empty=policy["source_empty_max"])
                if target_missing and not should_wait_for_baostock_gap(target_completion, minimum_coverage):
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
                    # query_all_stock may include delisted/index/other symbols outside the catalog's
                    # date-specific active universe. Only request gaps that the day actually expects.
                    day_universe = active_symbols_for_day(day)
                    expected_for_day = day_universe if day_universe is not None else set(universe)
                    for symbol in (expected_for_day & set(universe)) - present:
                        symbol_gap_dates[symbol].append(day)
                # A lagging/new symbol gets one range request per missing symbol, newest date first.
                gap_plan = build_gap_fill_plan(watermarks, target, sessions, depth,
                                               symbols=sorted(symbol_gap_dates),
                                               symbol_gap_dates=symbol_gap_dates)
                eligibility_status = "ELIGIBLE" if gap_plan else "NO_GAPS"
        fetched_by_day: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        checkpoint_root = base / ".checkpoints" / f"daily-kline-td-gap-{target}"
        actual_queries = 0
        last_progress_at = time.monotonic()
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
        return {"status": eligibility_status, "target": target, "watermark_symbols": len(watermarks),
                "range_symbols": 0, "range_queries": 0, "partitions": [],
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "baostock_logout_error": logout_error}

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
        day_expected_count = len(day_expected_symbols) if day_expected_symbols is not None else len(universe)
        meta = {"rows": rows, "source": "baostock", "provider": "baostock",
                "expected_count": day_expected_count, "field_completeness": completeness,
                "provider_attempts": [{"provider": "baostock", "result": "ok",
                                       "reason": "td_watermark_range_fill"}],
                "available_at": datetime.now(SHANGHAI).isoformat(),
                "as_of": day, "request_count": len([x for x in gap_plan if day in x["trade_dates"]])}
        if day == target and day_expected_symbols is not None:
            meta.update({"expected_symbols": sorted(day_expected_symbols),
                         "source_symbols": universe})
        result = runner.run("daily_kline", day, fetch=lambda _day: meta,
                            td_write=True, source="baostock", expected_count=day_expected_count,
                            baostock_policy=completion_policy if day == target else None)
        outcome = {"trade_date": day, "row_count": len(result.rows), "version": result.manifest["version"],
                   "sha256": result.manifest["sha256"], "td_publish": result.manifest.get("td_publish")}
        if day == target:
            quality = dict(result.manifest.get("quality") or {})
            business_status = result.manifest.get("business_status")
            if day_expected_symbols is not None:
                from .baostock_completion import classify_missing_symbols, evaluate_baostock_completion
                from .registry.daily_kline import BAOSTOCK_COMPLETION_POLICY
                policy = dict(BAOSTOCK_COMPLETION_POLICY)
                policy.update(completion_policy or {})
                bar_symbols = {str(row.get("symbol", row.get("code", ""))).zfill(6)
                               for row in result.rows}
                missing = classify_missing_symbols(day_expected_symbols, bar_symbols, universe)
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
            "baostock_logout_error": logout_error}
