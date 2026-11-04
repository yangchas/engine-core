"""Who is holding TDengine's query memory? (design/29 §3-B, card C660)

``SHOW QUERIES`` lists every running query with its client pid, start time and SQL. A query that has been running for a
long time (default 30 min) and does not belong to Core is reported as ``degraded`` with its owner and kill id, so the
reason behind ``[0x073a] Query memory exhausted`` is visible in the ledger instead of being guessed. The check is read-only:
it never kills anything.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_THRESHOLD_SECONDS = 1800
REDIS_KEY = "core:td:long_queries"
REDIS_TTL_SECONDS = 7200


def connect() -> Any:
    if not os.environ.get("TD_CONFIG"):
        raise RuntimeError("TD_CONFIG_required")
    import taos
    options: dict[str, Any] = {"config": os.environ["TD_CONFIG"]}
    for env_name, option in (("TDENGINE_HOST", "host"), ("TDENGINE_PORT", "port"),
                             ("TDENGINE_USER", "user"), ("TDENGINE_PASSWORD", "password")):
        if os.environ.get(env_name):
            options[option] = int(os.environ[env_name]) if option == "port" else os.environ[env_name]
    return taos.connect(**options)


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


def long_queries(cursor: Any, *, threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
                 owner_of: Callable[[Any], str] = owner_command) -> list[dict[str, Any]]:
    found = []
    for query in fetch_queries(cursor):
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
        owner_of: Callable[[Any], str] = owner_command, state: Any = None) -> tuple[int, dict[str, Any]]:
    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    settings = args.get("td_check") if isinstance(args.get("td_check"), Mapping) else {}
    threshold = int(settings.get("threshold_seconds", DEFAULT_THRESHOLD_SECONDS))
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
        found = long_queries(connection.cursor(), threshold_seconds=threshold, owner_of=owner_of)
    except Exception as exc:  # noqa: BLE001
        return 75, {"contract": 1, "status": "failed", "error_code": "TD_UNAVAILABLE", "retryable": True,
                    "artifacts": [], "detail": type(exc).__name__ + ":" + str(exc)[:160]}
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
    foreign = [item for item in found if not item["owner_is_core"]]
    detail = {"threshold_seconds": threshold, "long_queries": found, "foreign": len(foreign),
              "core": len(found) - len(foreign)}
    _publish(detail, state)
    if foreign:
        return 10, {"contract": 1, "status": "degraded", "error_code": "TD_FOREIGN_LONG_QUERY", "retryable": False,
                    "artifacts": [], "detail": detail}
    return 0, {"contract": 1, "status": "ok", "error_code": "", "retryable": False, "artifacts": [],
               "detail": detail}


def _publish(detail: Mapping[str, Any], state: Any) -> None:
    """Latest result for operators in Redis (core:td:long_queries, 2 h); observational, never fails the check."""
    from ..runtime_state import StateStore, StateUnavailable, redis_configured
    store = state if state is not None else (StateStore() if redis_configured() else None)
    if store is None:
        return
    try:
        store.set(REDIS_KEY, dict(detail), REDIS_TTL_SECONDS)
    except StateUnavailable:
        pass
