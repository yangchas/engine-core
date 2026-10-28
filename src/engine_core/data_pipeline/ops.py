"""Offline-safe orchestration helpers for Core data jobs."""
from __future__ import annotations

import fcntl
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Callable, Iterable, Mapping

from .jobs import JobRunner, RequestBudget
from .registry import DATASETS, get_dataset
from .storage import ArtifactCatalog, _atomic_write, canonical_json
from ..limit_price import limit_percent
from ..live_session import append_auction_amount_baseline
from .trade_calendar import TradeCalendarStore


def derive_stock_day_facts(day: str, source_rows: Iterable[Mapping[str, Any]],
                           metadata: Mapping[str, Any] | None = None
                           ) -> tuple[list[dict[str, Any]], str]:
    """Derive close-price proxy facts from daily bars without side effects."""
    metadata = metadata or {}
    facts = []
    for source_row in source_rows:
        symbol = str(source_row.get("symbol", "")).zfill(6)
        if not symbol.strip("0"):
            continue
        fact = {"symbol": symbol, "trade_date": day, "code": source_row.get("code"),
                "universe_version": source_row.get("universe_version"),
                "close": source_row.get("close"), "pre_close": source_row.get("pre_close"),
                "pct_chg": source_row.get("pct_chg"),
                "amount_yuan": source_row.get("amount_yuan", source_row.get("amount")),
                "volume_shares": source_row.get("volume"),
                "fact_source": "daily_kline", "quality": "CLOSE_PRICE_PROXY"}
        if metadata.get("source_version") is not None:
            fact["source_version"] = metadata["source_version"]
        facts.append(fact)
    return facts, "CLOSE_PRICE_PROXY"


class FileLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise RuntimeError("job_already_running") from None

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


def is_trading_day(value: str, trading_dates: Iterable[str] | None = None) -> bool:
    if trading_dates is not None:
        return value in set(trading_dates)
    from .trade_calendar import TradeCalendarStore
    try:
        return TradeCalendarStore().is_trading_day(value)
    except ValueError:
        return False


def retry_window_for(dataset: str, local_time: str) -> int | None:
    if dataset != "daily_kline":
        return 0
    hour, minute = (int(part) for part in local_time.split(":", 1))
    current = hour * 60 + minute
    slots = (17 * 60 + 40, 18 * 60 + 30, 20 * 60)
    return slots.index(current) if current in slots else None


_PHASE_JOBS = {
    "premarket": ("symbol_universe", "plate_members", "limit_up_pool", "limit_reasons", "hot_plates"),
    "intraday": ("hot_plates",),
    "postmarket": ("daily_kline", "ladder_daily", "stock_day_facts"),
}


