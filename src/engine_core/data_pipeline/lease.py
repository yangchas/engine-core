"""Single-flight lease on Redis so overlapping triggers of one data function never run in parallel."""
from __future__ import annotations

import os
import uuid
from typing import Any

LEASE_KEY_PREFIX = "core:lease:"  # the Redis guard proxy only allows delete-class commands on core: keys, and no EVAL


class RedisLease:
    """SET NX EX acquire, owner-checked renew/release. A crashed holder's lease simply expires.

    Plain commands only (the production Redis sits behind a guard that rejects EVAL), so renew/release are
    check-then-act; the window is tiny against a TTL of minutes and a renewal after every symbol."""

    def __init__(self, client: Any, key: str, *, owner: str | None = None, ttl_seconds: int = 300):
        if not key.startswith(LEASE_KEY_PREFIX):
            raise ValueError("lease_key_must_start_with_" + LEASE_KEY_PREFIX)
        if ttl_seconds < 1:
            raise ValueError("lease_ttl_must_be_positive")
        self.client = client
        self.key = key
        self.owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.ttl_seconds = int(ttl_seconds)
        self.held = False

    def acquire(self) -> bool:
        self.held = bool(self.client.set(self.key, self.owner, nx=True, ex=self.ttl_seconds))
        return self.held

    def renew(self) -> bool:
        """False once the lease expired or was taken over; the holder must stop."""
        if not self.held:
            return False
        if self._current() != self.owner:
            self.held = False
            return False
        self.held = bool(self.client.expire(self.key, self.ttl_seconds))
        return self.held

    def release(self) -> None:
        if self.held and self._current() == self.owner:
            self.client.delete(self.key)
        self.held = False

    def _current(self) -> str | None:
        value = self.client.get(self.key)
        return value.decode() if isinstance(value, bytes) else value
