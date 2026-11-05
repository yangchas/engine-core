# DATA: spec - the repository's own generated schedule (src/engine_core/guardian/schedule.json), read as it is
"""C689: events that re-run the same prep work get the same time budget.

2026-11-05: startup_0830 (901 s) failed three times, then startup_0900 (300 s) had to redo the same ~8 minute prep and
timed out twice, 300 s each, while the work itself needs 260 s on an idle TD and more under load. The catch-up events
exist to finish what the primary left undone, so their budget is the work's, not a no-op's."""
import json
import re
from pathlib import Path

SCHEDULE = json.loads((Path(__file__).resolve().parents[1] / "src" / "engine_core" / "guardian" / "schedule.json")
                      .read_text(encoding="utf-8"))
PREP_HANDLER = "premarket_data"


def seconds(text: str) -> int:
    value, unit = re.fullmatch(r"(\d+)([smh])", text).groups()
    return int(value) * {"s": 1, "m": 60, "h": 3600}[unit]


def ladder_timeout(event: dict) -> int:
    ladder = SCHEDULE["profiles"][event["profile"]]["ladder"]
    return sum(seconds(rung["timeout"]) for rung in ladder[:1])


def prep_events() -> dict[str, dict]:
    """The events that run the full prep (prep_trade_day); static_data_refresh runs another function on the same handler."""
    return {event["event_id"]: event for event in SCHEDULE["events"]
            if event["handler"] == PREP_HANDLER and "static_data_refresh" not in (event.get("args") or {})}


def test_the_full_prep_runs_at_least_at_0830_0850_and_0900():
    assert {"startup_0830", "premarket_slice_0850", "startup_0900"} <= set(prep_events())


def test_every_event_that_re_runs_the_prep_has_the_primary_budget():
    events = prep_events()
    primary = ladder_timeout(events["startup_0830"])
    short = {event_id: ladder_timeout(event) for event_id, event in events.items() if ladder_timeout(event) < primary}
    assert not short, f"catch-up events with less time than the prep needs ({primary}s): {short}"
