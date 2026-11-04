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


def redis_configured() -> bool:
    """True when throttle/breaker state should live in Redis. ``CORE_PROVIDER_STATE=memory`` keeps it in the process
    (offline tests and replays, which have no Redis to throttle against)."""
    if os.getenv("CORE_PROVIDER_STATE", "").lower() == "memory":
        return False
    return bool(os.getenv("CORE_REDIS_URL") or os.getenv("REDIS_URL"))


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
        name = self._key(key)
        try:
            raw = self._redis().get(name)
        except StateUnavailable:
            raise
        except Exception as exc:
            raise StateUnavailable(type(exc).__name__) from exc
        return None if raw is None else json.loads(raw)

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("state_ttl_must_be_positive")
        name = self._key(key)
        try:
            self._redis().set(name, json.dumps(value, sort_keys=True, separators=(",", ":")), ex=ttl_seconds)
        except StateUnavailable:
            raise
        except Exception as exc:
            raise StateUnavailable(type(exc).__name__) from exc

    def claim(self, key: str, value: Any, ttl_seconds: int) -> bool:
        """Set ``key`` only when it does not exist (SET NX EX); True when this caller now owns it."""
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("state_ttl_must_be_positive")
        name = self._key(key)
        try:
            return bool(self._redis().set(name, json.dumps(value, sort_keys=True, separators=(",", ":")),
                                          nx=True, ex=ttl_seconds))
        except StateUnavailable:
            raise
        except Exception as exc:
            raise StateUnavailable(type(exc).__name__) from exc

    def delete(self, key: str) -> None:
        name = self._key(key)
        try:
            self._redis().delete(name)
        except StateUnavailable:
            raise
        except Exception as exc:
            raise StateUnavailable(type(exc).__name__) from exc


class ProviderState:
    """Throttle / circuit-breaker state of one external provider: ``core:provider:<name>:state`` with a 2-day TTL."""

    TTL_SECONDS = 2 * 86400

    def __init__(self, name: str, store: StateStore | None = None):
        self.key = f"core:provider:{name}:state"
        self._store = store

    @property
    def store(self) -> StateStore:
        if self._store is None:
            self._store = StateStore()
        return self._store

    def load(self) -> dict[str, Any]:
        value = self.store.get(self.key)
        return value if isinstance(value, dict) else {}

    def save(self, value: Any) -> None:
        self.store.set(self.key, dict(value), self.TTL_SECONDS)
