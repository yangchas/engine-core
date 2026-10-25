"""Daily K-line sources, availability gates, and immutable quality upgrades."""
from __future__ import annotations

import inspect
import json
import math
import re
import time
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from ..storage import ArtifactCatalog, _atomic_write, canonical_json
from ..registry.daily_kline import classify_daily_kline

SHANGHAI = ZoneInfo("Asia/Shanghai")
PRIORITY = ("baostock", "wencai", "q2_final")
AVAILABLE_AFTER = {"baostock": (17, 30), "wencai": (15, 5), "q2_final": (15, 5)}


def _clock(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI)
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return result.replace(tzinfo=SHANGHAI) if result.tzinfo is None else result.astimezone(SHANGHAI)


def _available_at(trade_date: str, source: str) -> datetime:
    hour, minute = AVAILABLE_AFTER[source]
    return datetime.combine(date.fromisoformat(trade_date), day_time(hour, minute), SHANGHAI)


def _number(value: Any) -> float | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if value in (None, "", "--", "-", "None"):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


class Q2FinalProvider:
    """Read only q2 final snapshot. No key scans and no writes."""
    source = "legacy_q2_final"
    source_dependency = "legacy_redis_q2"
    available_at_rule = "trade_date 15:05 Asia/Shanghai; q2 ts same date >=15:00"

    def __init__(self, redis: Any):
        self.redis = redis

    def fetch(self, trade_date: str, *, now: str | datetime | None = None) -> dict[str, Any]:
        current = _clock(now)
        if current < _available_at(trade_date, "q2_final"):
            return {"status": "PROVIDER_UNAVAILABLE", "reason": "not_yet_available", "rows": []}
        compact_date = trade_date.replace("-", "")
        symbols = self.redis.smembers(f"q2:active:{trade_date}")
        if not symbols:
            # Production q2 uses compact dates; archived fixtures use ISO dates.
            symbols = self.redis.smembers(f"q2:active:{compact_date}")
        symbols = sorted({_text(value).zfill(6) for value in symbols})
        pipe = self.redis.pipeline(transaction=False)
        for symbol in symbols:
            pipe.hgetall(f"q2:{symbol}")
        raw_rows = pipe.execute() if symbols else []
        rows: list[dict[str, Any]] = []
        stale: list[str] = []
        for symbol, raw in zip(symbols, raw_rows):
            values = {_text(key): value for key, value in (raw or {}).items()}
            stamp = _number(values.get("ts"))
            if stamp is None:
                stale.append(symbol)
                continue
            observed = datetime.fromtimestamp(stamp / 1000, SHANGHAI)
            if observed.date().isoformat() != trade_date or observed.time() < day_time(15, 0):
                stale.append(symbol)
                continue
            px, pc = _number(values.get("px")), _number(values.get("pc"))
            amount, volume = _number(values.get("amt")), _number(values.get("vol"))
            high, low = _number(values.get("mx")), _number(values.get("mn"))
            row = {
                "symbol": symbol,
                "date": trade_date,
                "open": None,
                "open_missing": True,
                "close": px / 1000 if px is not None else None,
                "pre_close": pc / 1000 if pc is not None else None,
                "pct_chg": ((px / pc - 1) * 100) if px is not None and pc else None,
                "amount_yuan": amount,
                "volume": volume,
                "high": high / 1000 if high is not None else None,
                "low": low / 1000 if low is not None else None,
                "source": self.source,
                "quality": "DEGRADED",
            }
            rows.append(row)
        expected = len(symbols)
        return {
            "rows": rows,
            "expected_count": expected,
            "stale_count": len(stale),
            "stale_symbols": stale,
            "partial": len(rows) < expected,
            "field_units": {"close/high/low/pre_close": "yuan", "amount_yuan": "yuan",
                            "volume": "UNKNOWN"},
            "field_completeness": {"open": "MISSING", "adjustment": "UNADJUSTED_UNKNOWN"},
            "source_dependency": self.source_dependency,
        }

    def snapshot(self, trade_date: str, artifact_root: str | Path, *,
                 now: str | datetime | None = None) -> Any:
        """Seal the eligible q2 terminal state as an immutable local artifact."""
        from ..jobs import JobResult

        current = _clock(now)
        started = time.monotonic()
        payload = self.fetch(trade_date, now=current)
        if payload.get("status") == "PROVIDER_UNAVAILABLE":
            return None
        rows, quality = DailyKlineProviderChain._quality(payload)
        if quality["coverage"] is None or quality["coverage"] < 0.98:
            return None
        available = _available_at(trade_date, "q2_final")
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        attempts = [{"provider": "q2_final", "result": "ACCEPTED", "attempted_at": current.isoformat(),
                     "elapsed_ms": elapsed_ms, "reason": "coverage_accepted", "chain_result": "ACCEPTED"}]
        manifest = {
            "schema_version": 1, "dataset": "daily_kline", "trade_date": trade_date,
            "provider": "q2_final",
            "source": self.source, "source_dependency": self.source_dependency,
            "available_at": available.isoformat(), "fetched_at": current.isoformat(),
            "as_of": current.isoformat(), "quality": {**quality, "status": "DEGRADED"},
            "degraded": True, "degraded_label": "DEGRADED:q2_final",
            "status": "DEGRADED:q2_final",
            "business_status": classify_daily_kline("q2_final", quality.get("coverage"),
                                                      quality.get("field_completeness", {})),
            "provider_attempts": attempts,
            "field_completeness": payload["field_completeness"], "field_units": payload["field_units"],
        }
        committed = ArtifactCatalog(artifact_root).commit("daily_kline", trade_date, rows,
                                                           manifest, source=self.source)
        latest = ArtifactCatalog(artifact_root).read().get("datasets", {}).get("daily_kline", {}).get("latest")
        if latest is None or str(latest.get("trade_date", "")) <= trade_date:
            catalog = ArtifactCatalog(artifact_root)
            document = catalog.read()
            entry = document.setdefault("datasets", {}).setdefault("daily_kline", {"dates": {}, "latest": None})
            entry["latest"] = {"trade_date": trade_date, "version": committed["version"]}
            _atomic_write(catalog.path, canonical_json(document) + b"\n")
            _atomic_write(catalog.root / "daily_kline" / "latest.json", canonical_json({
                "trade_date": trade_date, "version": committed["version"],
                "manifest": committed["manifest_uri"],
            }) + b"\n")
        return JobResult(committed, rows)


