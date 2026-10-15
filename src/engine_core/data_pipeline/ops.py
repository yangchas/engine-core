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
    day = date.fromisoformat(value)
    if trading_dates is not None:
        return value in set(trading_dates)
    return day.weekday() < 5


def retry_window_for(dataset: str, local_time: str) -> int | None:
    if dataset != "daily_kline":
        return 0
    hour, minute = (int(part) for part in local_time.split(":", 1))
    current = hour * 60 + minute
    slots = (17 * 60 + 40, 18 * 60 + 30, 20 * 60)
    return slots.index(current) if current in slots else None


_PHASE_JOBS = {
    "premarket": ("plate_members", "limit_up_pool", "limit_reasons"),
    "intraday": ("hot_plates",),
    "postmarket": ("daily_kline", "ladder_daily", "auction_baseline", "stock_day_facts"),
}


def run_daily_phase(phase: str, trade_date: str, *, root: str | Path,
                    adapters: Mapping[str, Callable[..., Any]], trading_dates: Iterable[str] | None,
                    write: bool, redis: Any = None, td_adapter: Any = None,
                    td_write: bool = False, now: str | None = None,
                    lock: bool = True) -> dict[str, Any]:
    if phase not in _PHASE_JOBS:
        raise ValueError("unknown_phase")
    root = Path(root)
    status_path = root / "daily_status.json"
    if trading_dates is None:
        calendar = root / "trade_dates.json"
        if calendar.is_file():
            try: trading_dates = json.loads(calendar.read_text(encoding="utf-8"))
            except (OSError, ValueError): trading_dates = None
        if trading_dates is None:
            result = {"phase": phase, "trade_date": trade_date,
                      "status": "FAILED_CALENDAR_UNAVAILABLE", "jobs": [], "errors": []}
            if write: _atomic_write(status_path, canonical_json(result) + b"\n")
            return result
    if not is_trading_day(trade_date, trading_dates):
        result = {"phase": phase, "trade_date": trade_date, "status": "SKIPPED_NON_TRADING_DAY",
                  "jobs": []}
        if write:
            _atomic_write(status_path, canonical_json(result) + b"\n")
        return result
    lock_context = FileLock(root / ".locks" / f"daily-{phase}.lock") if lock else _NullContext()
    results = []
    errors = []
    with lock_context:
        runner = JobRunner(artifact_root=root, redis=redis, td_adapter=td_adapter,
                           budget=RequestBudget(max_requests=100, min_interval_seconds=0))
        for name in _PHASE_JOBS[phase]:
            if name not in DATASETS:
                errors.append({"job": name, "status": "NOT_REGISTERED"})
                continue
            existing_path = runner.catalog.locate(name, trade_date)
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
            if name == "ladder_daily" and fetch is None and runner.catalog.locate("daily_kline", trade_date):
                try:
                    derived = backfill_ladder_daily(runner, trade_date, trade_date)
                    results.append({"job": name, "status": derived["status"],
                                    "row_count": derived.get("row_count"),
                                    "reference_comparison": derived.get("reference_comparison")})
                except Exception as exc:
                    errors.append({"job": name, "status": "FAILED", "error": str(exc)[:120]})
                continue
            if name == "stock_day_facts" and fetch is None:
                def fetch_stock_day_facts(day: str) -> dict[str, Any]:
                    manifest_path = runner.catalog.locate("daily_kline", day)
                    if manifest_path is None: raise RuntimeError("daily_kline_artifact_required")
                    source_rows = runner.catalog.read_rows(manifest_path)
                    facts = []
                    for source_row in source_rows:
                        symbol = str(source_row.get("symbol", "")).zfill(6)
                        if not symbol.strip("0"): continue
                        facts.append({"symbol": symbol, "trade_date": day,
                            "close": source_row.get("close"), "pre_close": source_row.get("pre_close"),
                            "pct_chg": source_row.get("pct_chg"),
                            "amount_yuan": source_row.get("amount_yuan", source_row.get("amount")),
                            "volume_shares": source_row.get("volume"),
                            "fact_source": "daily_kline", "quality": "CLOSE_PRICE_PROXY"})
                    return {"rows": facts, "expected_count": len(facts),
                            "partial": not bool(facts)}
                fetch = fetch_stock_day_facts
            if fetch is None:
                errors.append({"job": name, "status": "FETCHER_NOT_CONFIGURED"})
                continue
            try:
                job = runner.run(name, trade_date, fetch=fetch, now=now,
                                 publish=bool(write and redis is not None),
                                 td_write=bool(td_write and name in {"daily_kline", "auction_baseline"}),
                                 dry_run=not write)
                results.append({"job": name, "status": job.manifest["quality"]["status"],
                                "version": job.manifest.get("version"),
                                "row_count": job.manifest["row_count"],
                                "sha256": job.manifest.get("sha256")})
            except Exception as exc:
                errors.append({"job": name, "status": "FAILED", "error": str(exc)[:120]})
        result = {"phase": phase, "trade_date": trade_date,
                  "status": "FAILED" if errors else "READY", "jobs": results, "errors": errors}
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


