"""Read-only client guards for the unfinished guardian startup rehearsal.

This module is not a startup rehearsal: the guards are not yet installed into
all guardian subprocesses. Do not run a startup with these guards alone.
"""
from __future__ import annotations

import re


class ReadOnlyRedis:
    """Deny everything except explicitly listed, side-effect-free commands."""

    _READS = frozenset({"GET", "MGET", "EXISTS", "HGET", "HMGET", "HGETALL",
                        "HEXISTS", "HLEN", "HKEYS", "HVALS", "SCAN", "HSCAN",
                        "SSCAN", "ZSCAN", "SMEMBERS", "SISMEMBER", "SCARD",
                        "ZRANGE", "ZRANGEBYSCORE", "ZCARD", "TYPE", "TTL",
                        "PTTL", "PING"})

    def __init__(self, client, violations):
        self._client = client
        self._violations = violations

    def execute_command(self, *args, **kwargs):
        raw = args[0] if args else ""
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", "replace")
        command = str(raw).upper()
        if command not in self._READS:
            self._violations.append({"backend": "redis", "command": command})
            raise PermissionError("redis_command_denied")
        return self._client.execute_command(*args, **kwargs)

    def get(self, key):
        return self.execute_command("GET", key)

    def close(self):
        return self._client.close()


class ReadOnlyTD:
    """Allow only single, read-only TD statements through a cursor."""

    _STATEMENT = re.compile(r"^\s*(SELECT|SHOW|DESCRIBE)\b", re.IGNORECASE)
    _UNSAFE = re.compile(r"(?:;|--|/\*|\*/|\bINTO\b)", re.IGNORECASE)

    def __init__(self, cursor, violations):
        self._cursor = cursor
        self._violations = violations

    def execute(self, sql, *args, **kwargs):
        if not isinstance(sql, str) or not self._STATEMENT.match(sql) or self._UNSAFE.search(sql):
            self._violations.append({"backend": "td", "command": "DENIED"})
            raise PermissionError("td_statement_denied")
        return self._cursor.execute(sql, *args, **kwargs)

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        return self._cursor.close()


if __name__ == "__main__":
    raise SystemExit("startup_dryrun is incomplete: subprocess and filesystem guards are not installed")
