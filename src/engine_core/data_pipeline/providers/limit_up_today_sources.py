"""Read-only Q2 input and strictly low-frequency WenCai confirmation for C582."""
from __future__ import annotations

import json
import os
import time
import fcntl
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .wencai_daily_adapter import DEFAULT_COOKIE_FILE

SHANGHAI = ZoneInfo("Asia/Shanghai")
WENCAI_PAGE_SIZE = 100
WENCAI_MIN_INTERVAL_SECONDS = 1800
WENCAI_MAX_REQUESTS_PER_DAY = 1


class SourceUnavailable(RuntimeError):
    pass


class WencaiThrottled(RuntimeError):
    pass


def read_q2_snapshot(trade_date: str, *, redis_url: str | None = None, client: Any = None) -> dict[str, Any]:
    """Read exactly one active set and its Q2 hashes; this function has no Redis write/scan path."""
    day = date.fromisoformat(trade_date).strftime("%Y%m%d")
    owned = client is None
    if client is None:
        url = redis_url or os.environ.get("REDIS_URL")
        if not url:
            raise SourceUnavailable("redis_url_unavailable")
        try:
            import redis
            client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)
        except Exception as exc:  # noqa: BLE001 - never include URL or credential text
            raise SourceUnavailable("redis_client_unavailable:" + type(exc).__name__) from None
    key = f"q2:active:{day}"
    try:
        if not client.exists(key):
            raise SourceUnavailable("q2_active_set_missing")
        members = sorted(str(item) for item in client.smembers(key))
        if not members:
            raise SourceUnavailable("q2_active_set_empty")
        pipe = client.pipeline(transaction=False)
        for symbol in members:
            pipe.hgetall(f"q2:{symbol}")
        fetched = pipe.execute()
    except SourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - never include Redis URL/response payload
        raise SourceUnavailable("q2_read_failed:" + type(exc).__name__) from None
    finally:
        if owned:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    return {"trade_date": date.fromisoformat(trade_date).isoformat(), "active_symbols": members,
            "hashes": {symbol: (fetched[index] if isinstance(fetched[index], Mapping) else {})
                       for index, symbol in enumerate(members)}}


def wencai_question(trade_date: str) -> str:
    day = date.fromisoformat(trade_date).isoformat()
    return f"{day}涨停;封单额;开板次数;首次涨停时间;最终涨停时间;去除st"


