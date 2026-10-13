"""Read-only live auction node composed from the existing session components.

Timer artifacts are written atomically before ledger recording and coordinator
acknowledgement. If the process crashes before ``ledger.record``, restart
dispatches the timer again. After restart, ``fired_time_ms``, ``late_by_ms``,
and the artifact hash depend on the restart polling time; identical rewritten
bytes are guaranteed only for identical observed inputs (at-least-once
dispatch with idempotent artifact content under the same inputs).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .calendar import build_calendar_snapshot
from .clock import local_datetime_ms
from .execution_ledger import ExecutionLedger
from .session import build_a_share_session_plan
from .session_runtime import SessionRuntimeCoordinator
from .timers import TimerFiring, TimerSpec


AUCTION_NODE_TIMERS = (
    TimerSpec("AUCTION_0926", "09:26:00"),
    TimerSpec("OPENING_0932", "09:32:00"),
)


def _single_day_session(trade_date: str):
    day = date.fromisoformat(trade_date)
    calendar = build_calendar_snapshot(
        (day,), version="explicit-single-day-v1", declared_valid_from=day,
        declared_valid_to=day, source_guard_valid_from=day,
        source_guard_valid_to=day, source_id="auction_node_explicit_trade_date",
    )
    return calendar, build_a_share_session_plan(trade_date, calendar)


def _firing_mapping(firing: TimerFiring) -> dict[str, Any]:
    return {
        "timer_id": firing.timer_id,
        "scheduled_time_ms": firing.scheduled_time_ms,
        "fired_time_ms": firing.fired_time_ms,
        "trigger_basis": firing.trigger_basis,
        "origin": firing.origin,
        "late_by_ms": firing.late_by_ms,
        "session_plan_hash": firing.session_plan_hash,
        "content_hash": firing.content_hash,
    }


def _write_node_run(out_dir: Path, value: Mapping[str, Any]) -> None:
    path = out_dir / "node_run.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def run_auction_node(
    *, trade_date: str, out_dir: Path, live_client: Any, ledger_path: Path,
    read_times: Sequence[str], live_pool_previous_date: str | None = None,
    td_rows_path: Path | None = None, confirm_read_only: bool = False,
    sleep_until: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Run one live, read-only auction session and persist timer dispatches locally.

    A test sleeper may return an explicit epoch millisecond value for the
    read's observation clock; a normal sleeper returns ``None``.
    """
    if not confirm_read_only:
        raise RuntimeError("live source requires confirm_read_only=True")
    if not isinstance(trade_date, str) or len(trade_date) != 10:
        raise ValueError("trade_date must be strict YYYY-MM-DD")
    calendar, session_plan = _single_day_session(trade_date)
    coordinator = SessionRuntimeCoordinator(
        trade_date=trade_date, calendar=calendar, session_plan=session_plan,
        timer_specs=AUCTION_NODE_TIMERS,
        q2_optional_timer_ids=("AUCTION_0926",),
    )
    ledger = ExecutionLedger(ledger_path)
    restored_firings = ledger.firings(trade_date)
    coordinator.restore_completed(restored_firings)
    restored_ids = sorted(item.timer_id for item in restored_firings)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_node_run(out_dir, {"status": "RUNNING", "trade_date": trade_date})
    run_index = 0
    while (out_dir / ("shadow_run_%03d" % run_index)).exists():
        run_index += 1
    shadow_dir = out_dir / ("shadow_run_%03d" % run_index)
    first_observation: int | None = None
    acknowledged: list[str] = []
    timers_pending: set[str] = set()
    readiness_delayed: set[str] = set()
    polls = 0

    def on_poll(record: Mapping[str, Any]) -> None:
        nonlocal first_observation, polls
        polls += 1
        observed_at_ms = int(record["observed_at_ms"])
        if first_observation is None:
            first_observation = observed_at_ms
        late_start = first_observation > local_datetime_ms(trade_date, "09:26:00")
        origin = "RECOVERY_CATCHUP" if late_start else "NORMAL"
        runtime_poll = coordinator.poll(
            as_of_ms=observed_at_ms, q2=record["snapshot"], origin=origin,
        )
        timers_pending.update(runtime_poll.deferred_timer_ids)
        readiness_delayed.update(runtime_poll.deferred_timer_ids)
        for firing in runtime_poll.dispatchable_firings:
            if (
                firing.timer_id == "OPENING_0932"
                and runtime_poll.readiness.status != "READY"
            ):
                timers_pending.add(firing.timer_id)
                readiness_delayed.add(firing.timer_id)
                continue
            anchor_name = "anchor_table_%03d.json" % int(record["poll"])
            anchor_path = Path(record["out_dir"]) / anchor_name
            anchor_digest = hashlib.sha256(anchor_path.read_bytes()).hexdigest()
            timer_path = out_dir / ("timer_%s.json" % firing.timer_id)
            artifact = {
                **_firing_mapping(firing),
                "poll": int(record["poll"]),
                "executed_at_ms": observed_at_ms,
                "executed_late_by_ms": observed_at_ms - firing.scheduled_time_ms,
                "executed_poll": int(record["poll"]),
                "late_by_ms_semantics": "first_firing_delay",
                # Keep the durable artifact stable across shadow_run_NNN restarts.
                "anchor_table_file": anchor_path.name,
                "anchor_table_sha256": anchor_digest,
                "cutoff_fidelity": (
                    "AT_CUTOFF"
                    if (
                        observed_at_ms - firing.scheduled_time_ms <= 30000
                        and firing.origin == "NORMAL"
                        and firing.timer_id not in readiness_delayed
                    )
                    else "AFTER_CUTOFF_CURRENT_STATE"
                ),
                "q2_readiness": runtime_poll.readiness.status,
            }
            temporary = timer_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, timer_path)
            ledger.record(trade_date, firing)
            coordinator.acknowledge(firing)
            acknowledged.append(firing.timer_id)
            timers_pending.discard(firing.timer_id)

    try:
        from examples.shadow_task008_redis_auction_session import run_shadow

        run_shadow(
            source="live", out_dir=shadow_dir, live_client=live_client,
            trade_date=trade_date, confirm_read_only=True, read_times=read_times,
            poll_callback=on_poll, seal_captures=True,
            live_pool_previous_date=live_pool_previous_date, td_rows_path=td_rows_path,
            sleep_until=sleep_until,
        )
        cold_start = json.loads((shadow_dir / "cold_start.json").read_text(encoding="utf-8"))
        result = {
            "trade_date": trade_date,
            "start_class": cold_start.get("start_class"),
            "polls": polls,
            "timers_acknowledged": acknowledged,
            "timers_restored_from_ledger": restored_ids,
            "ledger_path": str(Path(ledger_path)),
            "timers_pending": sorted(timers_pending),
            "status": "DONE" if not timers_pending else "DONE_WITH_PENDING",
            "shadow_dir": str(shadow_dir),
            "shadow_status": "DONE",
        }
        _write_node_run(out_dir, result)
    except Exception as error:
        _write_node_run(out_dir, {
            "status": "FAILED",
            "trade_date": trade_date,
            "exception_type": type(error).__name__,
        })
        raise
    return result


__all__ = ["AUCTION_NODE_TIMERS", "run_auction_node"]
