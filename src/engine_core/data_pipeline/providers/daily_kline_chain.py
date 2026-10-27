"""Daily K-line sources, availability gates, and immutable quality upgrades."""
from __future__ import annotations

import inspect
import json
import math
import os
import queue
import re
import socket
import threading
import time
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from ..storage import ArtifactCatalog, _atomic_write, canonical_json
from ..registry.daily_kline import classify_daily_kline
from ..daily_kline_schema import (
    CANONICAL_FIELD_UNITS,
    SOURCE_FIELD_UNITS,
    canonicalize_daily_kline_payload,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
PRIORITY = ("baostock", "wencai", "q2_final")
AVAILABLE_AFTER = {"baostock": (17, 30), "wencai": (15, 5), "q2_final": (15, 5)}
WENCAI_DATE_FIELDS = ("open", "high", "low", "close", "pre_close", "pct_chg", "volume", "amount_yuan")


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


def _ratio_meets_threshold(numerator: int, denominator: int, threshold: float) -> bool:
    return denominator > 0 and numerator / denominator >= threshold


def _normalize_source_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    compact = value.strip().replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        return None
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def _text(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def classify_provider_error(value: Any = None, *, status: str | None = None,
                            error_code: str | None = None,
                            reason: str | None = None,
                            retryable: bool | None = None) -> tuple[str, bool, bool]:
    """Return (code, retryable, tier_no_data) without treating programming errors as transient."""
    marker = " ".join(str(item or "") for item in (status, error_code, reason, value)).strip()
    upper = marker.upper()
    if "TIER_NO_DATA" in upper or upper in {"EMPTY_RESPONSE", "NO_DATA"}:
        return "TIER_NO_DATA", False, True
    if "STALL_NO_PROGRESS" in upper:
        return "STALL_NO_PROGRESS", True, False
    if retryable is True or "WENCAI_BLOCKED" in upper:
        if re.search(r"RATE.?LIMIT|TOO MANY REQUESTS|HTTP.?429", upper):
            return "RATE_LIMITED", True, False
        if isinstance(value, (TimeoutError, socket.timeout)) or re.search(r"TIMEOUT|TIMED OUT", upper):
            return "FETCH_TIMEOUT", True, False
        if re.search(r"NETWORK|CONNECTION|DNS|HTTP.?5\d\d|502|503|504|WENCAI_BLOCKED|STALL_NO_PROGRESS",
                     upper):
            return "NETWORK_ERROR", True, False
        return (error_code or "RETRYABLE_PROVIDER_ERROR").upper(), True, False
    if isinstance(value, (TimeoutError, socket.timeout)) or re.search(
            r"TIMEOUT|TIMED OUT|RATE.?LIMIT|TOO MANY REQUESTS|HTTP.?429|NETWORK|CONNECTION|DNS|HTTP.?5\d\d|502|503|504|STALL_NO_PROGRESS",
            upper):
        code = "RATE_LIMITED" if re.search(r"RATE.?LIMIT|TOO MANY REQUESTS|HTTP.?429", upper) else (
            "FETCH_TIMEOUT" if isinstance(value, (TimeoutError, socket.timeout)) or "TIMEOUT" in upper
            or "TIMED OUT" in upper else "NETWORK_ERROR")
        return code, True, False
    if (re.search(r"UNAVAILABLE|SERVICE UNAVAILABLE", upper) and
            not re.search(r"NOT.?CONFIGURED|INVALID|PARAMETER|BAD.?CODE|UNKNOWN.?CODE", upper)):
        return "NETWORK_ERROR", True, False
    if isinstance(value, (ValueError, TypeError, KeyError)) or re.search(
            r"INVALID|PARAMETER|BAD.?CODE|UNKNOWN.?CODE|SCHEMA.?INVALID|CONFIGURATION|NOT_CONFIGURED",
            upper):
        return (error_code or "INVALID_REQUEST").upper(), False, False
    return (error_code or type(value).__name__.upper() if isinstance(value, BaseException)
            else error_code or "PROVIDER_ERROR"), False, False


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
        payload = {
            "rows": rows,
            "expected_count": expected,
            "stale_count": len(stale),
            "stale_symbols": stale,
            "partial": len(rows) < expected,
            "field_units": {"price": "yuan", "pct_chg": "percent", "amount_yuan": "yuan",
                            "volume": "hands"},
            "field_completeness": {"open": "MISSING", "adjustment": "UNADJUSTED_UNKNOWN"},
            "source_dependency": self.source_dependency,
        }
        return canonicalize_daily_kline_payload("q2_final", payload)

    def snapshot(self, trade_date: str, artifact_root: str | Path, *,
                 now: str | datetime | None = None) -> Any:
        """Seal the eligible q2 terminal state as an immutable local artifact."""
        from ..jobs import JobResult

        current = _clock(now)
        started = time.monotonic()
        payload = canonicalize_daily_kline_payload("q2_final", self.fetch(trade_date, now=current))
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
            "tier": 2,
            "business_status": classify_daily_kline("q2_final", quality.get("coverage"),
                                                      quality.get("field_completeness", {})),
            "provider_attempts": attempts,
            "field_completeness": payload["field_completeness"],
            "field_units": CANONICAL_FIELD_UNITS,
            "source_field_units": payload["source_field_units"],
        }
        committed = ArtifactCatalog(artifact_root).commit("daily_kline", trade_date, rows,
                                                           manifest, source=self.source)
        ArtifactCatalog(artifact_root).advance_daily_kline_latest(trade_date, committed)
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
            return {**unavailable, "reason": "circuit_open", "error_code": "RATE_LIMITED",
                    "retryable": True}
        history = [float(item) for item in state.get("requests", []) if wall - float(item) < 60]
        last = float(state.get("last_request", 0))
        wait = self.min_interval_seconds - (wall - last) if last else 0
        if wait > 0:
            self.sleep_fn(wait)
            wall = self.clock_fn()
            history = [item for item in history if wall - item < 60]
        if len(history) >= self.max_requests:
            return {**unavailable, "reason": "rate_limited", "error_code": "RATE_LIMITED",
                    "retryable": True}
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
                return {**unavailable, "reason": "provider_blocked", "status_code": 403,
                        "error_code": "WENCAI_BLOCKED", "retryable": True}
            payload = dict(raw) if isinstance(raw, Mapping) else {"rows": raw}
            payload["rows"] = self._normalize_rows(payload.get("rows", []), trade_date)
            if "expected_count" not in payload and self.expected_count is not None:
                payload["expected_count"] = (self.expected_count(trade_date)
                                             if callable(self.expected_count) else self.expected_count)
            payload["source"] = self.source
            payload["source_dependency"] = self.source_dependency
            if not payload["rows"] and payload.get("partial"):
                payload.setdefault("status", "PROVIDER_UNAVAILABLE")
                payload.setdefault("error_code", "TIER_PARTIAL")
                payload["retryable"] = True
                payload.setdefault("reason", "partial_response_without_rows")
            elif not payload["rows"] and payload.get("status") not in {"PROVIDER_UNAVAILABLE", "TIER_NO_DATA"}:
                payload.update({"status": "TIER_NO_DATA", "error_code": "TIER_NO_DATA",
                                "retryable": False, "reason": "empty_response"})
            payload.setdefault("field_completeness", {"adjustment": "UNKNOWN"})
            return payload
        except Exception as exc:
            if re.search(r"403|forbidden", str(exc), re.I):
                state = self._state()
                state["blocked_until"] = self.clock_fn() + self.breaker_seconds
                self._save_state(state)
                return {**unavailable, "reason": "provider_blocked", "status_code": 403,
                        "error_code": "WENCAI_BLOCKED", "retryable": True}
            error_code, retryable, tier_no_data = classify_provider_error(exc)
            return {**unavailable, "reason": f"fetch_failed:{type(exc).__name__}",
                    "status": "TIER_NO_DATA" if tier_no_data else "PROVIDER_UNAVAILABLE",
                    "error_code": error_code, "retryable": retryable}

    @staticmethod
    def _normalize_rows(raw_rows: Any, trade_date: str) -> list[dict[str, Any]]:
        from .wencai_daily_adapter import _field_value
        from ...wencai_fallback import _number as normalize_wencai_number
        from ...wencai_fallback import _records as normalize_wencai_records
        from ...wencai_fallback import _symbol as normalize_wencai_symbol

        fields = ("open", "high", "low", "close", "pre_close", "pct_chg", "volume", "amount_yuan")
        normalized: list[dict[str, Any]] = []
        for raw in normalize_wencai_records(raw_rows):
            symbol = normalize_wencai_symbol(raw)
            row: dict[str, Any] = {"symbol": symbol, "date": trade_date}
            for output in fields:
                row[output] = normalize_wencai_number(_field_value(raw, output, trade_date))
            if row.get("symbol"):
                row["source"] = "wencai"
                row["quality"] = "DEGRADED"
                normalized.append(row)
        return normalized


class DailyKlineProviderChain:
    def __init__(self, *, providers: Mapping[str, Any], artifact_root: str | Path,
                 coverage_threshold: float = 0.98,
                 wencai_date_validation_threshold: float = 0.95,
                 wencai_reference_coverage_threshold: float = 0.95,
                 wencai_reference_strategy: str = "previous_trade_date",
                 wencai_reference_date: str | None = None,
                 wencai_retry_attempts: int = 2,
                 wencai_retry_backoff_seconds: float = 10.0,
                 sleep_fn: Callable[[float], None] = time.sleep):
        self.providers = dict(providers)
        self.catalog = ArtifactCatalog(artifact_root)
        self.coverage_threshold = coverage_threshold
        if not 0 < float(wencai_date_validation_threshold) <= 1:
            raise ValueError("wencai_date_validation_threshold_must_be_between_zero_and_one")
        if not 0 < float(wencai_reference_coverage_threshold) <= 1:
            raise ValueError("wencai_reference_coverage_threshold_must_be_between_zero_and_one")
        if wencai_reference_strategy not in {"previous_trade_date", "explicit"}:
            raise ValueError("wencai_reference_strategy_invalid")
        if wencai_reference_strategy == "explicit" and not wencai_reference_date:
            raise ValueError("wencai_reference_date_required_for_explicit_strategy")
        if wencai_reference_date is not None:
            wencai_reference_date = date.fromisoformat(wencai_reference_date).isoformat()
        self.wencai_date_validation_threshold = float(wencai_date_validation_threshold)
        self.wencai_reference_coverage_threshold = float(wencai_reference_coverage_threshold)
        self.wencai_reference_strategy = wencai_reference_strategy
        self.wencai_reference_date = wencai_reference_date
        self.wencai_retry_attempts = max(1, int(wencai_retry_attempts))
        self.wencai_retry_backoff_seconds = max(0.0, float(wencai_retry_backoff_seconds))
        self.sleep_fn = sleep_fn
        self._published: dict[str, list[dict[str, Any]]] = {}
        self.last_attempts: list[dict[str, Any]] = []

    def _wencai_reference_day(self, trade_date: str) -> str | None:
        if self.wencai_reference_strategy == "explicit":
            return self.wencai_reference_date
        try:
            from ..trade_calendar import previous_trade_date

            return previous_trade_date(trade_date, root=self.catalog.root).isoformat()
        except Exception:
            return None

    def _wencai_date_evidence(self, trade_date: str, payload: Mapping[str, Any],
                              rows: list[dict[str, Any]], quality: Mapping[str, Any]
                              ) -> tuple[str | None, dict[str, Any]]:
        source_dates = payload.get("source_dates")
        date_detail: dict[str, Any] = {"requested_date": trade_date, "source_dates": source_dates,
                                       "source_dates_consistent": payload.get("source_dates_consistent")}
        if not isinstance(source_dates, Mapping):
            return "TIER_DATE_MISMATCH", {**date_detail, "source_dates_status": "MISSING"}
        missing_fields = []
        mismatched_fields: dict[str, Any] = {}
        for field in WENCAI_DATE_FIELDS:
            value = source_dates.get(field)
            values = value if isinstance(value, (list, tuple, set)) else [value]
            normalized = [_normalize_source_date(item) for item in values]
            normalized = [item for item in normalized if item is not None]
            if not normalized:
                missing_fields.append(field)
            elif any(item != trade_date for item in normalized):
                mismatched_fields[field] = sorted(set(normalized))
        all_source_dates = payload.get("source_date_values")
        if isinstance(all_source_dates, (list, tuple, set)):
            normalized_all = [_normalize_source_date(item) for item in all_source_dates]
            normalized_all = [item for item in normalized_all if item is not None]
            if any(item != trade_date for item in normalized_all):
                mismatched_fields["numeric_columns"] = sorted(set(normalized_all))
            if not normalized_all:
                missing_fields.append("numeric_columns")
        if missing_fields or mismatched_fields or payload.get("source_dates_consistent") is False:
            return "TIER_DATE_MISMATCH", {
                **date_detail, "source_dates_status": "MISMATCH",
                "missing_fields": sorted(set(missing_fields)),
                "mismatched_fields": mismatched_fields,
            }

        reference_date = self._wencai_reference_day(trade_date)
        if not reference_date or reference_date >= trade_date:
            return "TIER_DATE_NO_REFERENCE", {
                **date_detail, "source_dates_status": "MATCH", "reference_date": reference_date,
                "reason": "previous_trade_date_unavailable",
            }
        reference_path = self.catalog.locate("daily_kline", reference_date)
        if reference_path is None:
            return "TIER_DATE_NO_REFERENCE", {
                **date_detail, "source_dates_status": "MATCH", "reference_date": reference_date,
                "reason": "reference_partition_missing",
            }
        try:
            reference_rows = self.catalog.read_rows(reference_path)
        except (OSError, ValueError, TypeError):
            reference_rows = []
        reference_manifest: dict[str, Any] = {}
        try:
            reference_manifest = json.loads(reference_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if reference_manifest.get("trade_date") not in (None, reference_date):
            return "TIER_DATE_NO_REFERENCE", {
                **date_detail, "source_dates_status": "MATCH", "reference_date": reference_date,
                "reason": "reference_date_mismatch",
            }
        reference_by_symbol = {
            self._symbol_of(row): _number(row.get("close")) for row in reference_rows
            if self._symbol_of(row) and _number(row.get("close")) is not None
        }
        comparable = [(row, reference_by_symbol[self._symbol_of(row)]) for row in rows
                      if self._symbol_of(row) in reference_by_symbol]
        expected = quality.get("expected_count")
        denominator = int(expected) if isinstance(expected, (int, float)) and expected > 0 else len(rows)
        reference_coverage = len(comparable) / denominator if denominator else 0.0
        base_detail = {
            **date_detail, "source_dates_status": "MATCH", "reference_date": reference_date,
            "reference_coverage": reference_coverage,
            "reference_coverage_threshold": self.wencai_reference_coverage_threshold,
            "reference_row_count": len(comparable), "reference_denominator": denominator,
        }
        if (not comparable or not _ratio_meets_threshold(
                len(comparable), denominator, self.wencai_reference_coverage_threshold)):
            return "TIER_DATE_NO_REFERENCE", {**base_detail, "reason": "reference_coverage_low"}

        preclose_comparable = [(row, previous_close) for row, previous_close in comparable
                               if _number(row.get("pre_close")) is not None]
        preclose_matches = sum(abs(float(row["pre_close"]) - previous_close) <= 0.01
                               for row, previous_close in preclose_comparable)
        preclose_ratio = (preclose_matches / len(preclose_comparable)
                          if preclose_comparable else 0.0)
        pct_comparable = [row for row in rows if _number(row.get("close")) is not None
                          and _number(row.get("pre_close")) not in (None, 0)
                          and _number(row.get("pct_chg")) is not None]
        pct_matches = 0
        for row in pct_comparable:
            expected_pct = (float(row["close"]) / float(row["pre_close"]) - 1.0) * 100.0  # canonical pct_chg is percent (C579)
            pct_matches += abs(float(row["pct_chg"]) - expected_pct) <= 0.01 + 1e-9
        pct_ratio = pct_matches / len(pct_comparable) if pct_comparable else 0.0
        detail = {
            **base_detail,
            "pre_close_match_count": preclose_matches,
            "pre_close_comparable_count": len(preclose_comparable),
            "pre_close_match_ratio": preclose_ratio,
            "pct_chg_consistent_count": pct_matches,
            "pct_chg_comparable_count": len(pct_comparable),
            "pct_chg_consistency_ratio": pct_ratio,
            "match_threshold": self.wencai_date_validation_threshold,
        }
        if (not preclose_comparable or not pct_comparable or
                not _ratio_meets_threshold(preclose_matches, len(preclose_comparable),
                                           self.wencai_date_validation_threshold) or
                not _ratio_meets_threshold(pct_matches, len(pct_comparable),
                                           self.wencai_date_validation_threshold)):
            detail.update({"status": "REJECTED", "reason": "column_date_and_value_evidence_conflict"})
            return "TIER_DATE_MISMATCH", detail
        return None, {**detail, "status": "MATCH"}

    def _save_wencai_checkpoint(self, trade_date: str, payload: Mapping[str, Any],
                                rows: list[dict[str, Any]], quality: Mapping[str, Any],
                                error_code: str) -> None:
        path = self.catalog.root / ".checkpoints" / f"daily_kline-wencai-{trade_date}.json"
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if len(previous.get("rows", [])) > len(rows):
                return
        except (OSError, ValueError, TypeError):
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, canonical_json({
            "trade_date": trade_date, "provider": "wencai", "error_code": error_code,
            "expected_count": quality.get("expected_count"), "coverage": quality.get("coverage"),
            "partial": True, "source_dates": payload.get("source_dates", {}), "rows": rows,
        }) + b"\n")

    def _fetch_wencai_with_retries(self, provider: Any, trade_date: str, now: datetime,
                                   deadline: float) -> tuple[dict[str, Any] | None,
                                                            list[dict[str, Any]], bool]:
        retry_records: list[dict[str, Any]] = []
        for attempt_number in range(1, self.wencai_retry_attempts + 1):
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                retry_records.append({"provider": "wencai", "result": "ERROR",
                                      "chain_result": "REJECTED", "error_code": "DAILY_KLINE_BUDGET_EXHAUSTED",
                                      "retryable": True, "reason": "wencai_retry_budget_exhausted"})
                return None, retry_records, True
            started = time.monotonic()
            try:
                raw = self._call_bounded(provider, trade_date, now, remaining)
                payload = dict(raw) if isinstance(raw, Mapping) else {"rows": raw}
            except Exception as exc:
                error_code, retryable, _ = classify_provider_error(exc)
                retry_records.append({"provider": "wencai", "result": "ERROR",
                                      "chain_result": "RETRY" if retryable and attempt_number < self.wencai_retry_attempts else "REJECTED",
                                      "error_code": error_code, "retryable": retryable,
                                      "reason": f"provider_error:{type(exc).__name__}",
                                      "attempt_number": attempt_number,
                                      "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
                if retryable and attempt_number < self.wencai_retry_attempts:
                    self.sleep_fn(self.wencai_retry_backoff_seconds)
                    continue
                return None, retry_records, True

            status = str(payload.get("status") or "")
            if status in {"PROVIDER_UNAVAILABLE", "TIER_NO_DATA"}:
                error_code, retryable, tier_no_data = classify_provider_error(
                    status=status, error_code=payload.get("error_code"),
                    reason=payload.get("reason"), retryable=payload.get("retryable"))
                if retryable:
                    retry_records.append({"provider": "wencai", "result": "ERROR",
                                          "chain_result": "RETRY" if attempt_number < self.wencai_retry_attempts else "REJECTED",
                                          "error_code": error_code, "retryable": True,
                                          "reason": payload.get("reason", status),
                                          "attempt_number": attempt_number,
                                          "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
                    if attempt_number < self.wencai_retry_attempts:
                        self.sleep_fn(self.wencai_retry_backoff_seconds)
                        continue
                    return None, retry_records, True
                if tier_no_data:
                    return payload, retry_records, False
                return None, retry_records, True

            try:
                normalized, _ = self._canonical_rows("wencai", trade_date, payload)
                rows, quality = self._quality(normalized)
                quality = self._apply_active_universe_coverage(trade_date, rows, quality)
            except Exception:
                return payload, retry_records, False
            incomplete = (bool(payload.get("partial")) or quality.get("coverage") is None or
                          quality.get("coverage") < self.coverage_threshold)
            if not incomplete:
                return payload, retry_records, False

            error_code = str(payload.get("error_code") or
                             ("TIER_PARTIAL" if payload.get("partial") else "COVERAGE_LOW"))
            retry_records.append({"provider": "wencai", "result": "PARTIAL",
                                  "chain_result": "RETRY" if attempt_number < self.wencai_retry_attempts else "REJECTED",
                                  "error_code": error_code, "retryable": True,
                                  "reason": ("upstream_partial" if payload.get("partial") else "coverage_below_threshold"),
                                  "coverage": quality.get("coverage"),
                                  "attempt_number": attempt_number,
                                  "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
            self._save_wencai_checkpoint(trade_date, payload, rows, quality, error_code)
            if attempt_number < self.wencai_retry_attempts:
                self.sleep_fn(self.wencai_retry_backoff_seconds)
                continue
            return None, retry_records, True
        return None, retry_records, True

    @staticmethod
    def _symbol_of(row: Mapping[str, Any]) -> str:
        value = str(row.get("symbol", row.get("code", "")) or "").strip()
        if "." in value:
            value = value.rsplit(".", 1)[-1]
        return value.zfill(6) if value.isdigit() else value

    def _canonical_rows(self, source: str, trade_date: str,
                        payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        """Normalize units, remove non-universe rows, and select one duplicate deterministically."""
        normalized_payload = dict(payload)
        raw_rows = payload.get("rows", [])
        if not isinstance(raw_rows, (list, tuple)):
            raise ValueError("schema_invalid:rows_not_a_list")
        universe_path = self.catalog.locate("symbol_universe", trade_date)
        universe: set[str] | None = None
        if universe_path is not None:
            universe = {self._symbol_of(row) for row in self.catalog.read_rows(universe_path)}
            universe.discard("")

        conversion_units = payload.get("field_units")
        if not isinstance(conversion_units, Mapping):
            conversion_units = SOURCE_FIELD_UNITS[source]
        source_units = payload.get("source_field_units")
        if not isinstance(source_units, Mapping):
            source_units = conversion_units
        groups: dict[str, list[dict[str, Any]]] = {}
        excluded = 0
        invalid = 0
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                invalid += 1
                continue
            symbol = self._symbol_of(raw)
            if not symbol or len(symbol) != 6 or not symbol.isdigit():
                invalid += 1
                continue
            if universe is not None and symbol not in universe:
                excluded += 1
                continue
            row = canonicalize_daily_kline_payload(
                source, {"rows": [raw], "field_units": conversion_units})["rows"][0]
            groups.setdefault(symbol, []).append(row)

        rows: list[dict[str, Any]] = []
        duplicate_rows = duplicate_symbols = conflicting_symbols = 0
        value_fields = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")
        for symbol, candidates in sorted(groups.items()):
            if len(candidates) > 1:
                duplicate_rows += len(candidates) - 1
                duplicate_symbols += 1
                signatures = {tuple(candidate.get(field) for field in value_fields)
                              for candidate in candidates}
                conflicting_symbols += int(len(signatures) > 1)
            candidates.sort(key=lambda row: (
                -sum(row.get(field) is not None for field in value_fields),
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ))
            rows.append(candidates[0])

        normalized_payload["rows"] = rows
        normalized_payload["field_units"] = dict(CANONICAL_FIELD_UNITS)
        normalized_payload["source_field_units"] = dict(source_units)
        normalized_payload["tier"] = {"baostock": 0, "wencai": 1, "q2_final": 2}[source]
        normalized_payload["degraded"] = source != "baostock"
        normalized_payload["source_dates"] = payload.get("source_dates", {})
        counts = {"input_rows": len(raw_rows), "universe_excluded_rows": excluded,
                  "invalid_rows": invalid, "duplicate_rows": duplicate_rows,
                  "duplicate_symbols": duplicate_symbols,
                  "conflicting_duplicate_symbols": conflicting_symbols}
        return normalized_payload, counts

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
    def _call_bounded(provider: Any, trade_date: str, now: datetime, timeout: float) -> Any:
        """Bound a source without an internal deadline to the chain's remaining wall time."""
        if timeout <= 0:
            raise TimeoutError("DAILY_KLINE_BUDGET_EXHAUSTED")
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result.put((True, DailyKlineProviderChain._call(provider, trade_date, now)))
            except BaseException as exc:
                result.put((False, exc))

        threading.Thread(target=invoke, name="daily-kline-provider", daemon=True).start()
        try:
            succeeded, value = result.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("DAILY_KLINE_BUDGET_EXHAUSTED") from None
        if succeeded:
            return value
        raise value

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
                   "field_units": payload.get("field_units", CANONICAL_FIELD_UNITS),
                   "source_field_units": payload.get("source_field_units", {}),
                   "source_dates": payload.get("source_dates", {})}
        return rows, quality

    def _apply_active_universe_coverage(self, trade_date: str, rows: list[dict[str, Any]],
                                        quality: dict[str, Any]) -> dict[str, Any]:
        universe_path = self.catalog.locate("symbol_universe", trade_date)
        if universe_path is None:
            return quality
        universe_rows = self.catalog.read_rows(universe_path)

        def symbol_of(row: Mapping[str, Any]) -> str:
            symbol = str(row.get("symbol", row.get("code", ""))).strip()
            if "." in symbol:
                symbol = symbol.rsplit(".", 1)[-1]
            return symbol.zfill(6) if symbol.isdigit() else symbol

        universe = {symbol_of(row): row for row in universe_rows}
        universe.pop("", None)
        suspended = {symbol for symbol, row in universe.items()
                     if str(row.get("trade_status", "")).strip() == "0"}
        active = set(universe) - suspended
        row_symbols = {symbol_of(row) for row in rows}
        row_symbols.discard("")
        present_active = active & row_symbols
        coverage = len(present_active) / len(active) if active else None
        quality.update({"expected_count": len(active), "expected_basis": "universe_active",
                        "suspended_in_universe": len(suspended),
                        "suspended_without_bar": len(suspended - row_symbols),
                        "coverage": coverage,
                        "missing": len(active - row_symbols) if active else None,
                        "partial": coverage is None or coverage < 1.0,
                        "status": "READY" if coverage is not None and coverage >= 0.98 else "PARTIAL"})
        return quality

    def _apply_sample_coverage(self, trade_date: str, rows: list[dict[str, Any]],
                               payload: Mapping[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
        sample_symbols = payload.get("sample_symbols")
        if not isinstance(sample_symbols, (list, tuple)):
            raise ValueError("symbols_file_sample_symbols_required")
        sample = {str(value).rsplit(".", 1)[-1].zfill(6) for value in sample_symbols}
        universe_path = self.catalog.locate("symbol_universe", trade_date)
        if universe_path is None:
            raise ValueError("symbol_universe_artifact_required:" + trade_date)
        universe_rows = self.catalog.read_rows(universe_path)

        def symbol_of(row: Mapping[str, Any]) -> str:
            value = str(row.get("symbol", row.get("code", ""))).strip()
            if "." in value:
                value = value.rsplit(".", 1)[-1]
            return value.zfill(6) if value.isdigit() else value

        universe = {symbol_of(row): row for row in universe_rows}
        if sample - universe.keys():
            raise ValueError("symbols_file_outside_universe")
        suspended = {symbol for symbol in sample
                     if str(universe[symbol].get("trade_status", "")).strip() == "0"}
        active = sample - suspended
        row_symbols = {symbol_of(row) for row in rows}
        present = active & row_symbols
        coverage = len(present) / len(active) if active else None
        quality.update({"expected_count": len(active), "expected_basis": "symbols_file_active",
                        "sample_symbol_count": len(sample), "suspended_in_universe": len(suspended),
                        "suspended_without_bar": len(suspended - row_symbols),
                        "coverage": coverage,
                        "missing": len(active - row_symbols) if active else None,
                        "partial": True, "status": "PARTIAL"})
        return quality

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
        self.catalog.advance_daily_kline_latest(trade_date, manifest)

    def _append_job_log(self, trade_date: str, *, started_at: str, elapsed_ms: float,
                        result: str, timing: Mapping[str, Any] | None = None) -> None:
        timing = timing if isinstance(timing, Mapping) else {}
        path = self.catalog.root / "job_log" / f"{trade_date}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"job": "daily_kline", "trade_date": trade_date, "provider": "baostock",
                  "started_at": started_at, "elapsed_ms": elapsed_ms,
                  "login_ms": timing.get("login_ms"), "query_ms": timing.get("query_ms"),
                  "query_count": timing.get("query_count"),
                  "error_events": timing.get("error_events", []),
                  "result": result, "error_code": timing.get("error_code")}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def fetch_chain(self, trade_date: str, *, now: str | datetime | None = None,
                    persist: bool = False, budget_seconds: float | None = None,
                    baostock_budget_fraction: float = 0.6) -> Any:
        from ..jobs import JobResult

        if budget_seconds is None:
            configured_budget = os.getenv("CORE_DAILY_KLINE_BUDGET_SECONDS")
            budget_seconds = float(configured_budget) if configured_budget else 240.0
        budget_seconds = float(budget_seconds)
        baostock_budget_fraction = float(baostock_budget_fraction)
        if not math.isfinite(budget_seconds) or budget_seconds <= 0:
            raise ValueError("daily_kline_budget_seconds_must_be_positive")
        if (not math.isfinite(baostock_budget_fraction) or
                not 0 < baostock_budget_fraction <= 1):
            raise ValueError("baostock_budget_fraction_must_be_between_zero_and_one")

        current = _clock(now)
        chain_started = time.monotonic()
        chain_deadline = chain_started + budget_seconds
        baostock_deadline = chain_started + budget_seconds * baostock_budget_fraction
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
            remaining = max(0.0, chain_deadline - time.monotonic())
            if remaining <= 0:
                attempts.append({"provider": source, "attempted_at": current.isoformat(),
                                 "result": "SKIPPED", "reason": "chain_budget_exhausted", "elapsed_ms": 0})
                break
            call_budget = remaining
            original_total_timeout: Any = None
            bounded_baostock = False
            if source == "baostock":
                call_budget = min(remaining, max(0.0, baostock_deadline - time.monotonic()))
                if call_budget <= 0:
                    attempts.append({"provider": source, "attempted_at": current.isoformat(),
                                     "result": "budget_exhausted", "chain_result": "REJECTED",
                                     "reason": "baostock_budget_exhausted_before_call", "elapsed_ms": 0,
                                     "completed_symbols": 0, "expected_symbols": None,
                                     "checkpoint_saved": False})
                    continue
                if hasattr(provider, "total_timeout_seconds"):
                    original_total_timeout = provider.total_timeout_seconds
                    provider.total_timeout_seconds = (call_budget if original_total_timeout is None else
                                                      min(float(original_total_timeout), call_budget))
                    bounded_baostock = True
            started = time.monotonic()
            started_at = datetime.now(SHANGHAI).isoformat()
            try:
                if source == "wencai":
                    payload, retry_records, exhausted = self._fetch_wencai_with_retries(
                        provider, trade_date, current, chain_deadline)
                    attempts.extend(retry_records)
                    if exhausted or payload is None:
                        self.last_attempts = list(attempts)
                        return None
                elif source == "baostock" and bounded_baostock:
                    payload = self._call(provider, trade_date, current)
                else:
                    payload = self._call_bounded(provider, trade_date, current, call_budget)
                payload = dict(payload) if isinstance(payload, Mapping) else {"rows": payload}
                timing = payload.get("baostock_timing") if isinstance(payload.get("baostock_timing"), Mapping) else {}
                if bounded_baostock:
                    provider.total_timeout_seconds = original_total_timeout
                status = str(payload.get("status") or "")
                if not payload.get("rows") and not status:
                    timing_result = str(timing.get("result") or "").lower()
                    if source == "baostock" and timing_result in {"budget_exhausted", "timeout", "error"}:
                        failure_code = ("DAILY_KLINE_BUDGET_EXHAUSTED" if timing_result == "budget_exhausted"
                                        else timing.get("error_code"))
                        failure_reason = timing.get("error_events") or timing_result
                        error_code, retryable, _ = classify_provider_error(
                            error_code=failure_code, reason=failure_reason,
                            retryable=timing_result in {"budget_exhausted", "timeout"})
                        payload.update({"status": "PROVIDER_UNAVAILABLE", "error_code": error_code,
                                        "retryable": retryable, "reason": f"baostock_{timing_result}"})
                    else:
                        payload.update({"status": "TIER_NO_DATA", "error_code": "TIER_NO_DATA",
                                        "retryable": False, "reason": "empty_response"})
                    status = str(payload["status"])
                if status in {"PROVIDER_UNAVAILABLE", "TIER_NO_DATA"}:
                    error_code, retryable, tier_no_data = classify_provider_error(
                        status=status, error_code=payload.get("error_code"),
                        reason=payload.get("reason"), retryable=payload.get("retryable"))
                    attempt = {"provider": source, "attempted_at": current.isoformat(),
                               "result": "TIER_NO_DATA" if tier_no_data else "ERROR",
                               "chain_result": "REJECTED", "reason": payload.get("reason", status),
                               "error_code": error_code, "retryable": retryable,
                               "tier_no_data": tier_no_data,
                               "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
                    attempts.append(attempt)
                    if not retryable and not tier_no_data:
                        break
                    continue
                payload, row_counts = self._canonical_rows(source, trade_date, payload)
                payload["data_quality"] = row_counts
                rows, quality = self._quality(payload)
                quality["data_quality"] = row_counts
                sample_scope = payload.get("sample_scope") == "symbols_file"
                if sample_scope:
                    quality = self._apply_sample_coverage(trade_date, rows, payload, quality)
                else:
                    quality = self._apply_active_universe_coverage(trade_date, rows, quality)
                elapsed_ms = round((time.monotonic() - started) * 1000, 3)
                call_result = str(timing.get("result") or ("error" if status == "PROVIDER_UNAVAILABLE" else "ok"))
                if source == "baostock":
                    if (call_result == "ok" and
                            (quality["coverage"] is None or quality["coverage"] < self.coverage_threshold)):
                        call_result = "error"
                    self._append_job_log(trade_date, started_at=str(timing.get("started_at") or started_at), elapsed_ms=elapsed_ms,
                                         result=call_result, timing=timing)
                baostock_progress = {key: timing.get(key, payload.get(key)) for key in
                                     ("completed_symbols", "expected_symbols", "resumed_count",
                                      "checkpoint_saved", "checkpoint_path")}
                if source == "baostock":
                    baostock_progress["baostock_budget_seconds"] = call_budget
                if (quality["coverage"] is None or quality["coverage"] < self.coverage_threshold or
                        (source == "baostock" and call_result in {"budget_exhausted", "timeout", "error"})):
                    reason = "coverage_unknown_or_below_threshold"
                    error_code = "COVERAGE_LOW"
                    retryable = False
                    if source == "baostock" and call_result in {"budget_exhausted", "timeout", "error"}:
                        timing_code = timing.get("error_code")
                        error_code, retryable, _ = classify_provider_error(
                            error_code=("DAILY_KLINE_BUDGET_EXHAUSTED" if call_result == "budget_exhausted"
                                        else timing_code or call_result),
                            reason=timing.get("error_events"))
                        reason = ("baostock_budget_exhausted" if call_result == "budget_exhausted"
                                  else "baostock_retryable_failure" if retryable else "baostock_quality_rejected")
                    attempt = {"provider": source, "attempted_at": timing.get("started_at") or current.isoformat(),
                               "result": call_result if source == "baostock" else "REJECTED",
                               "chain_result": "REJECTED",
                               "reason": reason,
                               "error_code": error_code, "retryable": retryable,
                               "coverage": quality["coverage"], "elapsed_ms": elapsed_ms}
                    if source == "baostock":
                        attempt.update({key: timing.get(key) for key in
                                        ("login_ms", "query_ms", "query_count", "error_events")})
                        attempt.update(baostock_progress)
                        if call_result == "budget_exhausted":
                            attempt["reason"] = "baostock_budget_exhausted"
                    attempts.append(attempt)
                    continue
                if source == "wencai":
                    error_code, date_detail = self._wencai_date_evidence(
                        trade_date, payload, rows, quality)
                    payload["wencai_date_validation"] = date_detail
                    if error_code:
                        attempts.append({
                            "provider": source, "attempted_at": current.isoformat(),
                            "result": "REJECTED", "chain_result": "REJECTED",
                            "reason": date_detail.get("reason", "wencai_date_evidence_rejected"),
                            "error_code": error_code, "retryable": False,
                            "coverage": quality.get("coverage"), "error_detail": date_detail,
                            "elapsed_ms": elapsed_ms,
                        })
                        self.last_attempts = list(attempts)
                        return None
                attempt = {"provider": source, "attempted_at": timing.get("started_at") or current.isoformat(),
                           "result": call_result if source == "baostock" else "ACCEPTED",
                           "chain_result": "ACCEPTED", "reason": "coverage_accepted",
                           "error_code": None, "retryable": False,
                           "coverage": quality["coverage"], "elapsed_ms": elapsed_ms}
                if source == "baostock":
                    attempt.update({key: timing.get(key) for key in
                                    ("login_ms", "query_ms", "query_count", "error_code", "error_events")})
                    attempt.update(baostock_progress)
                attempts.append(attempt)
                degraded = source != "baostock"
                quality["status"] = "DEGRADED" if degraded else quality["status"]
                business_status = ("PROVISIONAL" if degraded or sample_scope else
                                   classify_daily_kline(source, quality.get("coverage"),
                                                        quality.get("field_completeness", {})))
                manifest: dict[str, Any] = {
                    "schema_version": 1, "dataset": "daily_kline", "trade_date": trade_date,
                    "provider": source,
                    "source": payload.get("source", "legacy_q2_final" if source == "q2_final" else source),
                    "provider_chain": ">".join(PRIORITY),
                    "source_dependency": payload.get("source_dependency", "legacy_redis_q2" if source == "q2_final" else
                                                       "independent_wencai" if source == "wencai" else "independent_provider"),
                    "available_at": available.isoformat(), "fetched_at": current.isoformat(),
                    "as_of": current.isoformat(), "quality": quality, "degraded": degraded,
                    "degraded_label": f"DEGRADED:{source}" if degraded else None,
                    "status": (f"DEGRADED:{source}" if degraded else
                               "SAMPLE_ONLY" if sample_scope else "FORMAL"),
                    "business_status": business_status,
                    "tier": payload.get("tier", 1 if degraded else 0),
                    "provider_attempts": attempts, "field_completeness": quality["field_completeness"],
                    "field_units": quality["field_units"],
                    "source_field_units": quality["source_field_units"],
                    "source_dates": payload.get("source_dates", {}),
                    "source_dates_consistent": payload.get("source_dates_consistent"),
                    "wencai_date_validation": payload.get("wencai_date_validation"),
                    "quality_notes": {
                        "universe_excluded_rows": row_counts["universe_excluded_rows"],
                        "invalid_rows": row_counts["invalid_rows"],
                        "duplicate_rows": row_counts["duplicate_rows"],
                        "duplicate_symbols": row_counts["duplicate_symbols"],
                        "conflicting_duplicate_symbols": row_counts["conflicting_duplicate_symbols"],
                        "duplicate_selection": "most_non_null_canonical_fields_then_lexicographically_smallest_canonical_json",
                        "coverage_denominator": "local_active_symbol_universe",
                    },
                }
                if sample_scope:
                    manifest["sample_scope"] = "symbols_file"
                    manifest["sample_symbols"] = list(payload["sample_symbols"])
                if persist:
                    committed = self.catalog.commit("daily_kline", trade_date, rows, manifest, source=manifest["source"])
                    manifest.update(committed)
                    prior_rows = self._published.setdefault(trade_date, [])
                    if source == "baostock" and not sample_scope:
                        self._supersede_prior(trade_date, manifest, rows)
                        _atomic_write(Path(manifest["manifest_uri"]), canonical_json(manifest) + b"\n")
                    if not sample_scope:
                        self._advance_latest_pointer(trade_date, manifest)
                    prior_rows.append(manifest)
                self.last_attempts = list(attempts)
                return JobResult(manifest, rows)
            except Exception as exc:
                if bounded_baostock:
                    provider.total_timeout_seconds = original_total_timeout
                elapsed_ms = round((time.monotonic() - started) * 1000, 3)
                timing = getattr(exc, "baostock_timing", {})
                progress = getattr(exc, "baostock_progress", {})
                is_budget_exhausted = ("DAILY_KLINE_BUDGET_EXHAUSTED" in str(exc).upper() or
                                       (source == "baostock" and time.monotonic() >= baostock_deadline))
                is_timeout = "TIMEOUT" in str(exc).upper() or isinstance(exc, TimeoutError)
                call_result = ("budget_exhausted" if is_budget_exhausted else
                               "timeout" if is_timeout else "error")
                if is_budget_exhausted:
                    error_code, retryable = "DAILY_KLINE_BUDGET_EXHAUSTED", True
                else:
                    error_code, retryable, _ = classify_provider_error(
                        exc, error_code=timing.get("error_code"))
                if source == "baostock":
                    self._append_job_log(trade_date, started_at=str(timing.get("started_at") or started_at), elapsed_ms=elapsed_ms,
                                         result=call_result, timing=timing)
                attempt = {"provider": source, "attempted_at": timing.get("started_at") or current.isoformat(),
                           "result": call_result if source == "baostock" else "ERROR",
                           "chain_result": "REJECTED" if source == "baostock" else "ERROR",
                           "reason": ("baostock_budget_exhausted" if source == "baostock" and
                                      call_result == "budget_exhausted" else
                                      f"provider_error:{type(exc).__name__}"),
                           "error_code": error_code, "retryable": retryable,
                           "elapsed_ms": elapsed_ms}
                if source == "baostock":
                    attempt.update({key: timing.get(key) for key in
                                    ("login_ms", "query_ms", "query_count", "error_events")})
                    attempt.update({key: progress.get(key) for key in
                                    ("completed_symbols", "expected_symbols", "resumed_count",
                                     "checkpoint_saved", "checkpoint_path")})
                    attempt["baostock_budget_seconds"] = call_budget
                attempts.append(attempt)
                if not retryable:
                    break
        self.last_attempts = list(attempts)
        return None
