#!/usr/bin/env python3
"""Guardian handler for whole-day replays (examples/replay_day.py, C665).

Runs the real examples/guardian_handler.py with every upstream replaced:
  * the Kaipan SDK (``pykaipan.pykaipan``) answers from recorded raw responses in tests/fixtures/real, matched on the
    request's date/level/plate/symbol; anything else raises UnrecordedRequest;
  * every internet-protocol connection is a recorded gap (network.connect / network.resolve);
  * ``baostock`` / ``pywencai`` / ``redis`` / ``taos`` are stand-ins: any use is a recorded gap and fails the way an
    unreachable source fails (UnrecordedRequest, or ConnectionError for Redis).
Every request that could not be answered is appended, with the instance that made it, to $CORE_REPLAY_DIR/unrecorded.jsonl and every reply the handler
produced to $CORE_REPLAY_DIR/replies.jsonl, so the report can list gaps instead of hiding them.
"""
from __future__ import annotations

import glob
import io
import json
import os
import re
import sys
import time
import types
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
    def __init__(self, name, source):
        super().__init__(name)
        self._source = source

    def __getattr__(self, attribute):
        if attribute.startswith("__"):
            raise AttributeError(attribute)

        def call(*args, **kwargs):
            raise _unrecorded(self._source, attribute)
        return call


class _GapConnectionError(ConnectionError):
    """What the real client raises when its server cannot be reached; here the replay has no server."""


class _GapRedis:
    """Redis stand-in: every command is a recorded gap that fails like an unreachable server."""

    @classmethod
    def from_url(cls, *_args, **_kwargs):
        return cls()

    def __init__(self, *_args, **_kwargs):
        pass

    def __getattr__(self, command):
        if command.startswith("__"):
            raise AttributeError(command)

        def call(*_args, **_kwargs):
            raise _GapConnectionError(str(_unrecorded("redis", command)))
        return call


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
    redis_module.ConnectionError = redis_module.TimeoutError = redis_module.RedisError = _GapConnectionError  # type: ignore[attr-defined]
    redis_module.exceptions = redis_module  # type: ignore[attr-defined]
    sys.modules["redis"] = redis_module
    taos_module = types.ModuleType("taos")
    taos_module.connect = _gap_taos_connect  # type: ignore[attr-defined]
    sys.modules["taos"] = taos_module
    sys.modules["baostock"] = _Unreplayed("baostock", "baostock")
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
