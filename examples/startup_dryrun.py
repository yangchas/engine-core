"""Read-only client guards for the unfinished guardian startup rehearsal.

This module is not a startup rehearsal: the guards are not yet installed into
all guardian subprocesses. Do not run a startup with these guards alone.
"""
from __future__ import annotations

import re
import os
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def protect_data_root(root, violations):
    """Reject Python-audited filesystem mutations targeting the data root.

    This is an in-process guard, not an OS sandbox. Native extensions and
    subprocesses require separate isolation before a full rehearsal is safe.
    """
    protected = Path(root).resolve()
    active = [True]
    mutations = {
        "os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,),
        "os.rename": (0, 1), "os.link": (0, 1), "os.symlink": (1,),
        "os.chmod": (0,), "os.chown": (0,), "os.utime": (0,),
        "os.truncate": (0,),
    }

    def guarded_path(value):
        if isinstance(value, int):
            return True
        try:
            path = Path(os.fsdecode(value))
            # Relative paths can refer to an arbitrary dir_fd; fail closed.
            if not path.is_absolute():
                return True
            return path.resolve().is_relative_to(protected)
        except (TypeError, ValueError, OSError):
            return True

    def audit(event, args):
        if not active[0]:
            return
        if event == "open":
            mode, flags = args[1:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
            paths = (args[0],) if writing else ()
        else:
            paths = tuple(args[index] for index in mutations.get(event, ()))
        if any(guarded_path(path) for path in paths):
            violations.append({"backend": "filesystem", "command": event})
            raise PermissionError("data_root_write_denied")

    sys.addaudithook(audit)
    try:
        yield
    finally:
        active[0] = False


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
