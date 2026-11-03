"""Small operational state (idempotency markers, throttle state, breakers) kept in Redis, never in files (design/31).

Keys live under ``core:`` (the Redis guard proxy rejects anything else) and always carry a TTL, so state cannot pile up.
Every call may raise ``StateUnavailable``; callers decide whether that is retryable (it always is for the guardian).
"""
from __future__ import annotations

import json
import os
from typing import Any

PREFIX = "core:"


class StateUnavailable(RuntimeError):
    """Redis is not configured or not reachable."""


def redis_from_env() -> Any | None:
    url = os.getenv("CORE_REDIS_URL") or os.getenv("REDIS_URL")
    if not url:
        return None
    import redis
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)


class StateStore:
    def __init__(self, client: Any | None = None):
        self._client = client if client is not None else redis_from_env()

    def _redis(self) -> Any:
        if self._client is None:
            raise StateUnavailable("redis_not_configured")
        return self._client

    @staticmethod
    def _key(key: str) -> str:
        if not key.startswith(PREFIX):
            raise ValueError("state_key_must_start_with_core:")
        return key

    def get(self, key: str) -> Any | None:
        try:
            raw = self._redis().get(self._key(key))
        except StateUnavailable:
            raise
        except Exception as exc:
            raise StateUnavailable(type(exc).__name__) from exc
        return None if raw is None else json.loads(raw)

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("state_ttl_must_be_positive")
        try:
            self._redis().set(self._key(key), json.dumps(value, sort_keys=True, separators=(",", ":")), ex=ttl_seconds)
        except StateUnavailable:
            raise
        except Exception as exc:
            raise StateUnavailable(type(exc).__name__) from exc

    def claim(self, key: str, value: Any, ttl_seconds: int) -> bool:
        """Set ``key`` only when it does not exist (SET NX EX); True when this caller now owns it."""
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("state_ttl_must_be_positive")
        try:
            return bool(self._redis().set(self._key(key), json.dumps(value, sort_keys=True, separators=(",", ":")),
                                          nx=True, ex=ttl_seconds))
        except StateUnavailable:
            raise
        except Exception as exc:
            raise StateUnavailable(type(exc).__name__) from exc

    def delete(self, key: str) -> None:
        try:
            self._redis().delete(self._key(key))
        except StateUnavailable:
            raise
        except Exception as exc:
            raise StateUnavailable(type(exc).__name__) from exc
