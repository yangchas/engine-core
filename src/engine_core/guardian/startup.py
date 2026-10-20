"""Read-only implementations of the configured guardian startup events."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .registry import items_for_phase, load_registry, stale_items

PHASES = ("NIGHT", "PRE_MARKET", "AUCTION", "AUCTION_END", "OPEN",
          "INTRADAY_AM", "MIDDAY_BREAK", "PM_OPEN", "INTRADAY_PM",
          "CLOSE_AUCTION", "CLOSE", "POST_MARKET")


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
    trading = store.is_trading_day(trade_date)
    previous = store.previous_trade_date(trade_date).isoformat()
    following = store.next_trade_date(trade_date).isoformat()
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
                   root: str | Path, *, now: datetime | str | None = None) -> dict[str, Any]:
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
    try:
        import os
        from .reconcile_plan import reconcile_plan
        catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        try:
            _, calendar = _calendar(root, trade_date)
            previous = calendar["prev"]
        except Exception:
            previous = None
        plan = reconcile_plan(clock, catalog, trade_date=trade_date, prev_trade_date=previous,
                              phase=phase, load1=os.getloadavg()[0], cpus=os.cpu_count())
    except Exception as exc:  # the plan is advisory; never fail the boot check for it
        plan = [{"id": "*", "action": "PLAN_ERROR", "error": type(exc).__name__}]
    detail = {"phase": phase, "trade_date": trade_date, "gaps": gaps, "plan": plan,
              "recheck_required": bool(health.get("status") == "failed" or health.get("calendar", {}).get("status") == "failed")}
    return _result("degraded" if gaps or detail["recheck_required"] else "ok", detail)
