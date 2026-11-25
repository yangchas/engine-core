"""In-memory stand-in for the few Redis commands the state code uses (a behavioural double: it holds no data of its own).

``MemoryRedis(down=True)`` raises ConnectionError on every call (an unreachable Redis). Keys keep the TTL they were given.
"""
from __future__ import annotations

import fnmatch


class MemoryRedis:
    def __init__(self, down: bool = False):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.down = down

    def _check(self):
        if self.down:
            raise ConnectionError("redis is down")

    def get(self, name):
        self._check()
        return self.values.get(name)

    def set(self, name, value, ex=None, nx=False):
        self._check()
        if nx and name in self.values:
            return None
        self.values[name] = value
        if ex is not None:
            self.ttls[name] = ex
        else:
            self.ttls.pop(name, None)
        return True

    def delete(self, *names):
        self._check()
        removed = 0
        for name in names:
            removed += 1 if self.values.pop(name, None) is not None else 0
            self.ttls.pop(name, None)
        return removed

    def ttl(self, name):
        self._check()
        return self.ttls.get(name, -1) if name in self.values else -2

    def scan_iter(self, match="*", count=None):
        self._check()
        return iter([key for key in list(self.values) if fnmatch.fnmatch(key, match)])