def backfill_ladder_daily(runner: JobRunner, start: str, end: str, *,
                          reference_path: str | Path | None = None) -> dict[str, Any]:
    """Derive only the study's close-price proxy; never claim seal/order truth."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first: raise ValueError("backfill_date_range_invalid")
    days = [(first + timedelta(days=offset)).isoformat()
            for offset in range((last - first).days + 1)]
    catalog = runner.catalog
    source_rows = {}
    missing = []
    for day in days:
        manifest_path = catalog.locate("daily_kline", day)
        if manifest_path is None:
            missing.append(day)
            continue
        source_rows[day] = catalog.read_rows(manifest_path)
    symbols = sorted({str(row.get("symbol", "")).zfill(6) for rows in source_rows.values()
                      for row in rows if row.get("symbol")})
    streak = {symbol: 0 for symbol in symbols}
    catalog_data = catalog.read().get("datasets", {}).get("ladder_daily", {}).get("dates", {})
    previous_days = sorted(day for day in catalog_data if day < start)
    if previous_days:
        previous_manifest = catalog.locate("ladder_daily", previous_days[-1])
        if previous_manifest:
            for prior in catalog.read_rows(previous_manifest):
                symbol = str(prior.get("symbol", "")).zfill(6)
                if symbol: streak[symbol] = int(prior.get("streak_n", 0) or 0)
    processed = []
    for day in days:
        if day not in source_rows: continue
        if catalog.locate("ladder_daily", day):
            continue
        day_rows = {str(row.get("symbol", "")).zfill(6): row for row in source_rows[day] if row.get("symbol")}
        derived = []
        for symbol in symbols:
            row = day_rows.get(symbol)
            state = "MISSING"
            close = row.get("close") if row else None
            amount = row.get("amount_yuan", row.get("amount")) if row else None
            pct = row.get("pct_chg") if row else None
            up = False
            if row and close not in (None, 0) and amount == 0:
                state = "NO_TRADE_OBSERVED"
            elif row and close not in (None, 0) and pct is not None and amount is not None:
                try:
                    implied = (Decimal(str(close)) / (Decimal(1) + Decimal(str(pct)) / Decimal(100)))
                    pre_close = implied.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    fraction = Decimal(limit_percent(symbol)) / Decimal(100)
                    limit_up = (pre_close * (Decimal(1) + fraction)).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP)
                    up = Decimal(str(close)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == limit_up
                    state = "PRICE_CLOSE_PROXY" if amount > 0 else "NO_TRADE_OBSERVED"
                except (InvalidOperation, ValueError, ZeroDivisionError, TypeError):
                    state = "MISSING"
            if state == "NO_TRADE_OBSERVED":
                pass
            elif state == "MISSING":
                streak[symbol] = 0
            else:
                streak[symbol] = streak[symbol] + 1 if up else 0
            derived.append({"symbol": symbol, "trade_date": day, "limit_up": up,
                            "streak_n": streak[symbol], "status": state,
                            "seal_source": "PRICE_CLOSE_PROXY", "st_filter": "UNAVAILABLE"})
        # Name-based ST/new-listing exclusions used by the study cannot be
        # reconstructed from a daily bar that has no point-in-time name field.
        runner.run("ladder_daily", day, fetch=lambda _day, rows=derived: {
            "rows": rows, "expected_count": len(rows), "partial": True},
            source="core_price_close_proxy", dry_run=False)
        processed.append(day)
    comparison = compare_ladder_reference(catalog, reference_path) if reference_path else None
    return {"dataset": "ladder_daily", "from": start, "to": end, "processed": processed,
            "row_count": sum(len(source_rows[day]) for day in processed),
            "missing_daily_kline": missing, "status": "PARTIAL_ST_FILTER_UNAVAILABLE",
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
