# DATA: spec - the repository's generated schedule; the 52 minutes are the observed length of the legacy per-symbol sweep (SHOW QUERIES 2026-11-06 00:55-01:22)
"""C695: the backfill's retries must outlast a known contention window instead of landing inside it.

2026-11-06: attempts at 01:00, 01:05 and 01:19 all ran while the legacy engine-next swept TD (00:30 - ~01:22); each
hit 0x073a and the third ended the day at 01:21 - one minute before the sweep stopped."""
import json
import re
from pathlib import Path

SCHEDULE = json.loads((Path(__file__).resolve().parents[1] / "src" / "engine_core" / "guardian" / "schedule.json")
                      .read_text(encoding="utf-8"))
SWEEP_MINUTES = 52


def minutes(text):
    value, unit = re.fullmatch(r"(\d+)([smh])", text).groups()
    return int(value) * {"s": 1 / 60, "m": 1, "h": 60}[unit]


def waits(rung):
    """Minutes between attempts: base, doubled each time (``5m*2``), capped."""
    base, factor = rung["backoff"].split("*")
    wait, cap = minutes(base), minutes(rung["backoff_cap"])
    result = []
    for _ in range(rung["attempts"] - 1):
        result.append(min(wait, cap))
        wait *= float(factor)
    return result


def test_the_waits_are_computed_the_way_the_driver_computes_them():
    assert waits({"attempts": 4, "backoff": "5m*2", "backoff_cap": "20m"}) == [5, 10, 20]


def test_the_backfill_keeps_trying_for_longer_than_the_contention_window_it_hit():
    event = next(e for e in SCHEDULE["events"] if e["event_id"] == "daily_kline_backfill")
    rung = SCHEDULE["profiles"][event["profile"]]["ladder"][0]
    assert sum(waits(rung)) > SWEEP_MINUTES, waits(rung)
