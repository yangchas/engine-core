#!/usr/bin/env python3
"""Guardian handler for whole-day replays (examples/replay_day.py, C665).

Runs the real examples/guardian_handler.py with every upstream replaced:
  * the Kaipan SDK (``pykaipan.pykaipan``) answers from recorded raw responses in tests/fixtures/real, matched on the
    request's date/level/plate/symbol; anything else raises UnrecordedRequest;
  * every internet-protocol connection is a recorded gap (network.connect / network.resolve);
  * ``baostock`` / ``pywencai`` / ``redis`` / ``taos`` are stand-ins. Redis provides a shared in-memory
    Kaipan gap ledger and mail deduplication; unsupported calls remain recorded gaps and fail as unreachable sources.
Every request that could not be answered is appended, with the instance that made it, to $CORE_REPLAY_DIR/unrecorded.jsonl and every reply the handler
produced to $CORE_REPLAY_DIR/replies.jsonl, so the report can list gaps instead of hiding them.
"""
from __future__ import annotations

import fnmatch
import glob
import io
import json
import threading
from multiprocessing.managers import BaseManager, DictProxy
import os
import re
import sys
import time
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real"
REPLAY_DIR = Path(os.environ.get("CORE_REPLAY_DIR", "."))
_CONTEXT = {"instance": ""}


class UnrecordedRequest(RuntimeError):
    """The replayed day has no recording for this upstream request."""


def _append(name: str, row: dict) -> None:
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    with (REPLAY_DIR / name).open("a", encoding="utf-8") as handle:
        instance = _CONTEXT["instance"] or os.environ.get("CORE_REPLAY_INSTANCE", "")
        handle.write(json.dumps({"at": time.time(), "instance": instance, **row},
                                ensure_ascii=False, default=str) + "\n")


def _unrecorded(source: str, call: str, **args) -> UnrecordedRequest:
    _append("unrecorded.jsonl", {"source": source, "call": call, "args": args})
    return UnrecordedRequest(f"UNRECORDED_REQUEST:{source}.{call}:{json.dumps(args, ensure_ascii=False, default=str)}")


def _latest(pattern: str):
    found = sorted(glob.glob(str(FIXTURES / pattern)))
    return json.loads(Path(found[-1]).read_text(encoding="utf-8")) if found else None


def _arg(args, kwargs, name, position):
    if name in kwargs:
        return kwargs[name]
    return args[position] if len(args) > position else None


class ReplayKaipan(types.ModuleType):
    """Recorded raw Kaipan responses; the request must match the recording's date and key."""

    config: dict = {}

    def __init__(self):
        super().__init__("pykaipan.pykaipan")
        self.config = {}

    def getHisBans(self, *args, **kwargs):
        day, level = _arg(args, kwargs, "date", 0), _arg(args, kwargs, "ban", 1)
        hit = _latest(f"kaipan_pool_{day}_L{level}_*.json")
        if hit is None:
            raise _unrecorded("kaipan", "getHisBans", date=day, ban=level)
        return hit

    def getHisPlates(self, *args, **kwargs):
        day = _arg(args, kwargs, "date", 0)
        hit = _latest(f"kaipan_hot_plates_{day}_*.json")
        if hit is None:
            raise _unrecorded("kaipan", "getHisPlates", date=day)
        return hit

    def getHisPlateIds(self, *args, **kwargs):
        plate, day, index = kwargs.get("p"), kwargs.get("date"), kwargs.get("index", "0")
        hit = _latest(f"kaipan_plate_members_{day}_{plate}_i{index}_*.json")
        if hit is None:
            raise _unrecorded("kaipan", "getHisPlateIds", p=plate, date=day, index=index)
        return hit

    def getHisStockDDE(self, *args, **kwargs):
        symbol, day = _arg(args, kwargs, "symbol", 0), _arg(args, kwargs, "date", 1)
        hit = _latest(f"kaipan_stock_dde_{day}_{symbol}_*.json")
        if hit is None:
            raise _unrecorded("kaipan", "getHisStockDDE", symbol=symbol, date=day)
        return hit

    def getBanReasons(self, *args, **kwargs):
        code = str(kwargs.get("code") or (args[0] if args else "")).zfill(6)
        hit = _latest(f"kaipan_ban_reasons_*_{code}_st*_*.json")
        if hit is None:
            raise _unrecorded("kaipan", "getBanReasons", code=code)
        return hit

    def reques(self, *args, **kwargs):
        raise _unrecorded("kaipan", "reques")


class _Unreplayed(types.ModuleType):
    def __init__(self, name, source, unreachable=False):
        super().__init__(name)
        self._source = source
        self._unreachable = unreachable   # the real client fails with a connection error when its server cannot be reached

    def __getattr__(self, attribute):
        if attribute.startswith("__"):
            raise AttributeError(attribute)

        def call(*args, **kwargs):
            if self._unreachable:
                raise _GapConnectionError(str(_unrecorded(self._source, attribute)))
            raise _unrecorded(self._source, attribute)
        return call