class WencaiDailyProvider:
    """Injected Wencai adapter with a persistent rate gate and immediate 403 breaker."""
    source = "wencai"
    source_dependency = "independent_wencai"
    available_at_rule = "trade_date 15:05 Asia/Shanghai"

    def __init__(self, fetch: Callable[..., Any], *, state_path: str | Path | None,
                 max_requests: int = 15, min_interval_seconds: float = 10.0,
                 breaker_seconds: int = 3 * 60 * 60, windows: tuple[tuple[str, str], ...] = (),
                 expected_count: int | Callable[[str], int] | None = None,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 clock_fn: Callable[[], float] = time.time):
        self.fetcher = fetch
        self.state_path = Path(state_path) if state_path is not None else None
        self._memory_state: dict[str, Any] = {}
        self.max_requests = min(15, max(1, int(max_requests)))
        self.min_interval_seconds = max(10.0, float(min_interval_seconds))
        self.breaker_seconds = breaker_seconds
        self.windows = windows
        self.expected_count = expected_count
        self.sleep_fn, self.clock_fn = sleep_fn, clock_fn

    def _state(self) -> dict[str, Any]:
        if self.state_path is None:
            return dict(self._memory_state)
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, value: Mapping[str, Any]) -> None:
        if self.state_path is None:
            self._memory_state = dict(value)
        else:
            _atomic_write(self.state_path, canonical_json(dict(value)) + b"\n")

    def _blocked_by_window(self, current: datetime) -> bool:
        return any(start <= current.strftime("%H:%M") < end for start, end in self.windows)

    def fetch(self, trade_date: str, *, now: str | datetime | None = None) -> dict[str, Any]:
        current = _clock(now)
        unavailable = {"status": "PROVIDER_UNAVAILABLE", "rows": [], "partial": True}
        if current < _available_at(trade_date, "wencai"):
            return {**unavailable, "reason": "not_yet_available"}
        if self._blocked_by_window(current):
            return {**unavailable, "reason": "configured_window"}
        state = self._state()
        wall = self.clock_fn()
        if float(state.get("blocked_until", 0)) > wall:
            return {**unavailable, "reason": "circuit_open"}
        history = [float(item) for item in state.get("requests", []) if wall - float(item) < 60]
        last = float(state.get("last_request", 0))
        wait = self.min_interval_seconds - (wall - last) if last else 0
        if wait > 0:
            self.sleep_fn(wait)
            wall = self.clock_fn()
            history = [item for item in history if wall - item < 60]
        if len(history) >= self.max_requests:
            return {**unavailable, "reason": "rate_limited"}
        history.append(wall)
        state.update({"requests": history, "last_request": wall})
        self._save_state(state)

        class Context:
            # The first call is already accounted for below; fetchers call request()
            # before each additional page/request.
            request_count = 1
            def request(inner_self):
                now_wall = self.clock_fn()
                entries = [float(item) for item in self._state().get("requests", []) if now_wall - float(item) < 60]
                previous = float(self._state().get("last_request", 0))
                if len(entries) >= self.max_requests:
                    raise RuntimeError("rate_limited:window_budget")
                interval = self.min_interval_seconds - (now_wall - previous) if previous else 0
                if interval > 0:
                    self.sleep_fn(interval)
                    now_wall = self.clock_fn()
                entries.append(now_wall)
                current_state = self._state()
                current_state.update({"requests": entries, "last_request": now_wall})
                self._save_state(current_state)
                inner_self.request_count += 1

        context = Context()
        try:
            try:
                accepts_context = len(inspect.signature(self.fetcher).parameters) >= 2
            except (TypeError, ValueError):
                accepts_context = False
            if accepts_context:
                raw = self.fetcher(trade_date, context)
            else:
                raw = self.fetcher(trade_date)
            marker = str(raw.get("status", "")) + " " + str(raw.get("error", "")) if isinstance(raw, Mapping) else ""
            if (isinstance(raw, Mapping) and raw.get("status_code") == 403) or re.search(r"403|forbidden", marker, re.I):
                state = self._state()
                state["blocked_until"] = self.clock_fn() + self.breaker_seconds
                self._save_state(state)
                return {**unavailable, "reason": "provider_blocked", "status_code": 403}
            payload = dict(raw) if isinstance(raw, Mapping) else {"rows": raw}
            payload["rows"] = self._normalize_rows(payload.get("rows", []), trade_date)
            if "expected_count" not in payload and self.expected_count is not None:
                payload["expected_count"] = (self.expected_count(trade_date)
                                             if callable(self.expected_count) else self.expected_count)
            payload["source"] = self.source
            payload["source_dependency"] = self.source_dependency
            payload.setdefault("field_completeness", {"adjustment": "UNKNOWN"})
            return payload
        except Exception as exc:
            if re.search(r"403|forbidden", str(exc), re.I):
                state = self._state()
                state["blocked_until"] = self.clock_fn() + self.breaker_seconds
                self._save_state(state)
                return {**unavailable, "reason": "provider_blocked", "status_code": 403}
            return {**unavailable, "reason": f"fetch_failed:{type(exc).__name__}"}

    @staticmethod
    def _normalize_rows(raw_rows: Any, trade_date: str) -> list[dict[str, Any]]:
        from ...wencai_fallback import _find as find_wencai_field
        from ...wencai_fallback import _number as normalize_wencai_number
        from ...wencai_fallback import _records as normalize_wencai_records
        from ...wencai_fallback import _symbol as normalize_wencai_symbol

        aliases = {
            "date": ("date", "日期"),
            "open": ("open", "开盘", "开盘价"), "high": ("high", "最高", "最高价"),
            "low": ("low", "最低", "最低价"), "close": ("close", "收盘", "收盘价"),
            "pre_close": ("pre_close", "昨收", "前收盘"),
            "pct_chg": ("pct_chg", "涨跌幅", "涨幅"),
            "volume": ("volume", "成交量"),
            "amount_yuan": ("amount_yuan", "amount", "成交额", "成交金额"),
        }
        normalized: list[dict[str, Any]] = []
        for raw in normalize_wencai_records(raw_rows):
            symbol = normalize_wencai_symbol(raw)
            row: dict[str, Any] = {"symbol": symbol, "date": trade_date}
            found_date = find_wencai_field(raw, *aliases["date"])
            if found_date is not None:
                row["date"] = str(found_date).replace("/", "-")[:10]
            for output, candidates in aliases.items():
                if output == "date":
                    continue
                row[output] = normalize_wencai_number(find_wencai_field(raw, *candidates))
            if row.get("symbol") and row.get("date") == trade_date:
                row["source"] = "wencai"
                row["quality"] = "DEGRADED"
                normalized.append(row)
        return normalized


