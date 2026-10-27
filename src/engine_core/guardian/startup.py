"""Read-only implementations of the configured guardian startup events."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .registry import REGISTRY_PATH, items_for_phase, load_registry, stale_items

PHASES = ("NIGHT", "PRE_MARKET", "AUCTION", "AUCTION_END", "OPEN",
          "INTRADAY_AM", "MIDDAY_BREAK", "PM_OPEN", "INTRADAY_PM",
          "CLOSE_AUCTION", "CLOSE", "POST_MARKET")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


DAILY_KLINE_UPGRADE_MAX_ATTEMPTS = _env_int("CORE_DAILY_KLINE_UPGRADE_MAX_ATTEMPTS", 3)
DAILY_KLINE_UPGRADE_ESCALATE_TRADING_DAYS = _env_int(
    "CORE_DAILY_KLINE_UPGRADE_ESCALATE_TRADING_DAYS", 3)
DAILY_KLINE_BACKFILL_DAYS = _env_int("CORE_DAILY_KLINE_BACKFILL_DAYS", 5)


def _load_daily_kline_upgrade_states(root: Path) -> dict[str, Any]:
    try:
        states = json.loads((root / "guardian-upgrade-attempts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        states = {}
    return states if isinstance(states, dict) else {}


def _save_daily_kline_upgrade_states(root: Path, states: dict[str, Any]) -> None:
    from ..data_pipeline.storage import _atomic_write, canonical_json

    _atomic_write(root / "guardian-upgrade-attempts.json", canonical_json(states) + b"\n")


def _resolve_reconcile_load5_limit(value: Any, cpus: int) -> float:
    if isinstance(value, str) and value.lower() in {"cpus", "cpu_count"}:
        return float(cpus)
    if isinstance(value, bool):
        return float(cpus)
    try:
        limit = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(cpus)
    return limit if math.isfinite(limit) and limit >= 0 else float(cpus)


def _resolve_load_defer_window_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        return 3
    return limit if limit >= 1 else 3


def _record_daily_kline_load_deferral(root: Path, partition: str, *, now: datetime,
                                      phase: str, window_limit: int, load5: float,
                                      load_threshold: float) -> dict[str, Any] | None:
    from .reconcile_plan import daily_kline_next_retry_window, daily_kline_window_id

    states = _load_daily_kline_upgrade_states(root)
    previous = states.get(partition)
    state = dict(previous) if isinstance(previous, dict) else {"status": "queued", "attempts": 0}
    window_id = daily_kline_window_id(now, phase)
    if state.get("load_defer_last_window_id") == window_id:
        return None
    consecutive = (int(state.get("consecutive_load_defer_windows", 0) or 0) + 1
                   if state.get("load_defer_next_window_id") == window_id else 1)
    next_window = daily_kline_next_retry_window(now, phase)
    state.update(deferred_by_load=True, consecutive_load_defer_windows=consecutive,
                 load_defer_last_window_id=window_id,
                 load_defer_next_window_id=next_window,
                 last_load5=load5, load_threshold=load_threshold)
    alert = None
    if consecutive >= window_limit:
        state["load_override_window_id"] = next_window
        if not state.get("load_defer_alerted"):
            state["load_defer_alerted"] = True
            alert = {"event": "daily_kline_load_deferred",
                     "reason": "daily_kline_deferred_by_load_for_consecutive_quiet_windows",
                     "partition": partition, "consecutive_windows": consecutive,
                     "next_window": next_window, "load5": load5,
                     "load_threshold": load_threshold}
    states[partition] = state
    _save_daily_kline_upgrade_states(root, states)
    return alert


def _clear_daily_kline_load_deferral(root: Path, partition: str) -> None:
    states = _load_daily_kline_upgrade_states(root)
    previous = states.get(partition)
    if not isinstance(previous, dict) or not any(key in previous for key in (
            "deferred_by_load", "consecutive_load_defer_windows", "load_override_window_id")):
        return
    state = dict(previous)
    state.update(deferred_by_load=False, consecutive_load_defer_windows=0)
    for key in ("load_defer_last_window_id", "load_defer_next_window_id", "load_override_window_id"):
        state.pop(key, None)
    states[partition] = state
    _save_daily_kline_upgrade_states(root, states)


def _claim_daily_kline_load_override(root: Path, action: dict[str, Any], *, now: datetime,
                                     phase: str, dry_run: bool) -> bool:
    if not action.get("load_override_once"):
        return True
    if dry_run:
        return True
    from .reconcile_plan import daily_kline_window_id

    partition = str(action.get("partition", ""))
    window_id = daily_kline_window_id(now, phase)
    states = _load_daily_kline_upgrade_states(root)
    previous = states.get(partition)
    if not isinstance(previous, dict):
        return False
    if (previous.get("load_override_window_id") != window_id or
            previous.get("load_override_queued_window") == window_id):
        return False
    state = dict(previous)
    state["load_override_queued_window"] = window_id
    state.update(deferred_by_load=False, consecutive_load_defer_windows=0)
    for key in ("load_defer_last_window_id", "load_defer_next_window_id", "load_override_window_id"):
        state.pop(key, None)
    states[partition] = state
    _save_daily_kline_upgrade_states(root, states)
    return True


def _is_consecutive_failed_trading_days(root: Path, failed_days: list[str], today: str) -> bool:
    try:
        matrix = json.loads((root / "trade_calendar" / "calendar.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    trading_days = sorted(str(row["d"]) for row in matrix.get("days", [])
                          if isinstance(row, dict) and row.get("t") == 1 and str(row.get("d", "")) <= today)
    count = DAILY_KLINE_UPGRADE_ESCALATE_TRADING_DAYS
    recent = trading_days[-count:]
    return len(recent) == count and today in recent and set(recent).issubset(set(failed_days))


def _daily_kline_candidates(root: Path, trade_date: str, now: datetime) -> list[str]:
    """Return the last M eligible trading-day partitions, oldest first."""
    try:
        matrix = json.loads((root / "trade_calendar" / "calendar.json").read_text(encoding="utf-8"))
        days = matrix.get("days", [])
    except (OSError, ValueError):
        return []
    try:
        item = next(item for item in load_registry() if item["id"] == "daily_kline")
        trigger = str((item.get("late_policy") or {}).get("trigger_at", "17:30:00"))
    except (StopIteration, TypeError):
        trigger = "17:30:00"
    latest = trade_date if now.strftime("%H:%M:%S") >= trigger else (
        datetime.fromisoformat(trade_date).date() - timedelta(days=1)).isoformat()
    candidates = sorted(str(row["d"]) for row in days
                        if isinstance(row, dict) and row.get("t") == 1
                        and str(row.get("d", "")) <= latest)
    return candidates[-DAILY_KLINE_BACKFILL_DAYS:]


def _record_daily_kline_upgrade(root: Path, partition: str, *, succeeded: bool,
                                now: datetime | str | None = None,
                                phase: str | None = None) -> dict[str, Any] | None:
    """Record one source attempt against its partition and current quiet window."""
    from .registry import catalog_manifest, daily_kline_needs_upgrade
    from .reconcile_plan import daily_kline_next_retry_window, daily_kline_window_id
    from ..data_pipeline.storage import _atomic_write, canonical_json

    clock = datetime.fromisoformat(now.replace("Z", "+00:00")) if isinstance(now, str) else now
    clock = clock or datetime.now().astimezone()
    states = _load_daily_kline_upgrade_states(root)
    state = states.get(partition)
    if not isinstance(state, dict):
        legacy_key = next((key for key, value in states.items()
                           if key.startswith(f"{partition}:") and isinstance(value, dict)), None)
        state = states.pop(legacy_key) if legacy_key else {"status": "queued", "attempts": 0}
    else:
        state = dict(state)

    window_id = daily_kline_window_id(clock, phase)
    if state.get("window_id") != window_id:
        legacy_exhausted_today = (not state.get("window_id") and
                                  state.get("status") in {"exhausted", "GAP_WINDOW_EXHAUSTED"} and
                                  clock.date().isoformat() <= partition)
        if legacy_exhausted_today:
            state["window_id"] = window_id
            state["attempts"] = DAILY_KLINE_UPGRADE_MAX_ATTEMPTS
        else:
            state["attempts"] = 0
            state["window_id"] = window_id
            if not state.get("escalation_alerted"):
                state.update(status="queued", reason="retry_window_open")
            state.pop("next_retry_window", None)

    escalated_now = False
    if succeeded:
        try:
            catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalog = {"datasets": {}}
        manifest = catalog_manifest(catalog, "daily_kline", partition)
        if not daily_kline_needs_upgrade(manifest):
            state.update(status="resolved", reason="formal_version_available")
        else:
            state["attempts"] = int(state.get("attempts", 0)) + 1
            state["total_attempts"] = int(state.get("total_attempts", 0)) + 1
            state.update(reason="degraded_version_remains")
    else:
        state["attempts"] = int(state.get("attempts", 0)) + 1
        state["total_attempts"] = int(state.get("total_attempts", 0)) + 1
        state.update(reason="fetch_failed")

    if state.get("status") != "resolved":
        state["last_attempt_at"] = clock.isoformat()
        if int(state.get("attempts", 0)) >= DAILY_KLINE_UPGRADE_MAX_ATTEMPTS:
            failed_days = list(state.get("failed_trade_dates", []))
            today = clock.date().isoformat()
            if today not in failed_days:
                failed_days.append(today)
            state["failed_trade_dates"] = sorted(failed_days)
            if (not state.get("escalation_alerted") and
                    _is_consecutive_failed_trading_days(root, failed_days, today)):
                state["escalation_alerted"] = True
                state["reason"] = "daily_kline_upgrade_failed_consecutive_trading_days"
                escalated_now = True
            if state.get("escalation_alerted"):
                state["status"] = "GAP_ESCALATED"
            else:
                state["status"] = "GAP_WINDOW_EXHAUSTED"
                state["reason"] = "upgrade_attempt_budget_exhausted"
            state["next_retry_window"] = daily_kline_next_retry_window(clock, phase)
        elif state.get("escalation_alerted"):
            state["status"] = "GAP_ESCALATED"
            state["next_retry_window"] = window_id
        else:
            state["status"] = "queued"

    states[partition] = {key: value for key, value in state.items() if key != "escalated_now"}
    _atomic_write(root / "guardian-upgrade-attempts.json", canonical_json(states) + b"\n")
    return {**state, "escalated_now": escalated_now}


def _record_daily_kline_upgrade_gap(root: Path, partition: str, state: dict[str, Any],
                                    now: datetime | str | None = None) -> None:
    status = state.get("status")
    if status == "resolved":
        gap = {"id": "daily_kline", "partition": partition, "status": "RESOLVED",
               "reason": "formal_version_available", "recommendation": "resolved"}
    elif status in {"GAP_WINDOW_EXHAUSTED", "GAP_ESCALATED"} and state.get("next_retry_window"):
        gap = {"id": "daily_kline", "partition": partition, "status": status,
               "reason": state.get("reason", "upgrade_attempt_budget_exhausted"),
               "attempts": state.get("attempts", DAILY_KLINE_UPGRADE_MAX_ATTEMPTS),
               "next_retry_window": state["next_retry_window"],
               "recommendation": "automatic_retry_next_quiet_window"}
    else:
        return
    observed = now.isoformat() if isinstance(now, datetime) else now
    _append_gap(root, partition, gap, observed or datetime.now().astimezone().isoformat())


def _now(value: datetime | str | None, trade_date: str) -> datetime:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value or datetime.fromisoformat(f"{trade_date}T00:00:00+08:00")


def _calendar(root: Path, trade_date: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from ..data_pipeline.trade_calendar import TradeCalendarStore

    store = TradeCalendarStore(root)
    matrix = store.load()
    if not matrix or not matrix.get("lit_from", "") <= trade_date <= matrix.get("lit_to", ""):
        raise ValueError("calendar_missing_or_uncovered")
    from ..trade_day_context import load_context
    context = load_context(root, trade_date)
    trading = context["today_is_trading_day"]
    previous = context["prev_trade_date"]
    following = context["next_trade_date"]
    days = matrix.get("days", [])
    index = next(i for i, item in enumerate(days) if item["d"] == trade_date)
    window = days[max(0, index - 3):index + 4]
    return matrix, {"is_trading_day": trading, "prev": previous, "next": following,
                   "lit_window": {"lit_from": matrix["lit_from"], "lit_to": matrix["lit_to"],
                                  "days": window}}


def _result(status: str, detail: Any, *, error_code: str = "", retryable: bool = False) -> dict[str, Any]:
    return {"contract": 1, "status": status, "error_code": error_code,
            "retryable": retryable, "artifacts": [], "detail": detail}


def boot_trade_day(trade_date: str, root: str | Path, *, probes: bool = True) -> dict[str, Any]:
    root = Path(root).expanduser()
    try:
        _, summary = _calendar(root, trade_date)
    except Exception:
        return _result("failed", {"trade_date": trade_date, "calendar": "UNKNOWN"},
                       error_code="CALENDAR_UNKNOWN", retryable=True)
    detail: dict[str, Any] = {"trade_date": trade_date, **summary}
    try:
        from ..trade_day_context import refresh_context
        context = refresh_context(root, trade_date)
        detail["day_context"] = {key: context[key] for key in ("today", "yesterday", "tomorrow", "prev_trade_date",
                                                              "prev2_trade_date", "next_trade_date", "next2_trade_date")}
    except Exception as exc:  # the persisted copy is a convenience; consumers recompute from the calendar
        detail["day_context"] = "UNWRITTEN:" + type(exc).__name__
    if probes:
        # Historical auction evidence is strictly advisory and cannot affect the calendar verdict.
        try:
            detail["previous_day_auction_rows"] = _td_count(summary["prev"], root)
        except Exception:
            detail["previous_day_auction_rows"] = "UNKNOWN"
    return _result("ok", detail)


def _timed_probe(fn, timeout: float = 5.0):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise RuntimeError("probe_timeout")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _td_probes() -> dict[str, Any]:
    if not os.environ.get("TD_CONFIG"):
        raise RuntimeError("TD_CONFIG_missing")
    import taos
    connection = taos.connect(database="market_data1", config=os.environ["TD_CONFIG"])
    try:
        result = {"connection": "ok"}
        for table in ("stock_tick_v2", "auction_snapshot_v2"):
            cursor = connection.cursor()
            cursor.execute(f"SELECT LAST(ts) FROM {table}")
            rows = cursor.fetchall()
            result[table] = rows[0][0].isoformat() if rows and rows[0][0] else None
        return result
    finally:
        connection.close()


def _td_count(day: str, root: Path) -> Any:
    if not os.environ.get("TD_CONFIG"):
        raise RuntimeError("TD_CONFIG_missing")
    import taos
    connection = taos.connect(database="market_data1", config=os.environ["TD_CONFIG"])
    try:
        cursor = connection.cursor()
        end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        cursor.execute(f"SELECT COUNT(*) FROM auction_snapshot_v2 WHERE ts >= '{day} 00:00:00' AND ts < '{end} 00:00:00'")
        rows = cursor.fetchall()
        return rows[0][0] if rows else None
    finally:
        connection.close()


def _health_probes(root: Path) -> dict[str, Any]:
    probes: dict[str, Any] = {}
    try:
        probes["td"] = _timed_probe(_td_probes)
        probes["td"]["status"] = "ok"
    except Exception as exc:
        probes["td"] = {"status": "failed", "reason": type(exc).__name__}
    try:
        import redis
        url = os.environ.get("REDIS_URL")
        if not url:
            raise RuntimeError("REDIS_URL_missing")
        client = redis.Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)
        probes["redis"] = {"status": "ok" if client.ping() else "failed"}
        client.close()
    except Exception as exc:
        probes["redis"] = {"status": "failed", "reason": type(exc).__name__}
    try:
        path = root / "catalog.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict):
            raise ValueError("invalid_catalog")
        probes["catalog"] = {"status": "ok", "datasets": len(catalog.get("datasets", {}))}
    except Exception as exc:
        probes["catalog"] = {"status": "failed", "reason": type(exc).__name__}
    return probes


def boot_data_health(trade_date: str, prev_trade_date: str | None, root: str | Path, *,
                     now: datetime | str | None = None, dry_run: bool = False,
                     probes: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(root).expanduser()
    clock = _now(now, trade_date)
    probe_results = _health_probes(root) if probes is None else probes
    try:
        _, calendar = _calendar(root, trade_date)
        calendar_status = "ok"
        if prev_trade_date is None:
            prev_trade_date = calendar["prev"]
    except Exception:
        calendar, calendar_status = {"is_trading_day": None}, "failed"
    item_results: list[dict[str, Any]] = []
    if calendar_status == "ok" and calendar["is_trading_day"]:
        catalog_path = root / "catalog.json"
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalog = {"datasets": {}}
        item_results = stale_items(clock, catalog, trade_date=trade_date,
                                   prev_trade_date=prev_trade_date)
    detail = {"trade_date": trade_date, "calendar": {**calendar, "status": calendar_status},
              "probes": probe_results, "items": item_results,
              "summary": {status: sum(x.get("status") == status for x in item_results)
                          for status in ("OK", "PENDING", "UNKNOWN", "MISSING", "OVERDUE", "STALE")}}
    failed = any(x.get("status") == "failed" for x in probe_results.values()) or calendar_status != "ok"
    bad_items = any(x.get("status") in {"OVERDUE", "MISSING", "STALE"} for x in item_results)
    status = "degraded" if failed or bad_items else "ok"
    detail["status"] = status
    if not dry_run:
        from ..data_pipeline.storage import _atomic_write
        path = root / "guardian-native" / "health" / f"{trade_date}.json"
        _atomic_write(path, (json.dumps(detail, ensure_ascii=False, sort_keys=True) + "\n").encode())
    return _result(status, detail)


def boot_reconcile(trade_date: str, phase: str, health: dict[str, Any] | None,
                   root: str | Path, *, now: datetime | str | None = None,
                   reconcile_max_load5: Any = "cpus",
                   daily_kline_load_defer_window_limit: Any = 3) -> dict[str, Any]:
    root = Path(root).expanduser()
    if phase == "NON_TRADING":
        return _result("ok", {"phase": phase, "trade_date": trade_date, "gaps": []})
    try:
        phase_index = PHASES.index(phase)
    except ValueError:
        return _result("degraded", {"phase": phase, "gaps": [], "error": "phase_unknown"})
    if health is None:
        try:
            health = json.loads((root / "guardian-native" / "health" / f"{trade_date}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            health = {}
    items = health.get("items")
    if not isinstance(items, list):
        try:
            catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalog = {"datasets": {}}
        try:
            _, calendar = _calendar(root, trade_date)
            previous = calendar["prev"]
        except Exception:
            previous = None
        items = stale_items(_now(now, trade_date), catalog, trade_date=trade_date,
                            prev_trade_date=previous)
    by_id = {item["id"]: item for item in load_registry()}
    expected_ids = {item["id"] for name in PHASES[:phase_index + 1]
                    for item in items_for_phase(name, list(by_id.values()))}
    clock = _now(now, trade_date)
    gaps = []
    for state in items:
        spec = by_id.get(state.get("id"))
        if spec is None or spec["id"] not in expected_ids or state.get("status") not in {"OVERDUE", "MISSING", "STALE"}:
            continue
        freshness = spec.get("freshness")
        if (state.get("status") == "MISSING" and isinstance(freshness, dict)
                and freshness.get("expected_by") and phase in spec["phases"]):
            hour, minute = (int(part) for part in freshness["expected_by"].split(":"))
            if (clock.hour, clock.minute) < (hour, minute):
                continue
        if not any(PHASES.index(p) <= phase_index for p in spec["phases"]):
            continue
        event = spec.get("guardian_event")
        gaps.append({"id": spec["id"], "status": state["status"],
                     "guardian_event": event,
                     "recommendation": event or "NO_OWNER_EVENT",
                     "explanation": "expected artifact missing or overdue for current/past phase"})
    plan = []
    load5 = None
    load_threshold = None
    window_limit = _resolve_load_defer_window_limit(daily_kline_load_defer_window_limit)
    try:
        from .reconcile_plan import reconcile_plan
        catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        try:
            _, calendar = _calendar(root, trade_date)
            previous = calendar["prev"]
        except Exception:
            previous = None
        try:
            upgrade_attempts = json.loads((root / "guardian-upgrade-attempts.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            upgrade_attempts = {}
        daily_kline_candidates = _daily_kline_candidates(root, trade_date, clock)
        try:
            load5 = float(os.getloadavg()[1])
        except (AttributeError, IndexError, OSError, TypeError, ValueError):
            load5 = None
        cpus = os.cpu_count() or 1
        load_threshold = _resolve_reconcile_load5_limit(reconcile_max_load5, cpus)
        plan = reconcile_plan(clock, catalog, trade_date=trade_date, prev_trade_date=previous,
                              phase=phase, load5=load5, load_threshold=load_threshold,
                              upgrade_attempts=upgrade_attempts,
                              daily_kline_candidates=daily_kline_candidates)
    except Exception as exc:  # the plan is advisory; never fail the boot check for it
        plan = [{"id": "*", "action": "PLAN_ERROR", "error": type(exc).__name__}]
    load_defer_alert = None
    daily_kline_action = next((row for row in plan if row.get("id") == "daily_kline"), None)
    if daily_kline_action and daily_kline_action.get("deferred_by_load"):
        try:
            load_defer_alert = _record_daily_kline_load_deferral(
                root, str(daily_kline_action.get("partition", "")), now=clock, phase=phase,
                window_limit=window_limit, load5=float(load5),
                load_threshold=float(load_threshold))
        except (OSError, TypeError, ValueError):
            load_defer_alert = None
    elif (daily_kline_action and not daily_kline_action.get("load_override_once") and
          not daily_kline_action.get("deferred_by_load")):
        _clear_daily_kline_load_deferral(root, str(daily_kline_action.get("partition", "")))
    detail = {"phase": phase, "trade_date": trade_date, "gaps": gaps, "plan": plan,
              "recheck_required": bool(health.get("status") == "failed" or health.get("calendar", {}).get("status") == "failed")}
    if load_defer_alert:
        detail["load_defer_alert"] = load_defer_alert
    return _result("degraded" if gaps or detail["recheck_required"] else "ok", detail)


def _fetch_spec(item: dict[str, Any]) -> tuple[list[str], float]:
    spec = item.get("fetch_job")
    if isinstance(spec, list):
        return [str(x) for x in spec], 300.0
    if isinstance(spec, dict) and isinstance(spec.get("argv"), list):
        return [str(x) for x in spec["argv"]], float(spec.get("timeout_seconds", 300))
    raise ValueError("fetch_job_unconfigured:" + str(item.get("id")))


class FetchJobError(RuntimeError):
    def __init__(self, message: str, error_code: str, stderr: str = ""):
        super().__init__(message)
        self.error_code = error_code
        self.stderr = stderr[-300:]


def _run_fetch_job(item: dict[str, Any], day: str, timeout: float, root: str | Path | None = None) -> dict[str, Any]:
    argv, configured_timeout = _fetch_spec(item)
    timeout = min(timeout, configured_timeout)
    artifact_root = str(Path(root or os.environ.get("CORE_DATA_ROOT") or "~/core-data").expanduser())
    replacements = {"date": day, "trade_date": day, "item": str(item["id"]), "python": sys.executable,
                    "artifact_root": artifact_root}
    command = [part.format_map(replacements) for part in argv]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                   check=False, env=os.environ.copy())
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        raise FetchJobError("fetch_job_timeout", "FETCH_TIMEOUT", str(stderr)) from None
    payload: dict[str, Any] = {}
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                payload = value
                break
        except ValueError:
            continue
    has_committed_artifact = isinstance(payload.get("version"), str) and isinstance(payload.get("row_count"), int)
    explicit_failure = payload.get("status") in {"failed", "error"}
    # Exit status and explicit failure markers decide failure; success is then PROVEN by the catalog postcondition
    # (_verify_partition), not by guessing each job's stdout dialect (reindex prints no status, ladder prints
    # PARTIAL_*, daily_kline prints degraded, ...).
    if completed.returncode or explicit_failure or payload.get("error"):
        stderr = completed.stderr or ""
        error_code = "FETCH_EXIT_" + str(completed.returncode) if completed.returncode else str(
            payload.get("error_code") or "FETCH_JOB_FAILED")
        for marker in ("kaipan_hot_plates_response_date_mismatch",):
            if marker in stderr:
                error_code = marker
        raise FetchJobError(str(payload.get("error") or payload.get("error_code") or
                                "fetch_job_failed:" + str(completed.returncode)),
                            error_code, stderr)
    return payload


def _quiet_targets(item: dict[str, Any], phase: str, now: datetime) -> list[str]:
    rules = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    quiet = list(rules.get("quiet_phases_for_heavy", []))
    from .scheduler import _schedule_source
    cutoff = _schedule_source()["global"]["heavy_cutoffs"]["PRE_MARKET"]
    if phase == "PRE_MARKET":
        return ["PRE_MARKET"] if now.strftime("%H:%M:%S") < cutoff else ["MIDDAY_BREAK"]
    if phase in quiet:
        return [phase]
    order = list(PHASES)
    try:
        index = order.index(phase)
    except ValueError:
        index = -1
    future = [p for p in quiet if p in order and order.index(p) > index]
    return [future[0] if future else (quiet[0] if quiet else "POST_MARKET")]


def _append_gap(root: Path, trade_date: str, gap: dict[str, Any], observed_at: str) -> None:
    from ..data_pipeline.storage import _atomic_write

    path = root / "guardian-native" / "gaps" / f"{trade_date}.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            rows = []
    except (OSError, ValueError):
        rows = []
    key = (gap.get("id"), gap.get("partition"))
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        matches_key = (row.get("id"), row.get("partition")) == key
        matches_legacy_daily_kline = (gap.get("id") == "daily_kline" and
                                      row.get("id") == "daily_kline" and not row.get("partition"))
        if matches_key or matches_legacy_daily_kline:
            rows[index] = {**row, **gap, "observed_at": observed_at}
            break
    else:
        rows.append({**gap, "observed_at": observed_at})
    _atomic_write(path, (json.dumps(rows, ensure_ascii=False, sort_keys=True) + "\n").encode())


def _verify_partition(item: dict[str, Any], day: str, root: str | Path | None,
                      now: datetime | None = None) -> None:
    """After a fetch job exited cleanly, the catalog must now hold the partition (non-empty, complete enough)."""
    dataset = (item.get("artifact") or {}).get("catalog_dataset")
    if not dataset:
        return
    base = Path(root or os.environ.get("CORE_DATA_ROOT") or "~/core-data").expanduser()
    try:
        catalog = json.loads((base / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise FetchJobError("catalog_unreadable_after_fetch", "POSTCONDITION_FAILED") from None
    from .registry import catalog_partition_present
    if not catalog_partition_present(catalog, dataset, day, now or datetime.now().astimezone()):
        raise FetchJobError(f"partition_missing_after_fetch:{dataset}:{day}", "POSTCONDITION_FAILED")


def boot_apply_plan(trade_date: str, plan: list[dict[str, Any]], root: str | Path, *,
                    now: datetime | str | None = None, phase: str = "", dry_run: bool | None = None,
                    enqueue_only: bool = False,
                    active_events: Mapping[str, str] | list[str] | None = None) -> dict[str, Any]:
    """Execute the startup plan with bounded light concurrency and explicit deferred work."""
    root = Path(root).expanduser()
    clock = _now(now, trade_date)
    if dry_run is None:
        dry_run = os.environ.get("GUARDIAN_DRY_RUN") == "1"
    by_id = {item["id"]: item for item in load_registry()}
    active_owners = set(active_events or [])
    if active_owners:
        plan = [action for action in plan
                if not (by_id.get(str(action.get("id", "")), {}).get("guardian_event") in active_owners
                        and (not isinstance(active_events, Mapping)
                             or str(action.get("partition", "")) == str(active_events.get(
                                 by_id.get(str(action.get("id", "")), {}).get("guardian_event"), ""))))]
    results: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    def execute(pair):
        item, action = pair
        started = time.monotonic()
        row = {"id": item["id"], "action": "FETCH_NOW", "partition": str(action.get("partition", "")),
               "cost": (item.get("cost") or {}).get("class", "medium")}
        if action.get("load_override_once"):
            row["load_override_once"] = True
            if not _claim_daily_kline_load_override(root, action, now=clock, phase=phase,
                                                    dry_run=bool(dry_run)):
                row.update(status="already_queued", elapsed_ms=0)
                return row
        if dry_run:
            row.update(status="dry_run")
        else:
            try:
                timeout = float(item.get("fetch_job", {}).get("timeout_seconds", 300)) if isinstance(item.get("fetch_job"), dict) else 300
                detail = _run_fetch_job(item, str(action["partition"]), timeout, root)
                _verify_partition(item, str(action["partition"]), root)
                row.update(status="ok", detail=detail)
            except Exception as exc:
                error_code = getattr(exc, "error_code", "FETCH_JOB_FAILED")
                stderr = getattr(exc, "stderr", "")
                row.update(status="failed", retryable=True, error_code=error_code,
                           error=type(exc).__name__ + ":" + str(exc), stderr=str(stderr)[-300:])
                row["detail"] = {"item_id": item["id"], "error_code": error_code,
                                  "stderr_tail": str(stderr)[-300:],
                                  "elapsed_ms": round((time.monotonic() - started) * 1000)}
        row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        if row.get("status") != "failed":
            row["detail"] = {"item_id": item["id"], "error_code": None,
                              "stderr_tail": "", "elapsed_ms": row["elapsed_ms"]}
        return row

    indexed = {str(action.get("id", "")): action for action in plan}
    levels: dict[str, int] = {}

    def level(item_id: str, stack: tuple[str, ...] = ()) -> int:
        if item_id in levels:
            return levels[item_id]
        if item_id in stack:
            return len(plan) + 1
        action = indexed[item_id]
        spec = by_id.get(item_id, {})
        deps = action.get("depends_on")
        if not isinstance(deps, list):
            deps = (spec.get("late_policy") or {}).get("depends_on", [])
        deps = [dep for dep in deps if not indexed.get(str(dep), {}).get("upgrade_daily_kline")]
        level_value = 0 if not deps else max(
            (level(str(dep), stack + (item_id,)) + 1 for dep in deps if str(dep) in indexed), default=0)
        levels[item_id] = level_value
        return level_value

    for item_id in indexed:
        level(item_id)
    action_status: dict[str, str] = {}

    def record_nonfetch(action: dict[str, Any]) -> dict[str, Any]:
        item_id = str(action.get("id", ""))
        item = by_id.get(item_id)
        kind = action.get("action")
        started = time.monotonic()
        try:
            if not item:
                return {"id": item_id, "action": kind, "status": "failed",
                        "error_code": "UNKNOWN_ITEM", "error": "unknown_item", "stderr": "", "elapsed_ms": 0}
            if kind == "GAP":
                if not dry_run:
                    _append_gap(root, trade_date, action, clock.isoformat())
                return {"id": item_id, "action": kind, "status": "recorded" if not dry_run else "dry_run",
                        "elapsed_ms": round((time.monotonic() - started) * 1000)}
            if kind == "NEEDS_OPERATOR":
                return {"id": item_id, "action": kind, "status": "needs_operator",
                        "detail": action.get("reason", "manual_reference_import_required"),
                        "partition": action.get("partition"), "elapsed_ms": 0}
            if kind == "DEFER":
                if not item.get("fetch_job"):
                    return {"id": item_id, "action": kind, "status": "failed",
                            "error_code": "FETCH_JOB_UNAVAILABLE", "error": "independent_fetch_job_unconfigured",
                            "stderr": "", "elapsed_ms": 0}
                phases = _quiet_targets(item, phase, clock)
                deferred.append({"id": item_id, "partition": action.get("partition"),
                                 "fetch_job": item.get("fetch_job"), "run_in_phases": phases,
                                 "cost": (item.get("cost") or {}).get("class", "medium")})
                return {"id": item_id, "action": kind, "status": "deferred", "run_in_phases": phases,
                        "elapsed_ms": 0}
            if kind == "NO_FETCH_JOB":
                return {"id": item_id, "action": kind, "status": "failed",
                        "error_code": "FETCH_JOB_UNAVAILABLE",
                        "error": action.get("reason", "independent_fetch_job_unconfigured"), "stderr": "", "elapsed_ms": 0}
            return {"id": item_id, "action": kind, "status": "skipped", "elapsed_ms": 0}
        except Exception as exc:
            return {"id": item_id, "action": kind, "status": "failed",
                    "error_code": getattr(exc, "error_code", "PLAN_ITEM_FAILED"),
                    "error": type(exc).__name__ + ":" + str(exc),
                    "stderr": str(getattr(exc, "stderr", ""))[-300:], "retryable": True,
                    "elapsed_ms": round((time.monotonic() - started) * 1000)}

    for current_level in sorted(set(levels.values())):
        actions = [indexed[item_id] for item_id in indexed if levels[item_id] == current_level]
        ready: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for action in actions:
            item_id = str(action.get("id", ""))
            spec = by_id.get(item_id, {})
            deps = action.get("depends_on")
            if not isinstance(deps, list):
                deps = (spec.get("late_policy") or {}).get("depends_on", [])
            deps = [dep for dep in deps if not indexed.get(str(dep), {}).get("upgrade_daily_kline")]
            failed_deps = [str(dep) for dep in deps if str(dep) in indexed and
                           action_status.get(str(dep)) not in {"ok", "dry_run", "recorded", "queued"}]
            if failed_deps and action.get("action") == "FETCH_NOW":
                row = {"id": item_id, "action": "FETCH_NOW", "partition": str(action.get("partition", "")),
                       "status": "DEPENDENCY_FAILED",
                       "error_code": "DEPENDENCY_FAILED", "failed_dependencies": failed_deps,
                       "retryable": True, "cost": (by_id.get(item_id, {}).get("cost") or {}).get("class", "medium"),
                       "elapsed_ms": 0}
            elif action.get("action") == "FETCH_NOW":
                item = by_id.get(item_id)
                if item is None:
                    row = {"id": item_id, "action": "FETCH_NOW", "status": "failed",
                           "error_code": "UNKNOWN_ITEM", "retryable": True, "elapsed_ms": 0}
                else:
                    ready.append((item, action))
                    continue
            else:
                row = record_nonfetch(action)
            results.append(row)
            action_status[item_id] = row["status"]
        def after_queued(pair):
            deps = pair[1].get("depends_on")
            if not isinstance(deps, list):
                deps = (pair[0].get("late_policy") or {}).get("depends_on", [])
            deps = [dep for dep in deps if not indexed.get(str(dep), {}).get("upgrade_daily_kline")]
            return any(action_status.get(str(dep)) == "queued" for dep in deps)

        light = [pair for pair in ready if pair[1].get("lane") == "parallel" and not after_queued(pair)]
        serial = [pair for pair in ready if pair[1].get("lane") != "parallel" or after_queued(pair)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            if enqueue_only:
                layer_results = []
                for item, action in light:
                    if not _claim_daily_kline_load_override(root, action, now=clock, phase=phase,
                                                            dry_run=bool(dry_run)):
                        layer_results.append({"id": item["id"], "action": "FETCH_NOW",
                                              "partition": str(action.get("partition", "")),
                                              "status": "already_queued", "elapsed_ms": 0})
                        continue
                    deps = action.get("depends_on")
                    if not isinstance(deps, list):
                        deps = (item.get("late_policy") or {}).get("depends_on", [])
                    item_cost = (item.get("cost") or {}).get("class", "medium")
                    run_phases = ([phase] if phase else ["NIGHT"]) if item_cost == "light" else _quiet_targets(item, phase, clock)
                    deferred.append({"id": item["id"], "partition": action.get("partition"),
                                     "fetch_job": item.get("fetch_job"), "run_in_phases": run_phases,
                                     "cost": item_cost,
                                     "depends_on": [str(dep) for dep in deps if str(dep) in indexed]})
                    layer_results.append({"id": item["id"], "action": "FETCH_NOW",
                                          "partition": str(action.get("partition", "")),
                                          "status": "queued", "run_in_phases": run_phases,
                                          "cost": item_cost,
                                          **({"load_override_once": True} if action.get("load_override_once") else {}),
                                          "elapsed_ms": 0})
            else:
                futures = [pool.submit(execute, pair) for pair in light]
                layer_results = [future.result() for future in futures]
        for item, action in serial:
            if enqueue_only:
                if not _claim_daily_kline_load_override(root, action, now=clock, phase=phase,
                                                        dry_run=bool(dry_run)):
                    layer_results.append({"id": item["id"], "action": "FETCH_NOW",
                                          "partition": str(action.get("partition", "")),
                                          "status": "already_queued", "elapsed_ms": 0})
                    continue
                deps = action.get("depends_on")
                if not isinstance(deps, list):
                    deps = (item.get("late_policy") or {}).get("depends_on", [])
                item_cost = (item.get("cost") or {}).get("class", "medium")
                run_phases = ([phase] if phase else ["NIGHT"]) if item_cost == "light" else _quiet_targets(item, phase, clock)
                deferred.append({"id": item["id"], "partition": action.get("partition"),
                                 "fetch_job": item.get("fetch_job"), "run_in_phases": run_phases,
                                 "cost": item_cost,
                                 "depends_on": [str(dep) for dep in deps if str(dep) in indexed]})
                layer_results.append({"id": item["id"], "action": "FETCH_NOW",
                                      "partition": str(action.get("partition", "")),
                                      "status": "queued", "run_in_phases": run_phases,
                                      "cost": item_cost,
                                      **({"load_override_once": True} if action.get("load_override_once") else {}),
                                      "elapsed_ms": 0})
                continue
            # Non-light fetches (minutes: limit_reasons ~9min, daily bars) must not hold the boot chain, which blocks the
            # scheduler. Hand them to the scheduler as independent fetch_item instances (3 attempts, backoff, one alert).
            if dry_run:
                layer_results.append(execute((item, action)))
                continue
            deps = action.get("depends_on")
            if not isinstance(deps, list):
                deps = (item.get("late_policy") or {}).get("depends_on", [])
            item_cost = (item.get("cost") or {}).get("class", "medium")
            run_phases = (_quiet_targets(item, phase, clock) if item_cost == "heavy"
                          else ([phase] if phase else ["NIGHT"]))
            deferred.append({"id": item["id"], "partition": action.get("partition"),
                             "fetch_job": item.get("fetch_job"), "run_in_phases": run_phases,
                             "cost": item_cost,
                             "depends_on": [str(dep) for dep in deps if str(dep) in indexed]})
            if action.get("upgrade_daily_kline") and not dry_run:
                state_path = root / "guardian-upgrade-attempts.json"
                try:
                    upgrade_attempts = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    upgrade_attempts = {}
                budget_key = str(action.get("partition"))
                upgrade_attempts.setdefault(budget_key, {"status": "queued", "attempts": 0})
                from ..data_pipeline.storage import _atomic_write, canonical_json
                _atomic_write(state_path, canonical_json(upgrade_attempts) + b"\n")
            layer_results.append({"id": item["id"], "action": "FETCH_NOW", "partition": str(action.get("partition", "")),
                                  "status": "queued", "run_in_phases": run_phases,
                                  "cost": item_cost, "elapsed_ms": 0})
        results.extend(layer_results)
        action_status.update({row["id"]: row["status"] for row in layer_results})
    detail = {"trade_date": trade_date, "results": results, "deferred": deferred,
              "dry_run": dry_run}
    failures = sum(x.get("status") in {"failed", "unavailable"} for x in results)
    successes = len(results) - failures
    status = "failed" if failures and not successes else "degraded" if failures else "ok"
    return _result(status, detail, retryable=bool(failures))


def fetch_item(item_id: str, partition: str, root: str | Path, *,
               dry_run: bool | None = None, now: datetime | str | None = None,
               phase: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    root = Path(root).expanduser()
    if dry_run is None:
        dry_run = os.environ.get("GUARDIAN_DRY_RUN") == "1"
    item = next((x for x in load_registry() if x["id"] == item_id), None)
    if item is None:
        return _result("failed", {"id": item_id}, error_code="UNKNOWN_ITEM")
    if dry_run:
        return _result("ok", {"id": item_id, "partition": partition, "dry_run": True})
    upgrade = item_id == "daily_kline"
    clock = _now(now, partition) if now is not None else datetime.now().astimezone()
    if upgrade:
        from .reconcile_plan import daily_kline_retry_availability
        state = _load_daily_kline_upgrade_states(root).get(partition)
        available, reason = daily_kline_retry_availability(state, clock, phase, partition)
        if not available:
            detail = {"id": item_id, "partition": partition,
                      "upgrade": {**(state if isinstance(state, dict) else {}),
                                  "throttled": reason == "minimum_interval",
                                  "deferred": reason}}
            return _result("degraded", detail)
    try:
        _argv, timeout = _fetch_spec(item)
        result = _run_fetch_job(item, partition, timeout, root)
        _verify_partition(item, partition, root, clock)
        if upgrade:
            state = _record_daily_kline_upgrade(root, partition, succeeded=True, now=clock, phase=phase)
            if state:
                _record_daily_kline_upgrade_gap(root, partition, state, clock)
            if state and state.get("status") != "resolved":
                return _result("degraded", {"id": item_id, "partition": partition, "result": result,
                                              "upgrade": state})
        return _result("ok", {"id": item_id, "partition": partition, "result": result})
    except Exception as exc:
        upgrade_state = (_record_daily_kline_upgrade(root, partition, succeeded=False,
                                                     now=clock, phase=phase) if upgrade else None)
        if upgrade_state:
            _record_daily_kline_upgrade_gap(root, partition, upgrade_state, clock)
        code = getattr(exc, "error_code", "FETCH_FAILED")
        detail = {"item_id": item_id, "partition": partition,
                                   "error": type(exc).__name__ + ":" + str(exc),
                                   "error_code": code,
                                   "stderr_tail": str(getattr(exc, "stderr", ""))[-300:],
                                   "elapsed_ms": round((time.monotonic() - started) * 1000)}
        if upgrade_state:
            detail["upgrade"] = upgrade_state
        if upgrade_state and upgrade_state.get("status") in {"GAP_WINDOW_EXHAUSTED", "GAP_ESCALATED"}:
            return _result("degraded", detail)
        return _result("failed", detail, error_code=code,
                       retryable=not upgrade_state or upgrade_state.get("status") == "queued")
