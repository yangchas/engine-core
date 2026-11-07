"""Is there room in TDengine's query memory, and who is using it? (design/29 §3-B, cards C660 and C688)

TDengine's query pool is the system memory left after a reserve (documented default: 20% of physical memory, at least
1 GB), so ``[0x073a] Query memory exhausted`` can come from a long query, from many short ones, or from other
processes on the host shrinking the room. One run therefore records all three: ``SHOW QUERIES`` with every running
query (client pid, owner, age), the host's memory (``/proc/meminfo`` and taosd's resident size) and the pool headroom
estimated from the documented reserve. A long query of another process, a long query of Core itself, or headroom below
the limit is reported as ``degraded``; every run's snapshot is kept in Redis for three days, so the reason behind a
``0x073a`` can be read afterwards instead of guessed. The check is read-only: it never kills anything.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_THRESHOLD_SECONDS = 1800
DEFAULT_MIN_HEADROOM_MB = 512            # estimated pool headroom below this is reported
RESERVE_FRACTION, RESERVE_MIN_MB = 0.20, 1024      # TDengine's documented default reserve (minReservedMemorySize)
REDIS_KEY = "core:td:long_queries"
REDIS_TTL_SECONDS = 7200
HISTORY_KEY_PREFIX = "core:td:pool:"      # one snapshot per run, so a failure can be compared with the minutes before it
HISTORY_TTL_SECONDS = 3 * 86400
TOP_QUERIES = 5


def connect() -> Any:
    if not os.environ.get("TD_CONFIG"):
        raise RuntimeError("TD_CONFIG_required")
    from ..data_pipeline.td_sink import connect_td
    return connect_td()


def owner_command(pid: Any) -> str:
    """Best-effort command line of the client process (same machine); '' when it is gone or unreadable."""
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, ValueError, TypeError):
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()[:200]


def is_core_command(command: str) -> bool:
    return "engine_core" in command


def fetch_queries(cursor: Any) -> list[dict[str, Any]]:
    cursor.execute("SHOW QUERIES")
    columns = [column[0] for column in (cursor.description or [])]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def parse_meminfo(text: str) -> dict[str, int]:
    """``/proc/meminfo`` as {field: kB}."""
    values: dict[str, int] = {}
    for line in str(text).splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name.strip()] = int(parts[0])
    return values


def read_memory(*, meminfo: Path = Path("/proc/meminfo"), proc: Path = Path("/proc")) -> dict[str, Any]:
    """Host memory and taosd's resident size, with the pool headroom TDengine's documented reserve leaves.

    ``pool_headroom_estimate_mb`` is an estimate (available memory minus the documented default reserve), not a number
    read from TDengine."""
    fields = parse_meminfo(meminfo.read_text(encoding="utf-8"))
    total_mb = fields.get("MemTotal", 0) // 1024
    available_mb = fields.get("MemAvailable", 0) // 1024
    reserve_mb = max(int(total_mb * RESERVE_FRACTION), RESERVE_MIN_MB)
    taosd: dict[str, int] = {}
    for status in sorted(proc.glob("[0-9]*/status")):
        try:
            text = status.read_text(encoding="utf-8")
        except OSError:                                    # the process ended while we looked
            continue
        if text.split(None, 2)[1:2] == ["taosd"] and text.startswith("Name:"):
            values = parse_meminfo(text)
            taosd = {"pid": int(status.parent.name), "rss_mb": values.get("VmRSS", 0) // 1024,
                     "peak_rss_mb": values.get("VmHWM", 0) // 1024}
            break
    processes = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.name.isascii() or not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text(encoding="utf-8").strip()
            values = parse_meminfo((entry / "status").read_text(encoding="utf-8"))
            if "VmRSS" in values:
                processes.append({"pid": int(entry.name), "name": name, "rss_mb": values["VmRSS"] // 1024})
        except (OSError, UnicodeError, ValueError):
            continue
    top_processes = sorted(processes, key=lambda process: process["rss_mb"], reverse=True)[:8]
    return {"total_mb": total_mb, "available_mb": available_mb, "committed_mb": fields.get("Committed_AS", 0) // 1024,
            "commit_limit_mb": fields.get("CommitLimit", 0) // 1024, "swap_total_mb": fields.get("SwapTotal", 0) // 1024,
            "reserve_estimate_mb": reserve_mb, "pool_headroom_estimate_mb": available_mb - reserve_mb, "taosd": taosd,
            "top_processes": top_processes}


def running_queries(queries: list[dict[str, Any]], *,
                    owner_of: Callable[[Any], str] = owner_command) -> dict[str, Any]:
    """Every running query (not only the long ones): many short heavy queries can empty the pool too."""
    found = []
    for query in queries:
        command = owner_of(query.get("pid"))
        found.append({"seconds": round(int(query.get("exec_usec") or 0) / 1_000_000, 1), "pid": query.get("pid"),
                      "app": query.get("app"), "owner_is_core": is_core_command(command),
                      "sql": " ".join(str(query.get("sql") or "").split())[:160]})
    found.sort(key=lambda item: item["seconds"], reverse=True)
    return {"count": len(found), "core": sum(item["owner_is_core"] for item in found),
            "foreign": sum(not item["owner_is_core"] for item in found), "top": found[:TOP_QUERIES]}


def long_queries(cursor: Any, *, threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
                 owner_of: Callable[[Any], str] = owner_command) -> list[dict[str, Any]]:
    return _long_from(fetch_queries(cursor), threshold_seconds=threshold_seconds, owner_of=owner_of)


def _long_from(queries: list[dict[str, Any]], *, threshold_seconds: int,
               owner_of: Callable[[Any], str]) -> list[dict[str, Any]]:
    found = []
    for query in queries:
        seconds = int(query.get("exec_usec") or 0) / 1_000_000
        if seconds < threshold_seconds:
            continue
        command = owner_of(query.get("pid"))
        found.append({"kill_id": query.get("kill_id"), "pid": query.get("pid"), "app": query.get("app"),
                      "owner": command, "owner_is_core": is_core_command(command),
                      "minutes": round(seconds / 60, 1), "create_time": str(query.get("create_time")),
                      "sql": " ".join(str(query.get("sql") or "").split())[:200]})
    return found


def run(request: Mapping[str, Any], root: Any, *, dry_run: bool = False, connect_fn: Callable[[], Any] | None = None,
        owner_of: Callable[[Any], str] = owner_command, state: Any = None,
        memory_of: Callable[[], dict[str, Any]] | None = None,
        now: Callable[[], datetime] = datetime.now) -> tuple[int, dict[str, Any]]:
    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    settings = args.get("td_check") if isinstance(args.get("td_check"), Mapping) else {}
    threshold = int(settings.get("threshold_seconds", DEFAULT_THRESHOLD_SECONDS))
    min_headroom = int(settings.get("min_headroom_mb", DEFAULT_MIN_HEADROOM_MB))
    if dry_run:
        return 0, {"contract": 1, "status": "not_applicable", "error_code": "", "retryable": False,
                   "artifacts": [], "detail": "dry_run"}
    try:
        connection = (connect_fn or connect)()
    except RuntimeError as exc:
        if str(exc) == "TD_CONFIG_required":
            return 64, {"contract": 1, "status": "failed", "error_code": "CONFIG_ERROR", "retryable": False,
                        "artifacts": [], "detail": "TD_CONFIG_required"}
        return 75, {"contract": 1, "status": "failed", "error_code": "TD_UNAVAILABLE", "retryable": True,
                    "artifacts": [], "detail": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001 - an unreachable TD is retryable, never final
        return 75, {"contract": 1, "status": "failed", "error_code": "TD_UNAVAILABLE", "retryable": True,
                    "artifacts": [], "detail": type(exc).__name__}
    try:
        queries = fetch_queries(connection.cursor())
    except Exception as exc:  # noqa: BLE001
        return 75, {"contract": 1, "status": "failed", "error_code": "TD_UNAVAILABLE", "retryable": True,
                    "artifacts": [], "detail": type(exc).__name__ + ":" + str(exc)[:160]}
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
    found = _long_from(queries, threshold_seconds=threshold, owner_of=owner_of)
    foreign = [item for item in found if not item["owner_is_core"]]
    try:
        memory = (memory_of or read_memory)()
    except (OSError, ValueError):            # no /proc here: the queries are still worth reporting
        memory = None
    detail = {"threshold_seconds": threshold, "long_queries": found, "foreign": len(foreign),
              "core": len(found) - len(foreign), "queries": running_queries(queries, owner_of=owner_of),
              "memory": memory, "min_headroom_mb": min_headroom}
    findings = [code for code, present in (
        ("TD_FOREIGN_LONG_QUERY", bool(foreign)),
        ("TD_CORE_LONG_QUERY", len(found) > len(foreign)),
        ("TD_MEMORY_HEADROOM_LOW", bool(memory) and memory["pool_headroom_estimate_mb"] < min_headroom)) if present]
    detail["findings"] = findings
    _publish(detail, state, at=now())
    if findings:
        return 10, {"contract": 1, "status": "degraded", "error_code": findings[0], "retryable": False,
                    "artifacts": [], "detail": detail}
    return 0, {"contract": 1, "status": "ok", "error_code": "", "retryable": False, "artifacts": [],
               "detail": detail}


def _publish(detail: Mapping[str, Any], state: Any, *, at: datetime) -> None:
    """Latest result and this run's snapshot in Redis; observational, never fails the check."""
    from ..runtime_state import StateStore, StateUnavailable, redis_configured
    store = state if state is not None else (StateStore() if redis_configured() else None)
    if store is None:
        return
    try:
        store.set(REDIS_KEY, dict(detail), REDIS_TTL_SECONDS)
        store.set(HISTORY_KEY_PREFIX + at.strftime("%Y%m%dT%H%M%S"), {"at": at.isoformat(timespec="seconds"), **detail},
                  HISTORY_TTL_SECONDS)
    except StateUnavailable:
        pass