class DailyKlineProviderChain:
    def __init__(self, *, providers: Mapping[str, Any], artifact_root: str | Path,
                 coverage_threshold: float = 0.98):
        self.providers = dict(providers)
        self.catalog = ArtifactCatalog(artifact_root)
        self.coverage_threshold = coverage_threshold
        self._published: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _call(provider: Any, trade_date: str, now: datetime) -> Any:
        method = provider.fetch if hasattr(provider, "fetch") else provider
        try:
            params = inspect.signature(method).parameters
        except (TypeError, ValueError):
            params = {}
        if "now" in params:
            return method(trade_date, now=now)
        return method(trade_date)

    @staticmethod
    def _quality(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raw_rows = payload.get("rows", [])
        rows = [dict(row) for row in raw_rows if isinstance(row, Mapping)]
        for row in rows:
            if row.get("amount_yuan") is None and row.get("amount") is not None:
                row["amount_yuan"] = _number(row.get("amount"))
            if row.get("pre_close") is None and row.get("preclose") is not None:
                row["pre_close"] = _number(row.get("preclose"))
        expected = payload.get("expected_count")
        if not isinstance(expected, (int, float)) or expected <= 0:
            expected = None
        coverage = (min(1.0, len({str(row.get("symbol", "")) for row in rows}) / expected)
                    if expected else None)
        missing = (max(0, int(expected) - len({str(row.get("symbol", "")) for row in rows}))
                   if expected else None)
        complete_fields = {}
        for key in ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume"):
            present = sum(row.get(key) is not None for row in rows)
            complete_fields[key] = ("COMPLETE" if rows and present == len(rows) else
                                    "MISSING" if present == 0 else "PARTIAL")
        declared = payload.get("field_completeness")
        if isinstance(declared, Mapping):
            complete_fields.update(declared)
        quality = {"coverage": coverage, "missing": missing, "row_count": len(rows),
                   "partial": bool(payload.get("partial")) or coverage is None or coverage < 1,
                   "status": "READY" if coverage is not None and coverage >= 0.98 else "PARTIAL",
                   "field_completeness": complete_fields,
                   "field_units": payload.get("field_units", {})}
        return rows, quality

    @staticmethod
    def _reconcile(old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> dict[str, Any]:
        old = {str(row.get("symbol")): row for row in old_rows}
        new = {str(row.get("symbol")): row for row in new_rows}
        report: dict[str, Any] = {"old_only": len(old.keys() - new.keys()),
                                  "new_only": len(new.keys() - old.keys()), "samples": []}
        tolerances = {"close": 0.00001, "amount_yuan": 0.01, "pct_chg": 0.0001}
        for field, tolerance in tolerances.items():
            different = []
            for symbol in sorted(old.keys() & new.keys()):
                left, right = _number(old[symbol].get(field)), _number(new[symbol].get(field))
                if left is None or right is None:
                    if left != right: different.append((symbol, left, right))
                elif abs(left - right) > tolerance:
                    different.append((symbol, left, right))
            report[field] = {"compared_count": len(old.keys() & new.keys()),
                             "different_count": len(different), "tolerance": tolerance}
            report["samples"].extend({"symbol": s, "field": field, "old": a, "new": b}
                                     for s, a, b in different[:20])
        return report

    def _supersede_prior(self, trade_date: str, new_manifest: dict[str, Any], new_rows: list[dict[str, Any]]) -> None:
        versions = self.catalog.read().get("datasets", {}).get("daily_kline", {}).get("dates", {}).get(trade_date, [])
        comparisons = []
        for item in versions:
            path = Path(item["manifest"])
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if manifest.get("version") == new_manifest.get("version") or manifest.get("status") == "SUPERSEDED":
                continue
            old_rows = self.catalog.read_rows(path)
            comparison = self._reconcile(old_rows, new_rows)
            comparisons.append({"version": manifest.get("version"), "source": manifest.get("source"), **comparison})
            manifest["status"] = "SUPERSEDED"
            manifest["superseded_by"] = new_manifest.get("version")
            manifest["reconciliation"] = comparison
            _atomic_write(path, canonical_json(manifest) + b"\n")
            for published in self._published.get(trade_date, []):
                if published.get("version") == manifest.get("version"):
                    published.update(manifest)
        if comparisons:
            new_manifest["reconciliation"] = comparisons[-1]

    def _advance_latest_pointer(self, trade_date: str, manifest: Mapping[str, Any]) -> None:
        catalog = self.catalog.read()
        entry = catalog.setdefault("datasets", {}).setdefault("daily_kline", {"dates": {}, "latest": None})
        latest = entry.get("latest")
        if latest and str(latest.get("trade_date", "")) > trade_date:
            return
        entry["latest"] = {"trade_date": trade_date, "version": manifest["version"]}
        _atomic_write(self.catalog.path, canonical_json(catalog) + b"\n")
        pointer = {"trade_date": trade_date, "version": manifest["version"],
                   "manifest": manifest["manifest_uri"]}
        _atomic_write(self.catalog.root / "daily_kline" / "latest.json", canonical_json(pointer) + b"\n")

    def _append_job_log(self, trade_date: str, *, started_at: str, elapsed_ms: float,
                        result: str, timing: Mapping[str, Any] | None = None) -> None:
        timing = timing if isinstance(timing, Mapping) else {}
        path = self.catalog.root / "job_log" / f"{trade_date}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"job": "daily_kline", "trade_date": trade_date, "provider": "baostock",
                  "started_at": started_at, "elapsed_ms": elapsed_ms,
                  "login_ms": timing.get("login_ms"), "query_ms": timing.get("query_ms"),
                  "result": result, "error_code": timing.get("error_code")}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def fetch_chain(self, trade_date: str, *, now: str | datetime | None = None,
                    persist: bool = False) -> Any:
        from ..jobs import JobResult

        current = _clock(now)
        attempts: list[dict[str, Any]] = []
        for source in PRIORITY:
            provider = self.providers.get(source)
            if provider is None:
                continue
            available = _available_at(trade_date, source)
            if current < available:
                attempts.append({"provider": source, "attempted_at": current.isoformat(),
                                 "result": "SKIPPED", "reason": "not_yet_available", "available_at": available.isoformat(), "elapsed_ms": 0})
                continue
            started = time.monotonic()
            started_at = datetime.now(SHANGHAI).isoformat()
            try:
                payload = self._call(provider, trade_date, current)
                payload = dict(payload) if isinstance(payload, Mapping) else {"rows": payload}
                timing = payload.get("baostock_timing") if isinstance(payload.get("baostock_timing"), Mapping) else {}
                rows, quality = self._quality(payload)
                status = payload.get("status")
                elapsed_ms = round((time.monotonic() - started) * 1000, 3)
                call_result = str(timing.get("result") or ("error" if status == "PROVIDER_UNAVAILABLE" else "ok"))
                if source == "baostock":
                    self._append_job_log(trade_date, started_at=str(timing.get("started_at") or started_at), elapsed_ms=elapsed_ms,
                                         result=call_result, timing=timing)
                if status == "PROVIDER_UNAVAILABLE" or quality["coverage"] is None or quality["coverage"] < self.coverage_threshold:
                    attempt = {"provider": source, "attempted_at": timing.get("started_at") or current.isoformat(),
                               "result": call_result if source == "baostock" else "REJECTED",
                               "chain_result": "REJECTED",
                               "reason": payload.get("reason", "coverage_unknown_or_below_threshold"),
                               "coverage": quality["coverage"], "elapsed_ms": elapsed_ms}
                    if source == "baostock":
                        attempt.update({key: timing.get(key) for key in ("login_ms", "query_ms", "error_code")})
                    attempts.append(attempt)
                    continue
                attempt = {"provider": source, "attempted_at": timing.get("started_at") or current.isoformat(),
                           "result": call_result if source == "baostock" else "ACCEPTED",
                           "chain_result": "ACCEPTED", "reason": "coverage_accepted",
                           "coverage": quality["coverage"], "elapsed_ms": elapsed_ms}
                if source == "baostock":
                    attempt.update({key: timing.get(key) for key in ("login_ms", "query_ms", "error_code")})
                attempts.append(attempt)
                degraded = source != "baostock"
                quality["status"] = "DEGRADED" if degraded else quality["status"]
                business_status = classify_daily_kline(source, quality.get("coverage"),
                                                       quality.get("field_completeness", {}))
                manifest: dict[str, Any] = {
                    "schema_version": 1, "dataset": "daily_kline", "trade_date": trade_date,
                    "provider": source,
                    "source": payload.get("source", "legacy_q2_final" if source == "q2_final" else source),
                    "source_dependency": payload.get("source_dependency", "legacy_redis_q2" if source == "q2_final" else
                                                       "independent_wencai" if source == "wencai" else "independent_provider"),
                    "available_at": available.isoformat(), "fetched_at": current.isoformat(),
                    "as_of": current.isoformat(), "quality": quality, "degraded": degraded,
                    "degraded_label": f"DEGRADED:{source}" if degraded else None,
                    "status": f"DEGRADED:{source}" if degraded else "FORMAL",
                    "business_status": business_status,
                    "provider_attempts": attempts, "field_completeness": quality["field_completeness"],
                    "field_units": quality["field_units"],
                }
                if persist:
                    committed = self.catalog.commit("daily_kline", trade_date, rows, manifest, source=manifest["source"])
                    manifest.update(committed)
                    prior_rows = self._published.setdefault(trade_date, [])
                    if source == "baostock":
                        self._supersede_prior(trade_date, manifest, rows)
                        _atomic_write(Path(manifest["manifest_uri"]), canonical_json(manifest) + b"\n")
                    self._advance_latest_pointer(trade_date, manifest)
                    prior_rows.append(manifest)
                return JobResult(manifest, rows)
            except Exception as exc:
                elapsed_ms = round((time.monotonic() - started) * 1000, 3)
                timing = getattr(exc, "baostock_timing", {})
                is_timeout = "TIMEOUT" in str(exc).upper() or isinstance(exc, TimeoutError)
                call_result = "timeout" if is_timeout else "error"
                if source == "baostock":
                    self._append_job_log(trade_date, started_at=str(timing.get("started_at") or started_at), elapsed_ms=elapsed_ms,
                                         result=call_result, timing=timing)
                attempt = {"provider": source, "attempted_at": timing.get("started_at") or current.isoformat(),
                           "result": call_result if source == "baostock" else "ERROR",
                           "chain_result": "ERROR", "reason": f"provider_error:{type(exc).__name__}",
                           "elapsed_ms": elapsed_ms}
                if source == "baostock":
                    attempt.update({key: timing.get(key) for key in ("login_ms", "query_ms", "error_code")})
                attempts.append(attempt)
        return None