def run_derived_daily(root: str | Path, day: str, errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build daily_indicators then daily_report_features for ``day`` when their upstream is ready."""
    from ..daily_history import materialize_daily_indicators, materialize_daily_report_features
    from .derived_daily import UpstreamNotReady
    catalog = ArtifactCatalog(root)
    out: list[dict[str, Any]] = []
    for name, build in (("daily_indicators", materialize_daily_indicators),
                        ("daily_report_features", materialize_daily_report_features)):
        try:
            result = build(catalog, trade_date=day)
            manifest = result.get("manifest") or result
            out.append({"job": name, "status": result["status"], "version": manifest.get("version"),
                        "business_status": manifest.get("business_status"),
                        "row_count": manifest.get("row_count")})
        except UpstreamNotReady as exc:
            out.append({"job": name, "status": "SKIPPED_UPSTREAM_NOT_READY", "reason": exc.code})
            break
        except Exception as exc:
            errors.append({"job": name, "status": "FAILED", "error": str(exc)[:120]})
            break
    return out


def run_daily_phase(phase: str, trade_date: str, *, root: str | Path,
                    adapters: Mapping[str, Callable[..., Any]], trading_dates: Iterable[str] | None,
                    write: bool, redis: Any = None, td_adapter: Any = None,
                    td_write: bool = False, now: str | None = None,
                    lock: bool = True) -> dict[str, Any]:
    if phase not in _PHASE_JOBS:
        raise ValueError("unknown_phase")
    root = Path(root)
    status_path = root / "daily_status.json"
    try:
        calendar = TradeCalendarStore(root)
        calendar_is_trading = calendar.is_trading_day(trade_date)
    except ValueError:
        result = {"phase": phase, "trade_date": trade_date,
                  "status": "FAILED_CALENDAR_UNAVAILABLE", "jobs": [], "errors": []}
        if write: _atomic_write(status_path, canonical_json(result) + b"\n")
        return result
    if not calendar_is_trading:
        result = {"phase": phase, "trade_date": trade_date, "status": "SKIPPED_NON_TRADING_DAY",
                  "jobs": []}
        if write:
            _atomic_write(status_path, canonical_json(result) + b"\n")
        return result
    lock_context = FileLock(root / ".locks" / f"daily-{phase}.lock") if lock else _NullContext()
    results = []
    errors = []
    job_trade_date = trade_date
    if phase == "premarket":
        try:
            job_trade_date = calendar.previous_trade_date(trade_date).isoformat()
        except ValueError:
            result = {"phase": phase, "trade_date": trade_date,
                      "status": "FAILED_CALENDAR_UNAVAILABLE", "jobs": [], "errors": []}
            if write: _atomic_write(status_path, canonical_json(result) + b"\n")
            return result
    with lock_context:
        runner = JobRunner(artifact_root=root, redis=redis, td_adapter=td_adapter,
                           budget=RequestBudget(max_requests=100, min_interval_seconds=0))
        for name in _PHASE_JOBS[phase]:
            if name not in DATASETS:
                errors.append({"job": name, "status": "NOT_REGISTERED"})
                continue
            dataset_day = trade_date if name == "symbol_universe" else job_trade_date
            existing_path = runner.catalog.locate(name, dataset_day)
            if existing_path is not None and name != "hot_plates":
                try:
                    existing_manifest = json.loads(existing_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    existing_manifest = {}
                is_formal_daily_bar = (name == "daily_kline" and
                                       existing_manifest.get("business_status") == "FORMAL")
                if name != "daily_kline" or is_formal_daily_bar:
                    results.append({"job": name, "status": "SKIPPED_ALREADY_PUBLISHED",
                                    "version": existing_manifest.get("version"),
                                    "row_count": existing_manifest.get("row_count")})
                    continue
            fetch = adapters.get(name)
            if name == "ladder_daily" and fetch is None and runner.catalog.locate("daily_kline", job_trade_date):
                try:
                    derived = backfill_ladder_daily(runner, job_trade_date, job_trade_date, td_write=bool(write and td_write))
                    results.append({"job": name, "status": derived["status"],
                                    "row_count": derived.get("row_count"),
                                    "reference_comparison": derived.get("reference_comparison")})
                except Exception as exc:
                    from .td_sink import TdUnavailable, TdVerifyError
                    error_code = ("TD_UNAVAILABLE" if isinstance(exc, TdUnavailable) else
                                  "TD_VERIFY_FAILED" if isinstance(exc, TdVerifyError) else None)
                    errors.append({"job": name, "status": "FAILED", "error": str(exc)[:120],
                                   **({"error_code": error_code} if error_code else {})})
                continue
            if name == "stock_day_facts" and fetch is None:
                def fetch_stock_day_facts(day: str) -> dict[str, Any]:
                    manifest_path = runner.catalog.locate("daily_kline", day)
                    if manifest_path is None: raise RuntimeError("daily_kline_artifact_required")
                    source_rows = runner.catalog.read_rows(manifest_path)
                    facts, _quality = derive_stock_day_facts(day, source_rows)
                    return {"rows": facts, "expected_count": len(facts),
                            "partial": not bool(facts)}
                fetch = fetch_stock_day_facts
            if fetch is None:
                errors.append({"job": name, "status": "FETCHER_NOT_CONFIGURED"})
                continue
            try:
                job = runner.run(name, dataset_day, fetch=fetch, now=now,
                                 publish=bool(write and redis is not None),
                                 td_write=bool(td_write and name in {"daily_kline", "auction_baseline"}),
                                 dry_run=not write)
                results.append({"job": name, "status": job.manifest["quality"]["status"],
                                "version": job.manifest.get("version"),
                                "row_count": job.manifest["row_count"],
                                "sha256": job.manifest.get("sha256")})
            except Exception as exc:
                from .td_sink import TdUnavailable, TdVerifyError
                error_code = ("TD_UNAVAILABLE" if isinstance(exc, TdUnavailable) else
                              "TD_VERIFY_FAILED" if isinstance(exc, TdVerifyError) else None)
                errors.append({"job": name, "status": "FAILED", "error": str(exc)[:120],
                               **({"error_code": error_code} if error_code else {})})
        if phase == "postmarket" and write:
            # C571: the derived daily datasets are produced as soon as the gated daily_kline of this day is
            # present (any quality >= PROVISIONAL), by the same event that published it; a later FORMAL upgrade
            # reruns this phase (probe events / reconcile) and the stale-input check rebuilds a new version.
            results.extend(run_derived_daily(root, job_trade_date, errors))
        baseline_append = None
        amount_provider = adapters.get("auction_amount_provider")
        if phase == "postmarket" and amount_provider is not None:
            try:
                observed = amount_provider(trade_date)
                observed = dict(observed) if isinstance(observed, Mapping) else {}
                baseline_dates = runner.catalog.read().get("datasets", {}).get(
                    "auction_baseline", {}).get("dates", {})
                prior_dates = sorted(day for day in baseline_dates if day < trade_date)
                previous_rows = []
                if prior_dates:
                    path = runner.catalog.locate("auction_baseline", prior_dates[-1])
                    if path is not None:
                        previous_rows = runner.catalog.read_rows(path)
                declared_available_at = observed.get("available_at")
                captured_at = observed.get("captured_at") or now
                updated = append_auction_amount_baseline(
                    previous_rows, trade_date=trade_date,
                    amount_yuan=observed.get("amount_yuan"),
                    available_at=declared_available_at or captured_at or datetime.now().astimezone().isoformat())
                committed = None
                if write and observed.get("amount_yuan") is not None:
                    manifest = {"trade_date": trade_date, "as_of": observed.get("as_of", "UNKNOWN"),
                        "available_at": declared_available_at, "captured_at": captured_at,
                        "source_asof": observed.get("as_of", "UNKNOWN"), "point_in_time_date": trade_date,
                        "provenance": "postmarket_auction_summary",
                        "source": observed.get("source", "injected_auction_provider"),
                        "row_count": len(updated), "status": "READY"}
                    committed = runner.catalog.commit("auction_baseline", trade_date, updated,
                        manifest, source="core_derived")
                baseline_append = {"status": "READY" if observed.get("amount_yuan") is not None else "PARTIAL",
                    "row_count": len(updated), "version": committed.get("version") if committed else None,
                    "available_at": committed.get("available_at") if committed else declared_available_at,
                    "missing": None if observed.get("amount_yuan") is not None else "AUCTION_AMOUNT_MISSING"}
            except Exception as exc:
                errors.append({"job": "auction_baseline_append", "status": "FAILED",
                               "error": str(exc)[:120]})
                baseline_append = {"status": "FAILED", "error": str(exc)[:120]}
        result = {"phase": phase, "trade_date": trade_date,
                  "status": "FAILED" if errors else "READY", "jobs": results, "errors": errors}
        if any(item.get("error_code") == "TD_UNAVAILABLE" for item in errors):
            result["error_code"], result["retryable"] = "TD_UNAVAILABLE", True
        elif any(item.get("error_code") == "TD_VERIFY_FAILED" for item in errors):
            result["error_code"], result["retryable"] = "TD_VERIFY_FAILED", False
        if baseline_append is not None:
            result["auction_baseline_append"] = baseline_append
        if write:
            _atomic_write(status_path, canonical_json(result) + b"\n")
            if redis is not None:
                key = "core:cache:data:status"
                previous = redis.get(key)
                try:
                    if isinstance(previous, bytes): previous = previous.decode("utf-8")
                    status = json.loads(previous) if previous else {}
                    if not isinstance(status, dict): status = {}
                except (TypeError, ValueError): status = {}
                status[f"phase:{phase}"] = {"trade_date": trade_date, "status": result["status"],
                                            "jobs": results, "errors": errors}
                redis.set(key, canonical_json(dict(list(status.items())[-32:])).decode("utf-8"), ex=7 * 86400)
    return result


class _NullContext:
    def __enter__(self): return self
    def __exit__(self, *_exc): return False


def backfill_daily_kline(runner: JobRunner, start: str, end: str, *, fetch: Callable[[str], Any],
                         max_requests: int | None = None, write: bool = False,
                         td_reader: Any = None, ladder_reference: str | Path | None = None) -> dict[str, Any]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise ValueError("backfill_date_range_invalid")
    days = [(first + timedelta(days=offset)).isoformat()
            for offset in range((last - first).days + 1)]
    skipped, processed, reports = [], [], []
    for day in days:
        if max_requests is not None and len(processed) >= max_requests:
            break
        existing = runner.catalog.locate("daily_kline", day)
        if existing is not None:
            skipped.append(day)
            continue
        job = runner.run("daily_kline", day, fetch=fetch, publish=False, dry_run=not write)
        processed.append(day)
        if td_reader is not None:
            legacy = td_reader(day)
            reports.append({"trade_date": day,
                            **reconcile_daily_rows(job.rows, legacy)})
    ladder_report = None
    if ladder_reference:
        ladder_report = compare_ladder_reference(runner.catalog, ladder_reference)
    return {"dataset": "daily_kline", "from": start, "to": end, "processed": processed,
            "skipped_existing": skipped, "remaining": [day for day in days if day not in skipped + processed],
            "td_reconciliation": reports, "ladder_reconciliation": ladder_report}


def backfill_ladder_daily(runner: JobRunner, start: str, end: str, *, td_write: bool = False,
                          reference_path: str | Path | None = None, config: Mapping[str, Any] | None = None,
                          **materialize_kwargs: Any) -> dict[str, Any]:
    """C577: derive ladder_daily per day with the stateless ladder_v1 builder (docs/LADDER_DAILY.md).

    The pre-C577 relay accumulation (streak carried over from the previous ladder partition, close-price proxy)
    is gone; every day is recomputed from its own window of per-day limit-up sets."""
    from ..ladder_daily_adapter import materialize_ladder_daily
    from .derived_daily import UpstreamNotReady
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first: raise ValueError("backfill_date_range_invalid")
    days = [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]
    processed, skipped, rows, td_outcomes = [], [], 0, []
    for day in days:
        on_committed = None
        if td_write:
            from ..guardian.daily_kline_coverage import td_settings
            from .td_ladder_sink import LadderTdSink, TaosLadderStore
            from .td_sink import TdUnavailable, TdVerifyError
            settings = td_settings()
            if settings is None or not os.environ.get("TD_CONFIG"):
                raise TdUnavailable("td_sink_not_configured")

            def on_committed(commit_day, commit_rows, manifest):
                store = TaosLadderStore(str(settings["database"]))
                try:
                    outcome = LadderTdSink(store, runner.root).write_partition(commit_day, commit_rows, manifest)
                finally:
                    store.close()
                if outcome.status not in {"synced", "unchanged", "skipped_lower"}:
                    raise TdVerifyError("td_ladder_write_failed:" + commit_day + ":" + outcome.status)
                return outcome.as_dict()

        try:
            result = materialize_ladder_daily(runner.catalog, trade_date=day, config=config,
                                              on_committed=on_committed, **materialize_kwargs)
        except (UpstreamNotReady, KeyError):
            skipped.append(day)   # non-trading day or no limit-up set obtainable: nothing derived, nothing invented
            continue
        if td_write and result.get("td_write"):
            td_outcomes.append(result["td_write"])
        if result["status"] in {"COMMITTED", "UP_TO_DATE"}:
            processed.append(day)
            rows += int(result.get("row_count") or 0)
    comparison = compare_ladder_reference(runner.catalog, reference_path) if reference_path else None
    return {"dataset": "ladder_daily", "from": start, "to": end, "processed": processed, "row_count": rows,
            "skipped": skipped, "status": "LADDER_V1", "td_outcomes": td_outcomes,
            "reference_comparison": comparison}


def reconcile_daily_rows(core_rows: Iterable[Mapping[str, Any]],
                         reference_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    def keyed(rows):
        result = {}
        for row in rows:
            symbol = str(row.get("symbol", row.get("code", ""))).zfill(6)
            if symbol.strip("0"):
                result[symbol] = row
        return result
    core, reference = keyed(core_rows), keyed(reference_rows)
    mismatches = []
    for symbol in sorted(core.keys() & reference.keys()):
        fields = []
        for field in ("close", "amount"):
            left, right = core[symbol].get(field), reference[symbol].get(field)
            if left is None or right is None:
                if left != right: fields.append(field)
            else:
                try:
                    if abs(float(left) - float(right)) > max(0.01, abs(float(right)) * 1e-6):
                        fields.append(field)
                except (TypeError, ValueError):
                    if left != right: fields.append(field)
        if fields: mismatches.append({"symbol": symbol, "fields": fields})
    return {"core_rows": len(core), "reference_rows": len(reference),
            "core_only": sorted(core.keys() - reference.keys()),
            "reference_only": sorted(reference.keys() - core.keys()),
            "mismatch_count": len(mismatches), "mismatches": mismatches}


def compare_ladder_reference(catalog: ArtifactCatalog, reference_path: str | Path) -> dict[str, Any]:
    path = Path(reference_path)
    if not path.is_file():
        return {"status": "REFERENCE_MISSING", "path": str(path)}
    import csv
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reference = list(csv.DictReader(handle))
    expected = {row.get("date"): row for row in reference}
    mismatches = []
    compared = 0
    for day in expected:
        manifest_path = catalog.locate("ladder_daily", day)
        if manifest_path is None: continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = catalog.read_rows(manifest_path)
        actual = {}
        for item in rows:
            if item.get("limit_up"):
                height = min(int(item.get("streak_n", 0)), 11)
                actual[height] = actual.get(height, 0) + 1
        row = expected[day]
        fields = []
        for height in range(1, 11):
            field = f"board_{height}_n"
            try: wanted = int(float(row.get(field) or 0))
            except (TypeError, ValueError): continue
            if actual.get(height, 0) != wanted: fields.append(field)
        if fields: mismatches.append({"trade_date": day, "fields": fields})
        compared += 1
    return {"status": "PARTIAL_ST_FILTER_UNAVAILABLE", "reference_rows": len(reference),
            "compared_days": compared, "mismatch_days": len(mismatches),
            "mismatches": mismatches, "study_source": str(path)}


def dataset_health(root: str | Path, *, redis: Any = None,
                   trade_dates: Iterable[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    root = Path(root)
    catalog = ArtifactCatalog(root)
    dates = list(trade_dates or ())
    current = now or datetime.now().astimezone()
    datasets = {}
    overall = "GREEN"
    for name, spec in DATASETS.items():
        manifest_path = catalog.locate(name)
        manifest = None
        if manifest_path and manifest_path.is_file():
            try: manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError): manifest = None
        row = {"latest_success_date": manifest.get("trade_date") if manifest else None,
               "coverage": (manifest.get("quality") or {}).get("coverage") if manifest else None,
               "provisional": bool(manifest and manifest.get("business_status") == "PROVISIONAL"),
               "artifact_sha256": manifest.get("sha256") if manifest else None,
               "row_count": manifest.get("row_count") if manifest else None,
               "ttl_remaining_seconds": None, "artifact_redis_consistent": None}
        if manifest and dates:
            try: row["trading_day_lag"] = max(0, dates.index(dates[-1]) - dates.index(manifest["trade_date"]))
            except ValueError: row["trading_day_lag"] = None
        if redis is not None:
            pointer_raw = redis.get(f"core:cache:data:{name}:latest")
            if isinstance(pointer_raw, bytes): pointer_raw = pointer_raw.decode("utf-8")
            try: pointer = json.loads(pointer_raw) if pointer_raw else None
            except (TypeError, ValueError): pointer = None
            if pointer and manifest:
                redis_raw = redis.get(f"core:cache:data:{name}:{pointer.get('trade_date')}:{pointer.get('version')}")
                if isinstance(redis_raw, bytes): redis_raw = redis_raw.decode("utf-8")
                try: redis_manifest = json.loads(redis_raw) if redis_raw else None
                except (TypeError, ValueError): redis_manifest = None
                row["artifact_redis_consistent"] = bool(redis_manifest and
                    redis_manifest.get("sha256") == manifest.get("sha256") and
                    redis_manifest.get("row_count") == manifest.get("row_count"))
            elif manifest:
                row["artifact_redis_consistent"] = False
            ttl_fn = getattr(redis, "ttl", None)
            if callable(ttl_fn):
                try: row["ttl_remaining_seconds"] = ttl_fn(f"core:cache:data:{name}:latest")
                except Exception: pass
        status = "GREEN"
        if manifest is None:
            status = "RED"
        elif row["provisional"] or (row.get("coverage") is not None and row["coverage"] < 1):
            status = "YELLOW"
        if redis is not None and row["artifact_redis_consistent"] is False:
            status = "RED"
        row["status"] = status
        datasets[name] = row
        if status == "RED": overall = "RED"
        elif status == "YELLOW" and overall == "GREEN": overall = "YELLOW"
    return {"summary": overall, "checked_at": current.isoformat(), "datasets": datasets}
