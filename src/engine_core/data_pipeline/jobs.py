"""Fetch/clean/validate/compute/artifact-publish jobs with injected providers."""
from __future__ import annotations

import json
import inspect
import math
import re
import time
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .registry import DatasetSpec, get_dataset
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
    if not rows and not spec.empty_is_valid:
        raise ValueError("quality_invalid:empty_response")
    return rows


def _quality(rows: list[dict[str, Any]], fetch_meta: Mapping[str, Any]) -> dict[str, Any]:
    expected = fetch_meta.get("expected_count")
    actual = len(rows)
    coverage = actual / expected if isinstance(expected, (int, float)) and expected > 0 else None
    missing = int(max(0, expected - actual)) if isinstance(expected, (int, float)) else None
    return {"coverage": coverage, "missing": missing, "row_count": actual,
            "status": "PARTIAL" if coverage is not None and coverage < 1 else "READY"}


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
            source_event_at: str | None = None, algorithm_version: str = "1") -> JobResult:
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
                self._open_breaker(spec.fetch_budget_scope, clock, "provider_blocked")
                self._record_failure(name, trade_date, "provider_blocked", clock)
            else:
                self._record_failure(name, trade_date, f"fetch_failed:{type(exc).__name__}", clock)
            raise RuntimeError(f"fetch_failed:{type(exc).__name__}") from None
        fetch_meta = dict(raw) if isinstance(raw, Mapping) else {}
        raw_rows = fetch_meta.get("rows", raw) if fetch_meta else raw
        response_marker = str(fetch_meta.get("status", "")).lower() + " " + str(fetch_meta.get("error", "")).lower()
        if fetch_meta.get("status_code") == 403 or any(word in response_marker for word in ("forbidden", "anti-bot", "captcha")):
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
        quality = _quality(rows, fetch_meta)
        digest_payload = b"\n".join(canonical_json(row) for row in rows)
        if len(digest_payload) > self.max_payload_bytes:
            raise ValueError("quality_invalid:artifact_payload_budget")
        fetched = available_at or clock.isoformat().replace("+00:00", "Z")
        safe_source = source if isinstance(source, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", source) else spec.source
        raw_digest = fetch_meta.get("raw_sha256")
        if not isinstance(raw_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", raw_digest):
            raw_digest = None
        manifest = {
            "schema_version": 1, "dataset": name, "trade_date": trade_date,
            "source": safe_source, "source_dependency": spec.source_dependency,
            "fetched_at": fetched, "available_at": available_at or fetched,
            "as_of": as_of or trade_date, "source_event_at": source_event_at,
            "point_in_time": spec.point_in_time, "algorithm_version": algorithm_version,
            "quality": quality, "quality_checks": ["schema", "finite_numbers", "coverage"],
            "row_count": len(rows), "request_count": fetch_meta.get("request_count"),
            "raw_sha256": raw_digest,
        }
        if name == "daily_kline":
            shanghai_now = clock.astimezone(ZoneInfo("Asia/Shanghai"))
            manifest["business_status"] = ("FORMAL" if shanghai_now.strftime("%H:%M") >= "17:40"
                                           else "PROVISIONAL")
        # Durable artifact publication is the commit point. Redis/TD are later, optional tiers.
        committed = self.catalog.commit(name, trade_date, rows, manifest, source=safe_source)
        committed["cache_publish"] = "SKIPPED"
        if td_write:
            if self.td_adapter is None: raise RuntimeError("td_adapter_not_configured")
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
        payload = dict(manifest)
        encoded_rows = canonical_json(rows)
        if spec.hot_view and len(encoded_rows) <= spec.max_redis_payload_bytes:
            payload["rows"] = rows
        else:
            payload["rows"] = None
            payload["payload_omitted"] = "artifact_catalog"
        version_key = f"core:cache:data:{spec.name}:{trade_date}:{manifest['version']}"
        version_json = canonical_json(payload).decode("utf-8")
        _redis_set(self.redis, version_key, version_json, spec.redis_ttl_seconds)
        date_key = f"core:cache:data:{spec.name}:{trade_date}"
        _redis_set(self.redis, date_key, canonical_json({"version": manifest["version"],
            "manifest": manifest.get("manifest_uri"), "trade_date": trade_date}).decode(), spec.redis_ttl_seconds)
        latest_key = f"core:cache:data:{spec.name}:latest"
        existing = _redis_get_json(self.redis, latest_key)
        existing_day = existing.get("trade_date") if existing else None
        if (manifest.get("business_status") != "PROVISIONAL"
                and (existing_day is None or trade_date >= existing_day)):
            _redis_set(self.redis, latest_key, canonical_json({"trade_date": trade_date,
                "version": manifest["version"], "manifest": manifest.get("manifest_uri")}).decode(),
                spec.redis_ttl_seconds)
        status_key = "core:cache:data:status"
        status = _redis_get_json(self.redis, status_key) or {}
        status[spec.name] = {"trade_date": trade_date, "available_at": manifest["available_at"],
                             "version": manifest["version"], "quality": manifest["quality"]}
        # Keep status bounded and expire it like other coordination metadata.
        status = dict(list(status.items())[-32:])
        _redis_set(self.redis, status_key, canonical_json(status).decode(), 7 * 86400)


def _redis_set(client: Any, key: str, value: str, ttl: int) -> None:
    try: client.set(key, value, ex=ttl)
    except TypeError: client.set(key, value, ttl=ttl)


def _redis_get_json(client: Any, key: str) -> dict[str, Any] | None:
    raw = client.get(key)
    if raw is None: return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError): return None
