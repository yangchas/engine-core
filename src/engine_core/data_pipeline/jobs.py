"""Fetch/clean/validate/compute/artifact-publish jobs with injected providers."""
from __future__ import annotations

import json
import inspect
import hashlib
import gzip
import math
import re
import time
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .registry import DatasetSpec, get_dataset
from .registry.daily_kline import classify_daily_kline
from .storage import ArtifactCatalog, canonical_json


@dataclass(frozen=True)
class JobResult:
    manifest: dict[str, Any]
    rows: list[dict[str, Any]]


class RequestBudget:
    """In-process request counter; callers should share one per provider group."""
    def __init__(self, *, max_requests: int = 30, min_interval_seconds: float = 10.0,
                 window_seconds: int = 600):
        self.max_requests = max_requests
        self.min_interval_seconds = min_interval_seconds
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = {}

    def consume(self, provider: str, *, at: float) -> None:
        history = self._history.setdefault(provider, [])
        history[:] = [value for value in history if at - value < self.window_seconds]
        if history and at - history[-1] < self.min_interval_seconds:
            raise RuntimeError("rate_limited:minimum_interval")
        if len(history) >= self.max_requests:
            raise RuntimeError("rate_limited:window_budget")
        history.append(at)

    def acquire(self, provider: str) -> None:
        history = self._history.setdefault(provider, [])
        now = time.monotonic()
        if history:
            wait = self.min_interval_seconds - (now - history[-1])
            if wait > 0: time.sleep(wait)
        self.consume(provider, at=time.monotonic())


class FetchContext:
    """Pass to paginated adapters; every outbound request must call request()."""
    def __init__(self, budget: RequestBudget, scope: str):
        self.budget, self.scope = budget, scope
        self.request_count = 0

    def request(self) -> None:
        self.budget.acquire(self.scope)
        self.request_count += 1


def _parse_now(value: str | None) -> datetime:
    if value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _clean_symbol(value: Any) -> str:
    if value is None:
        raise ValueError("schema_invalid:symbol_missing")
    raw = str(value).strip()
    if raw.endswith(".0") and raw[:-2].isdigit(): raw = raw[:-2]
    if raw.isdigit() and len(raw) <= 6: return raw.zfill(6)
    if len(raw) == 8 and raw[:2] in {"sh", "sz", "bj", "SH", "SZ", "BJ"} and raw[2:].isdigit():
        return raw[2:]
    raise ValueError("schema_invalid:symbol_format")


def _clean_rows(spec: DatasetSpec, raw_rows: Any) -> list[dict[str, Any]]:
    if isinstance(raw_rows, Mapping):
        raw_rows = raw_rows.get("rows", [])
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise ValueError("schema_invalid:rows_not_sequence")
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping): continue
        row = dict(raw)
        symbol_key = next((key for key in ("symbol", "code", "stock_code") if row.get(key) not in (None, "")), None)
        if symbol_key:
            row["symbol"] = _clean_symbol(row[symbol_key])
        elif "symbol" in spec.required_fields:
            raise ValueError("schema_invalid:symbol_missing")
        for key in ("plate_code", "plateid"):
            if key in row and row[key] not in (None, ""):
                digits = re.sub(r"\D", "", str(row[key]))
                if not digits: raise ValueError("schema_invalid:plate_code")
                row["plate_code"] = digits
                break
        for key, value in tuple(row.items()):
            if isinstance(value, float) and not math.isfinite(value):
                row[key] = None
        rows.append(row)
    if spec.name == "symbol_universe":
        for row in rows:
            if any(row.get(key) in (None, "") for key in spec.required_fields):
                raise ValueError("schema_invalid:required_field_missing")
        symbols = [row["symbol"] for row in rows]
        if len(symbols) != len(set(symbols)):
            raise ValueError("quality_invalid:duplicate_symbol")
    if not rows and not spec.empty_is_valid:
        raise ValueError("quality_invalid:empty_response")
    return rows


