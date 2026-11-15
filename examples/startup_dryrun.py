"""Run the guardian boot planning chain with in-process read-only guards."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from datetime import datetime
from unittest.mock import patch
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

    def ping(self):
        return self.execute_command("PING")

    def set(self, *args, **kwargs):
        return self.execute_command("SET", *args, **kwargs)

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


class ReadOnlyConnection:
    def __init__(self, connection, violations):
        self._connection = connection
        self._violations = violations

    def cursor(self):
        return ReadOnlyTD(self._connection.cursor(), self._violations)

    def close(self):
        return self._connection.close()


@contextmanager
def guarded_clients(violations):
    import redis
    from engine_core.data_pipeline import td_sink

    redis_from_url = redis.Redis.from_url
    connect_td = td_sink.connect_td
    with patch.object(redis.Redis, "from_url", side_effect=lambda *a, **kw:
                      ReadOnlyRedis(redis_from_url(*a, **kw), violations)), \
         patch.object(td_sink, "connect_td", side_effect=lambda *a, **kw:
                      ReadOnlyConnection(connect_td(*a, **kw), violations)):
        yield


def rehearse(root, trade_date, now, *, protected_root=None):
    from engine_core.guardian import startup
    from engine_core.guardian.scheduler import phase_at
    from engine_core.guardian import daily_kline_coverage

    violations = []
    clock = datetime.fromisoformat(f"{trade_date}T{now}:00+08:00")
    with patch.object(daily_kline_coverage, "note_td_readability", return_value=None), \
         guarded_clients(violations), protect_data_root(protected_root or root, violations), \
         protect_data_root(root, violations):
        trade = startup.boot_trade_day(trade_date, root)
        detail = trade.get("detail") or {}
        try:
            health = startup.boot_data_health(trade_date, detail.get("prev"), root, now=clock, dry_run=True)
        except PermissionError as exc:
            health = {"status": "failed", "error_code": str(exc),
                      "detail": {"status": "failed"}}
        phase = (phase_at(clock) if detail.get("is_trading_day") is True else
                 "NON_TRADING" if detail.get("is_trading_day") is False else "UNKNOWN")
        reconcile = startup.boot_reconcile(trade_date, phase, health.get("detail"), root, now=clock)
    rows = (reconcile.get("detail") or {}).get("plan", [])
    return {"boot": {"boot_trade_day": trade, "boot_data_health": health,
                     "boot_reconcile": reconcile},
            "plan": [{"item": row.get("id"), "action": row.get("action"),
                      "reason": row.get("reason") or row.get("error")} for row in rows],
            "violations": violations}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--now", required=True, help="HH:MM in Asia/Shanghai")
    parser.add_argument("--root", default=os.environ.get("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--without-catalog", action="store_true")
    parser.add_argument("--copy-dir", default=None, help="directory for the catalog-free root")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    result = rehearse(root, args.date, args.now)
    if args.without_catalog:
        evidence = Path(args.copy_dir).expanduser() if args.copy_dir else Path(__file__).resolve().parents[1] / "fw" / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        copy = Path(tempfile.mkdtemp(prefix="startup-no-catalog-", dir=evidence))
        for child in root.iterdir():
            if child.name != "catalog.json":
                (copy / child.name).symlink_to(child, target_is_directory=child.is_dir())
        other = rehearse(copy, args.date, args.now, protected_root=root)
        result = {"with_catalog": result, "without_catalog": other,
                  "plan_difference": {"with_catalog": result["plan"], "without_catalog": other["plan"]}}
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