class _GapConnectionError(ConnectionError):
    """What the real client raises when its server cannot be reached; here the replay has no server."""


class RedisError(Exception):
    __module__ = "redis.exceptions"


class RedisConnectionError(RedisError):
    __module__ = "redis.exceptions"


class RedisTimeoutError(RedisError):
    __module__ = "redis.exceptions"


class _RedisGapError(RedisConnectionError):
    """Raised where the real redis-py client raises ``redis.exceptions.ConnectionError``; the module name is part of
    how Core recognises the Redis client's errors."""
    __module__ = "redis.exceptions"


class _GapRedis:
    """Isolated Kaipan ledger and mail deduplication; other upstream data stays unavailable."""

    _values = {}
    _deadlines = {}

    @classmethod
    def from_url(cls, *_args, **kwargs):
        return cls(**kwargs)

    def __init__(self, *_args, **kwargs):
        self.decode_responses = kwargs.get("decode_responses", False)
        socket_path = os.environ.get("CORE_REPLAY_REDIS_SOCKET")
        if socket_path:
            manager = _ReplayRedisManager(address="\0" + socket_path, authkey=b"replay")
            manager.connect()
            self._values = manager.get_values()
            self._deadlines = manager.get_deadlines()

    def close(self, *_args, **_kwargs):
        """No connection to close."""

    @staticmethod
    def _kaipan():
        return bool(re.fullmatch(r"kaipan_hot_\d{6}", _CONTEXT["instance"] or os.environ.get("CORE_REPLAY_INSTANCE", "")))

    def _allowed(self, key):
        return self._kaipan() and isinstance(key, str) and (
            key.startswith("core:cache:data:hot_plates_gaps:") or key.startswith("core:mail:sent:"))

    def _check(self, command, key):
        if not self._allowed(key):
            raise _RedisGapError(str(_unrecorded("redis", command)))
        if key in self._deadlines and self._deadlines[key] <= time.monotonic():
            self._values.pop(key, None)
            self._deadlines.pop(key, None)

    def get(self, key):
        key = key.decode() if isinstance(key, bytes) else key
        if self._kaipan() and re.fullmatch(r"core:view:hot_rank:\d{4}-\d{2}-\d{2}", key):
            return None
        self._check("get", key)
        value = self._values.get(key)
        return value.decode() if value is not None and self.decode_responses else value

    def set(self, key, value, ex=None, nx=False, xx=False):
        key = key.decode() if isinstance(key, bytes) else key
        self._check("set", key)
        if (nx and key in self._values) or (xx and key not in self._values):
            return None
        if ex is not None and ex <= 0:
            raise RedisError("invalid expire time in set")
        self._values[key] = value if isinstance(value, bytes) else str(value).encode()
        self._deadlines.pop(key, None)
        if ex is not None:
            self._deadlines[key] = time.monotonic() + ex
        return True

    def scan_iter(self, *args, **kwargs):
        match = kwargs.get("match")
        if (self._kaipan() and not args and isinstance(match, str) and
                re.fullmatch(r"core:cache:data:hot_plates:\d{4}-\d{2}-\d{2}:\*", match)):
            return iter(())
        if not self._allowed(match):
            raise _RedisGapError(str(_unrecorded("redis", "scan_iter")))
        keys = []
        for key in self._values.keys():
            self._check("scan_iter", key)
            if key in self._values and fnmatch.fnmatchcase(key, match):
                keys.append(key if self.decode_responses else key.encode())
        return iter(keys)

    def delete(self, *keys):
        count = 0
        for key in keys:
            key = key.decode() if isinstance(key, bytes) else key
            self._check("delete", key)
            count += key in self._values
            self._values.pop(key, None)
            self._deadlines.pop(key, None)
        return count

    def expire(self, key, seconds):
        key = key.decode() if isinstance(key, bytes) else key
        self._check("expire", key)
        if key not in self._values:
            return False
        if seconds <= 0:
            self.delete(key)
        else:
            self._deadlines[key] = time.monotonic() + seconds
        return True

    def exists(self, *keys):
        keys = tuple(key.decode() if isinstance(key, bytes) else key for key in keys)
        for key in keys:
            self._check("exists", key)
        return sum(key in self._values for key in keys)

    def __getattr__(self, command):
        if command.startswith("__"):
            raise AttributeError(command)

        def call(*_args, **_kwargs):
            raise _RedisGapError(str(_unrecorded("redis", command)))
        return call



class _ReplayRedisManager(BaseManager):
    pass