def _quality(rows: list[dict[str, Any]], fetch_meta: Mapping[str, Any]) -> dict[str, Any]:
    expected = fetch_meta.get("expected_count")
    actual = fetch_meta.get("coverage_count", len(rows))
    coverage = actual / expected if isinstance(expected, (int, float)) and expected > 0 else None
    missing = int(max(0, expected - actual)) if isinstance(expected, (int, float)) else None
    partial = bool(fetch_meta.get("partial"))
    declared_status = fetch_meta.get("quality_status")
    if declared_status not in {"READY", "PARTIAL", "DEGRADED"}:
        declared_status = None
    if fetch_meta.get("sample_scope") == "symbols_file":
        partial = True
        declared_status = "PARTIAL"
    return {"coverage": coverage, "missing": missing, "row_count": len(rows),
            "status": declared_status or
                     ("PARTIAL" if partial or (coverage is not None and coverage < 1) else "READY"),
            "partial": partial}


_SECRET_KEY = re.compile(r"token|cookie|password|passwd|device.?id|authorization|secret|headers?|url", re.I)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _redact(item) for key, item in value.items() if not _SECRET_KEY.search(str(key))}
    if isinstance(value, list): return [_redact(item) for item in value]
    if isinstance(value, tuple): return [_redact(item) for item in value]
    return value