def authorize_wencai_request(root: str | Path, trade_date: str, now: datetime, *,
                             min_interval_seconds: int = WENCAI_MIN_INTERVAL_SECONDS,
                             max_requests_per_day: int = WENCAI_MAX_REQUESTS_PER_DAY) -> dict[str, Any]:
    """Persist the per-date budget before an upstream request, including failed requests."""
    if type(min_interval_seconds) is not int or min_interval_seconds < WENCAI_MIN_INTERVAL_SECONDS:
        raise ValueError("wencai_min_interval_must_be_at_least_1800_seconds")
    if type(max_requests_per_day) is not int or max_requests_per_day != WENCAI_MAX_REQUESTS_PER_DAY:
        raise ValueError("wencai_max_requests_per_day_must_equal_one")
    day = date.fromisoformat(trade_date).isoformat()
    current = now.astimezone(SHANGHAI) if now.tzinfo else now.replace(tzinfo=SHANGHAI)
    state_dir = Path(root).expanduser() / "control" / "limit_up_pool_today_wencai"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{day}.json"
    lock_path = state_dir / f"{day}.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {"trade_date": day, "requests": []}
            requests = state.get("requests") if isinstance(state, dict) else None
            if not isinstance(requests, list):
                requests = []
            if len(requests) >= max_requests_per_day:
                raise WencaiThrottled("wencai_daily_request_limit_reached")
            if requests:
                previous = datetime.fromisoformat(str(requests[-1]).replace("Z", "+00:00"))
                if previous.tzinfo is None:
                    previous = previous.replace(tzinfo=SHANGHAI)
                if (current - previous.astimezone(SHANGHAI)).total_seconds() < min_interval_seconds:
                    raise WencaiThrottled("wencai_minimum_interval_not_elapsed")
            requests.append(current.isoformat(timespec="seconds"))
            body = json.dumps({"trade_date": day, "requests": requests}, separators=(",", ":")) + "\n"
            temp_path = state_path.with_suffix(".tmp")
            temp_path.write_text(body, encoding="utf-8")
            os.replace(temp_path, state_path)
            return {"trade_date": day, "request_count": len(requests),
                    "last_request_at": requests[-1], "min_interval_seconds": min_interval_seconds}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def fetch_wencai_today(trade_date: str, *, cookie_file: str | os.PathLike[str] | None = DEFAULT_COOKIE_FILE,
                       query_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Fetch at most one 100-row page; no retry/cooldown loop is permitted for this daily confirmation."""
    from .wencai_daily_adapter import _FORBIDDEN, _Query, _Run, _fetch_query, _read_cookie

    if query_fn is None:
        from pywencai.wencai import get_robot_data
        query_fn = get_robot_data
    question = wencai_question(trade_date)
    run = _Run(query_fn=query_fn, context=None, page_size=WENCAI_PAGE_SIZE,
               cookie=_read_cookie(cookie_file), cooldown_seconds=0, cooldown_factor=1,
               cooldown_max_seconds=0, max_cooldowns=0, max_requests=1,
               sleep_fn=lambda _seconds: None, clock_fn=time.monotonic,
               wall_budget_seconds=None, checkpoint_path=None, trade_date=trade_date, question=question)
    try:
        result = _fetch_query(run, question, unit_prefix="today_pool", max_pages=1, first_unit=True)
    except Exception as exc:  # noqa: BLE001 - redact cookie and avoid leaking provider body
        text = str(exc)
        if run.cookie:
            text = text.replace(run.cookie, "<cookie>")
        code = "WENCAI_BLOCKED" if _FORBIDDEN.search(text) else "WENCAI_REQUEST_FAILED"
        raise SourceUnavailable(code + ":" + type(exc).__name__) from None
    if not result.rows:
        raise SourceUnavailable("wencai_response_empty")
    return {"records": result.rows, "reported_total": result.total, "request_count": run.requests,
            "complete": result.complete, "truncated": result.truncated or not result.complete,
            "question": question}


def publish_hot_snapshot(manifest: Mapping[str, Any], rows: list[dict[str, Any]], *, client: Any = None
                         ) -> dict[str, Any]:
    """Publish an immutable Core Redis view; never address legacy Q2 keys."""
    from ..registry import get_dataset
    from ..storage import canonical_json

    spec = get_dataset("limit_up_pool_today")
    day = date.fromisoformat(str(manifest["trade_date"])).isoformat()
    as_of = str(manifest["as_of"])
    version = str(manifest["version"])
    namespace = f"core:cache:data:{spec.name}"
    version_key = f"{namespace}:{day}:{version}"
    date_key = f"{namespace}:{day}"
    latest_key = f"{namespace}:latest"
    payload = {**dict(manifest), "rows": rows}
    version_json = canonical_json(payload).decode("utf-8")
    if len(version_json.encode("utf-8")) > spec.max_redis_payload_bytes:
        raise ValueError("redis_payload_budget_exceeded")

    owned = client is None
    if client is None:
        url = os.environ.get("CORE_REDIS_URL") or os.environ.get("REDIS_URL")
        if not url:
            raise SourceUnavailable("core_redis_url_unavailable")
        try:
            import redis
            client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)
        except Exception as exc:  # noqa: BLE001 - do not include URL or credential text
            raise SourceUnavailable("core_redis_client_unavailable:" + type(exc).__name__) from None
    try:
        def _json_at(key: str) -> dict[str, Any]:
            raw = client.get(key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                value = json.loads(raw) if raw is not None else {}
            except (TypeError, ValueError):
                value = {}
            return value if isinstance(value, dict) else {}

        def _newer(candidate: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
            candidate_day = str(candidate.get("trade_date") or "")
            current_day = str(current.get("trade_date") or "")
            if candidate_day != current_day:
                return candidate_day > current_day
            return str(candidate.get("as_of") or "") >= str(current.get("as_of") or "")

        date_pointer = _json_at(date_key)
        if date_pointer and not _newer({"trade_date": day, "as_of": as_of}, date_pointer):
            return {"status": "superseded", "key": date_key, "ttl_seconds": spec.redis_ttl_seconds}
        date_json = canonical_json({"trade_date": day, "version": version,
                                    "manifest": manifest.get("manifest_uri"), "as_of": as_of}).decode("utf-8")
        plan = [(version_key, version_json), (date_key, date_json)]
        if str(manifest.get("business_status")) == "FORMAL":
            latest = _json_at(latest_key)
            candidate = {"trade_date": day, "as_of": as_of}
            if not latest or _newer(candidate, latest):
                latest_json = canonical_json({"trade_date": day, "version": version,
                                              "manifest": manifest.get("manifest_uri"),
                                              "as_of": as_of}).decode("utf-8")
                plan.append((latest_key, latest_json))
        pipe_factory = getattr(client, "pipeline", None)
        if callable(pipe_factory):
            pipe = pipe_factory(transaction=True)
            for key, value in plan:
                pipe.set(key, value, ex=spec.redis_ttl_seconds)
            pipe.execute()
        else:
            for key, value in plan:
                client.set(key, value, ex=spec.redis_ttl_seconds)
        return {"status": "published", "keys": [key for key, _ in plan],
                "ttl_seconds": spec.redis_ttl_seconds}
    except Exception as exc:  # noqa: BLE001 - callers keep artifact commit as the durable record
        raise SourceUnavailable("core_redis_publish_failed:" + type(exc).__name__) from None
    finally:
        if owned:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
