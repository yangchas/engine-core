"""Single-flight lease on Redis so overlapping triggers of one data function never run in parallel."""
from __future__ import annotations

import os
import uuid
from typing import Any

_RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class RedisLease:
    """SET NX EX acquire, owner-checked renew/release. A crashed holder's lease simply expires."""

    def __init__(self, client: Any, key: str, *, owner: str | None = None, ttl_seconds: int = 300):
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
        self.held = bool(self.client.eval(_RENEW, 1, self.key, self.owner, self.ttl_seconds))
        return self.held

    def release(self) -> None:
        if self.held:
            self.client.eval(_RELEASE, 1, self.key, self.owner)
            self.held = False