_ReplayRedisManager.register("get_values", callable=lambda: _GapRedis._values, proxytype=DictProxy)
_ReplayRedisManager.register("get_deadlines", callable=lambda: _GapRedis._deadlines, proxytype=DictProxy)


def _start_replay_redis():
    if os.environ.get("CORE_REPLAY_REDIS_SOCKET"):
        return
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    socket_path = "engine-core-replay-" + uuid.uuid4().hex
    manager = _ReplayRedisManager(address="\0" + socket_path, authkey=b"replay")
    server = manager.get_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["CORE_REPLAY_REDIS_SOCKET"] = socket_path

def _gap_taos_connect(*_args, **_kwargs):
    raise _unrecorded("td", "connect")


_INSTALLED = {"done": False}


def _block_network() -> None:
    """Any internet-protocol connection is a recorded gap that fails like an unreachable host. Unix sockets (local IPC)
    stay open. Catches whatever the named stand-ins do not: requests/urllib calls to a quote site, a stray local Redis."""
    import socket

    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise _GapConnectionError(str(_unrecorded("network", "connect", address=str(address))))
        return real_connect(self, address)

    def getaddrinfo(host, port, *args, **kwargs):
        raise _GapConnectionError(str(_unrecorded("network", "resolve", host=str(host), port=str(port))))

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo
    _ = real_getaddrinfo


class VirtualTime:
    """Time that only moves when something sleeps: sleep(s) adds s to an offset and returns at once, time.time() and
    time.monotonic() include the offset. A handler that waits 240 s for something that will never come then finishes
    its wait instantly instead of burning 240 real seconds. (datetime.now() is not virtualised.)"""

    def __init__(self):
        self.offset = 0.0
        self.real_time, self.real_monotonic, self.real_sleep = time.time, time.monotonic, time.sleep

    def time(self):
        return self.real_time() + self.offset

    def monotonic(self):
        return self.real_monotonic() + self.offset

    def sleep(self, seconds):
        self.offset += max(0.0, float(seconds))
        self.real_sleep(0)


def install() -> None:
    if _INSTALLED["done"]:
        return
    _INSTALLED["done"] = True
    _start_replay_redis()
    # Job scripts are started with sys.executable and some replace PYTHONPATH, so every replayed process (handler, job,
    # the jobs' own children ...) points sys.executable at a wrapper that installs the stand-ins before running anything.
    wrapper = str(ROOT / "examples" / "replay_python")
    if sys.executable != wrapper:
        os.environ.setdefault("CORE_REPLAY_REAL_PYTHON", sys.executable)
        sys.executable = wrapper
    _block_network()
    virtual = VirtualTime()
    time.time, time.monotonic, time.sleep = virtual.time, virtual.monotonic, virtual.sleep
    parent = types.ModuleType("pykaipan")
    parent.__path__ = []  # type: ignore[attr-defined]
    replay = ReplayKaipan()
    parent.pykaipan = replay  # type: ignore[attr-defined]
    sys.modules["pykaipan"] = parent
    sys.modules["pykaipan.pykaipan"] = replay
    redis_module = types.ModuleType("redis")
    redis_module.Redis = _GapRedis  # type: ignore[attr-defined]
    redis_module.from_url = _GapRedis.from_url  # type: ignore[attr-defined]
    exceptions_module = types.ModuleType("redis.exceptions")
    exceptions_module.RedisError = redis_module.RedisError = RedisError
    exceptions_module.ConnectionError = redis_module.ConnectionError = RedisConnectionError
    exceptions_module.TimeoutError = redis_module.TimeoutError = RedisTimeoutError
    redis_module.exceptions = exceptions_module
    sys.modules["redis"] = redis_module
    sys.modules["redis.exceptions"] = exceptions_module
    taos_module = types.ModuleType("taos")
    taos_module.connect = _gap_taos_connect  # type: ignore[attr-defined]
    sys.modules["taos"] = taos_module
    sys.modules["baostock"] = _Unreplayed("baostock", "baostock", unreachable=True)
    sys.modules["pywencai"] = _Unreplayed("pywencai", "wencai")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    install()
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "examples"))
    sys.path.insert(0, str(ROOT))
    import guardian_handler

    raw = sys.stdin.read()
    try:
        _CONTEXT["instance"] = str((json.loads(raw) or {}).get("instance") or "")
    except ValueError:
        pass
    os.environ["CORE_REPLAY_INSTANCE"] = _CONTEXT["instance"]      # child processes attribute their gaps to it

    sys.stdin = io.StringIO(raw)
    original = guardian_handler._json
    command = argv[0] if argv else ""

    def tee(payload):
        _append("replies.jsonl", {"command": command, "reply": payload})
        return original(payload)

    guardian_handler._json = tee
    code = guardian_handler.main(argv)
    _append("exits.jsonl", {"command": command, "exit": code})
    return code


if __name__ == "__main__":
    raise SystemExit(main())