class JobRunner:
    def __init__(self, *, artifact_root: str | Path, redis: Any = None, td_adapter: Any = None,
                 budget: RequestBudget | None = None, breaker_hours: float = 6,
                 max_payload_bytes: int = 64 * 1024 * 1024):
        self.catalog = ArtifactCatalog(artifact_root)
        self.root = Path(artifact_root)
        self.redis = redis
        self.td_adapter = td_adapter
        self.budget = budget or RequestBudget()
        self.breaker_hours = breaker_hours
        self.max_payload_bytes = max_payload_bytes

    def _state(self) -> dict[str, Any]:
        path = self.root / "job_state.json"
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError): return {"breakers": {}, "status": {}}

    def _save_state(self, state: Mapping[str, Any]) -> None:
        from .storage import _atomic_write
        _atomic_write(self.root / "job_state.json", canonical_json(state) + b"\n")

    def _check_breaker(self, scope: str, now: datetime) -> None:
        entry = self._state().get("breakers", {}).get(scope)
        if entry and now < datetime.fromisoformat(entry["until"]):
            raise RuntimeError("circuit_open:provider_cooldown")

    def _open_breaker(self, scope: str, now: datetime, category: str) -> None:
        state = self._state()
        state.setdefault("breakers", {})[scope] = {
            "until": (now + timedelta(hours=self.breaker_hours)).isoformat(),
            "category": category,
        }
        self._save_state(state)

    def _record_failure(self, name: str, trade_date: str, category: str, now: datetime) -> None:
        state = self._state()
        state.setdefault("status", {})[name] = {"trade_date": trade_date,
            "failed_at": now.isoformat(), "status": "FAILED", "reason": category}
        self._save_state(state)

    def run(self, name: str, trade_date: str, *, fetch: Callable[[str], Any],
            compute: Callable[[list[dict[str, Any]]], Any] | None = None,
            now: str | None = None, publish: bool = False, td_write: bool = False,
            source: str | None = None, expected_count: int | None = None,
            as_of: str | None = None, available_at: str | None = None,
            source_event_at: str | None = None, algorithm_version: str = "1",
            dry_run: bool = False,
            baostock_policy: Mapping[str, Any] | None = None) -> JobResult:
        spec = get_dataset(name)
        day = datetime.strptime(trade_date, "%Y-%m-%d").date()
        clock = _parse_now(now)
        if as_of:
            as_of_day = datetime.strptime(as_of[:10], "%Y-%m-%d").date()
            if as_of_day > day and spec.point_in_time:
                raise ValueError("temporal_invalid:future_as_of")
        self._check_breaker(spec.fetch_budget_scope, clock)
        try:
            context = FetchContext(self.budget, spec.fetch_budget_scope)
            try:
                accepts_context = len(inspect.signature(fetch).parameters) >= 2
            except (TypeError, ValueError):
                accepts_context = False
            if accepts_context:
                raw = fetch(trade_date, context)
            else:
                context.request()
                raw = fetch(trade_date)
        except Exception as exc:
            message = str(exc).lower()
            if any(marker in message for marker in ("403", "forbidden", "anti-bot", "captcha")):
                if not dry_run:
                    self._open_breaker(spec.fetch_budget_scope, clock, "provider_blocked")
                    self._record_failure(name, trade_date, "provider_blocked", clock)
            elif not dry_run:
                self._record_failure(name, trade_date, f"fetch_failed:{type(exc).__name__}", clock)
            raise RuntimeError(f"fetch_failed:{type(exc).__name__}:{exc}") from None
        fetch_meta = dict(raw) if isinstance(raw, Mapping) else {}
        raw_rows = fetch_meta.get("rows", raw) if fetch_meta else raw
        response_marker = str(fetch_meta.get("status", "")).lower() + " " + str(fetch_meta.get("error", "")).lower()
        if fetch_meta.get("status_code") == 403 or any(word in response_marker for word in ("forbidden", "anti-bot", "captcha")):
            if not dry_run:
                self._open_breaker(spec.fetch_budget_scope, clock, "provider_blocked")
            raise RuntimeError("fetch_failed:provider_blocked")
        if context.request_count:
            fetch_meta.setdefault("request_count", context.request_count)
        if expected_count is not None: fetch_meta["expected_count"] = expected_count
        rows = _clean_rows(spec, _redact(raw_rows))
        computed = compute(rows) if compute else rows
        if isinstance(computed, Mapping):
            computed = computed.get("rows", rows)
        rows = _clean_rows(spec, _redact(computed))
        if name == "limit_reasons":
            if any(row.get("date") != trade_date for row in rows):
                raise ValueError("temporal_invalid:limit_reasons_date_mismatch")
            keys = [(row.get("date"), row.get("symbol")) for row in rows]
            if len(keys) != len(set(keys)):
                raise ValueError("quality_invalid:limit_reasons_duplicate_key")
        quality = _quality(rows, fetch_meta)
        baostock_completion = None
        if (name == "daily_kline" and fetch_meta.get("provider") == "baostock" and
                fetch_meta.get("sample_scope") != "symbols_file"):
            expected_symbols = fetch_meta.get("expected_symbols")
            if isinstance(expected_symbols, (list, tuple, set)):
                from .baostock_completion import classify_missing_symbols, evaluate_baostock_completion
                from .registry.daily_kline import BAOSTOCK_COMPLETION_POLICY
                policy = dict(BAOSTOCK_COMPLETION_POLICY)
                if baostock_policy is not None:
                    policy.update(baostock_policy)
                bar_symbols = {row.get("symbol") for row in rows if row.get("symbol")}
                source_symbols = fetch_meta.get("source_symbols")
                if not isinstance(source_symbols, (list, tuple, set)):
                    source_symbols = None
                known_missing = classify_missing_symbols(expected_symbols, bar_symbols, source_symbols)
                baostock_completion = evaluate_baostock_completion(
                    expected_symbols, bar_symbols, known_missing,
                    coverage_threshold=policy["formal_coverage_threshold"],
                    max_source_empty=policy["source_empty_max"])
                quality.update(baostock_completion["quality"])
                quality["classification_source"] = "baostock.query_all_stock"
                quality["source_universe_status"] = "READY" if source_symbols is not None else "UNKNOWN"
                quality["source_universe_symbol_count"] = len(source_symbols or ())
                if source_symbols is not None:
                    quality["source_universe_sha256"] = hashlib.sha256(
                        "\n".join(sorted({str(value).zfill(6) for value in source_symbols})).encode("ascii")
                    ).hexdigest()
                elif fetch_meta.get("source_universe_error"):
                    quality["source_universe_error"] = str(fetch_meta["source_universe_error"])
        if fetch_meta.get("sample_scope") == "symbols_file":
            quality["partial"] = True
            quality["status"] = "PARTIAL"
        digest_payload = b"\n".join(canonical_json(row) for row in rows)
        if len(digest_payload) > self.max_payload_bytes:
            raise ValueError("quality_invalid:artifact_payload_budget")
        fetched = (clock.astimezone(ZoneInfo("Asia/Shanghai")).isoformat() if name == "daily_kline"
                   else clock.isoformat().replace("+00:00", "Z"))
        declared_available_at = available_at or fetch_meta.get("available_at")
        provider_chain = None
        if name == "daily_kline":
            requested_source = source
            actual_source = fetch_meta.get("source")
            actual_provider = fetch_meta.get("provider")
            if actual_source not in {"baostock", "wencai", "q2_final", "legacy_q2_final"}:
                actual_source = ("legacy_q2_final" if actual_provider == "q2_final" else actual_provider)
            if actual_source in {"baostock", "wencai", "legacy_q2_final", "q2_final"}:
                source = str(actual_source)
            provider_chain = fetch_meta.get("provider_chain")
            if not isinstance(provider_chain, str) or not provider_chain:
                provider_chain = (requested_source if isinstance(requested_source, str)
                                  and requested_source else str(source or spec.source))
        safe_source = source if isinstance(source, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", source) else spec.source
        raw_digest = fetch_meta.get("raw_sha256")
        if not isinstance(raw_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", raw_digest):
            raw_digest = None
        manifest = {
            "schema_version": 1, "dataset": name, "trade_date": trade_date,
            "source": safe_source, "source_dependency": spec.source_dependency,
            "fetched_at": fetched, "declared_available_at": declared_available_at,
            "captured_at": fetched,
            "source_asof": as_of or fetch_meta.get("as_of") or "UNKNOWN",
            "as_of": as_of or fetch_meta.get("as_of") or trade_date, "source_event_at": source_event_at,
            "point_in_time": spec.point_in_time, "algorithm_version": algorithm_version,
            "quality": quality, "quality_checks": ["schema", "finite_numbers", "coverage"],
            "row_count": len(rows), "request_count": fetch_meta.get("request_count"),
            "raw_sha256": raw_digest,
        }
        if isinstance(fetch_meta.get("inputs"), Mapping):
            manifest["inputs"] = dict(fetch_meta["inputs"])
        if name == "limit_reasons":
            # The point-in-time cutoff is part of this dataset's identity. Include it in the
            # version inputs so a corrected timezone-aware as_of creates a new immutable version.
            version_inputs = dict(manifest.get("inputs", {}))
            version_inputs["point_in_time_as_of"] = str(as_of or fetch_meta.get("as_of") or trade_date)
            manifest["inputs"] = version_inputs
        provider_details = {key: fetch_meta[key] for key in
                            ("failed_plates", "completed_plate_count", "queried_plate_count",
                             "unqueried_plate_count", "universe_duplicate_conflicts",
                             "queried_symbol_count", "unqueried_symbol_count", "overwritten_symbol_count",
                             "universe_delta_count", "source_counts", "historical_attribution_count",
                             "identity_mismatch_count", "identity_mismatch_codes", "reason_unknown_count",
                             "all_sources_error", "kaipan_errors", "history_page_size") if key in fetch_meta}
        if provider_details:
            manifest["provider_details"] = provider_details
        if name == "limit_reasons":
            provider = str(fetch_meta.get("provider") or "UNKNOWN")
            safe_source = provider if re.fullmatch(r"[A-Za-z0-9_.()= -]{1,96}", provider) else spec.source
            source = safe_source
            manifest["source"] = safe_source
            manifest["provider"] = provider
            manifest["provider_chain"] = list(fetch_meta.get("provider_chain", ()))
            manifest["provider_attempts"] = list(fetch_meta.get("provider_attempts", ()))
            business_status = str(fetch_meta.get("business_status") or "FORMAL")
            manifest["business_status"] = "MISSING" if fetch_meta.get("all_sources_error") else business_status
            primary_providers = set(spec.primary_providers or ((spec.source,) if spec.source else ()))
            manifest["degraded"] = (business_status in {"DEGRADED", "PARTIAL"} or
                                     (bool(primary_providers) and provider not in primary_providers))
            manifest["field_units"] = {}
            quality.update({
                "expected_basis": "pool_members",
                "expected_count": int(fetch_meta.get("expected_count") or 0),
                "missing": int(fetch_meta.get("missing_count") or 0),
                "field_completeness": dict(fetch_meta.get("field_completeness") or {}),
                "partial": bool(fetch_meta.get("partial")),
                "dropped_duplicates": 0,
            })
            if fetch_meta.get("all_sources_error"):
                manifest.update({"mode": "MISSING", "storage_backend": None, "storage_uri": None,
                                 "manifest_uri": None, "rows_uri": None, "cache_publish": "SKIPPED"})
                return JobResult(manifest, rows)
        for key in ("carried_forward_from", "universe_asof", "tier", "degraded",
                    "sample_scope", "sample_symbols"):
            if key in fetch_meta:
                manifest[key] = fetch_meta[key]
        if name == "daily_kline":
            manifest["provider_chain"] = provider_chain
            provider = fetch_meta.get("provider")
            if provider not in {"baostock", "wencai", "q2_final"}:
                provider = "baostock" if isinstance(fetch_meta.get("baostock_timing"), Mapping) else (
                    source if source in {"baostock", "wencai", "q2_final"} else
                    "q2_final" if source == "legacy_q2_final" else "UNKNOWN")
            manifest["provider"] = provider
            attempts = fetch_meta.get("provider_attempts")
            timing = fetch_meta.get("baostock_timing")
            if isinstance(attempts, (list, tuple)):
                manifest["provider_attempts"] = list(attempts)
            elif isinstance(timing, Mapping):
                manifest["provider_attempts"] = [{
                    "provider": provider, "result": timing.get("result", "UNKNOWN"),
                    "attempted_at": timing.get("started_at"),
                    "elapsed_ms": fetch_meta.get("elapsed_ms"),
                    "reason": "baostock_fetch_timing",
                    "login_ms": timing.get("login_ms"), "query_ms": timing.get("query_ms"),
                    "query_count": timing.get("query_count"), "error_code": timing.get("error_code"),
                    "error_events": timing.get("error_events", []),
                }]
            else:
                manifest["provider_attempts"] = [{
                    "provider": provider, "result": "UNKNOWN", "attempted_at": None,
                    "elapsed_ms": None, "reason": "provider_attempts_not_returned_by_fetch",
                }]
            manifest["field_completeness"] = fetch_meta.get("field_completeness", {})
            manifest["business_status"] = ("PROVISIONAL" if fetch_meta.get("sample_scope") == "symbols_file"
                                           else baostock_completion["business_status"]
                                           if baostock_completion is not None
                                           else classify_daily_kline(provider, quality.get("coverage"),
                                                                     manifest["field_completeness"]))
            if baostock_completion is not None:
                policy = dict(BAOSTOCK_COMPLETION_POLICY)
                if baostock_policy is not None:
                    policy.update(baostock_policy)
                manifest["completion_policy"] = policy
        elif fetch_meta.get("partial"):
            manifest["business_status"] = "PARTIAL"
        if dry_run:
            manifest.update({"mode": "DRY_RUN", "storage_backend": None, "storage_uri": None,
                             "manifest_uri": None, "rows_uri": None, "cache_publish": "SKIPPED"})
            return JobResult(manifest, rows)
        # Durable artifact publication is the commit point. Redis/TD are later, optional tiers.
        committed = self.catalog.commit(name, trade_date, rows, manifest, source=safe_source)
        committed["cache_publish"] = "SKIPPED"
        if td_write:
            if self.td_adapter is None: raise RuntimeError("td_adapter_not_configured")
            if baostock_completion is not None and not baostock_completion["quality"].get("status") == "READY":
                committed["td_publish"] = "SKIPPED_PARTIAL"
            else:
                self.td_adapter.write_dataset(spec, trade_date, rows, committed)
                committed["td_publish"] = "READY"
        if publish:
            if self.redis is None: raise RuntimeError("redis_adapter_not_configured")
            self._publish_redis(spec, trade_date, committed, rows, safe_source)
            committed["cache_publish"] = "READY"
        state = self._state()
        state.setdefault("status", {})[name] = {"trade_date": trade_date,
            "available_at": committed["available_at"], "version": committed["version"],
            "quality": quality, "cache_publish": committed["cache_publish"]}
        self._save_state(state)
        return JobResult(committed, rows)

    def _publish_redis(self, spec: DatasetSpec, trade_date: str, manifest: Mapping[str, Any],
                       rows: list[dict[str, Any]], source: str) -> None:
        latest_key = f"core:cache:data:{spec.name}:latest"
        existing = _redis_get_json(self.redis, latest_key)
        existing_day = existing.get("trade_date") if existing else None
        payload = dict(manifest)
        encoded_rows = canonical_json(rows)
        is_latest_member_day = (spec.name != "plate_members" or existing_day is None or
                                trade_date >= existing_day)
        if spec.hot_view and is_latest_member_day and len(encoded_rows) <= spec.max_redis_payload_bytes:
            payload["rows"] = rows
        else:
            payload["rows"] = None
            payload["payload_omitted"] = "artifact_catalog"
        version_key = f"core:cache:data:{spec.name}:{trade_date}:{manifest['version']}"
        version_json = canonical_json(payload).decode("utf-8")
        date_key = f"core:cache:data:{spec.name}:{trade_date}"
        date_json = canonical_json({"version": manifest["version"],
            "manifest": manifest.get("manifest_uri"), "trade_date": trade_date}).decode()
        if (manifest.get("business_status") != "PROVISIONAL"
                and (existing_day is None or trade_date >= existing_day)):
            latest_json = canonical_json({"trade_date": trade_date,
                "version": manifest["version"], "manifest": manifest.get("manifest_uri")}).decode()
        else:
            latest_json = None
        status_key = "core:cache:data:status"
        status = _redis_get_json(self.redis, status_key) or {}
        status[spec.name] = {"trade_date": trade_date, "available_at": manifest["available_at"],
                             "version": manifest["version"], "quality": manifest["quality"]}
        # Keep status bounded and expire it like other coordination metadata.
        status = dict(list(status.items())[-32:])
        status_json = canonical_json(status).decode()
        plan = [(version_key, version_json, spec.redis_ttl_seconds),
                (date_key, date_json, spec.redis_ttl_seconds), (status_key, status_json, 7 * 86400)]
        if latest_json is not None:
            plan.append((latest_key, latest_json, spec.redis_ttl_seconds))
        limit = max(1024, int(spec.max_redis_payload_bytes))
        too_large = [(key, len(value.encode("utf-8"))) for key, value, _ in plan
                     if len(value.encode("utf-8")) > limit]
        print("WRITE_PLAN " + json.dumps({"keys": [{"key": key, "bytes": len(value.encode("utf-8")),
              "ttl_seconds": ttl} for key, value, ttl in plan], "max_value_bytes": limit}, sort_keys=True))
        if too_large:
            raise ValueError("redis_payload_budget_exceeded")
        originals = {}
        for key, _, _ in plan:
            previous = _redis_get(self.redis, key)
            ttl_fn = getattr(self.redis, "ttl", None)
            try: prior_ttl = ttl_fn(key) if callable(ttl_fn) else None
            except Exception: prior_ttl = None
            originals[key] = (previous, prior_ttl)
        written = []
        try:
            for key, value, ttl in plan:
                _redis_set(self.redis, key, value, ttl)
                written.append(key)
            verify_raw = _redis_get(self.redis, version_key)
            if isinstance(verify_raw, bytes): verify_raw = verify_raw.decode("utf-8")
            verify = json.loads(verify_raw) if verify_raw is not None else {}
            verify_rows = verify.get("rows")
            if verify.get("sha256") != manifest.get("sha256") or verify.get("row_count") != len(rows):
                raise RuntimeError("redis_readback_manifest_mismatch")
            if verify_rows is not None:
                verify_payload = b"\n".join(canonical_json(row) for row in verify_rows)
                if verify_rows: verify_payload += b"\n"
            else:
                rows_path = Path(str(verify.get("rows_uri") or manifest.get("rows_uri") or ""))
                if not rows_path.is_file(): raise RuntimeError("redis_readback_artifact_missing")
                opener = gzip.open if rows_path.suffix == ".gz" else open
                with opener(rows_path, "rb") as handle: verify_payload = handle.read()
            if len(verify_rows) != len(rows) if verify_rows is not None else int(verify.get("row_count", -1)) != len(rows):
                raise RuntimeError("redis_readback_row_count_mismatch")
            if hashlib.sha256(verify_payload).hexdigest() != manifest.get("sha256"):
                raise RuntimeError("redis_readback_sha256_mismatch")
        except Exception:
            for key in reversed(written):
                previous, prior_ttl = originals[key]
                if previous is None:
                    _redis_delete(self.redis, key)
                else:
                    ttl = prior_ttl if isinstance(prior_ttl, int) and prior_ttl > 0 else spec.redis_ttl_seconds
                    _redis_set(self.redis, key, previous, ttl)
            raise


def _redis_set(client: Any, key: str, value: str, ttl: int) -> None:
    try: client.set(key, value, ex=ttl)
    except TypeError: client.set(key, value, ttl=ttl)


def _redis_get(client: Any, key: str) -> Any:
    getter = getattr(client, "get", None)
    return getter(key) if callable(getter) else None


def _redis_delete(client: Any, key: str) -> None:
    deleter = getattr(client, "delete", None)
    if callable(deleter): deleter(key)


def _redis_get_json(client: Any, key: str) -> dict[str, Any] | None:
    raw = client.get(key)
    if raw is None: return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError): return None
