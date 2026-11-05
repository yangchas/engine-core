# DATA: spec (the repository's schedule.json and the Go driver's request) + real:Kaipan recordings used by the sealed replay handler (tests/fixtures/real)
"""C692: a data function runs by itself for one date and time slice - no daemon, no full run."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import run_function  # noqa: E402

SCHEDULE = json.loads(run_function.SCHEDULE.read_text(encoding="utf-8"))
DAY = "2026-10-23"


def _event(event_id):
    return next(e for e in SCHEDULE["events"] if e["event_id"] == event_id)


def test_the_request_has_every_field_guardiand_sends():
    """The Go driver builds the handler request in one map literal; a field added there must be added here."""
    go = (ROOT / "native" / "guardiand" / "runtime.go").read_text(encoding="utf-8")
    line = next(l for l in go.splitlines() if l.lstrip().startswith("in := map[string]any{"))
    nested = {"on", "root", "ref", "params", "probe"}                         # keys of the sandbox / gate sub-maps
    sent = (set(re.findall(r'"([a-z_]+)":', line)) - nested) | {"timeout_seconds"}      # timeout is added on the next line
    request = run_function.request_for(_event("kaipan_hot_092110"), DAY, ["2026-10-22", DAY], SCHEDULE)
    assert sent <= set(request), sorted(sent - set(request))
    assert request["asof"] == f"{DAY}T09:21:10+08:00" and request["event"] == "kaipan_hot_092110"
    assert request["args"] == _event("kaipan_hot_092110")["args"]


def test_the_data_date_follows_the_event_offset_over_trading_days():
    dates = ["2026-10-21", "2026-10-22", DAY]
    assert run_function._data_date(DAY, 0, dates) == DAY and run_function._data_date(DAY, -1, dates) == "2026-10-22"


def test_a_time_slice_selects_its_events_in_schedule_order():
    chosen = run_function.select(SCHEDULE, event=None, start="09:15:00", until="09:30:00", handler=None)
    times = [run_function._at(e) for e in chosen]
    assert chosen and times == sorted(times) and all("09:15:00" <= t <= "09:30:00" for t in times)
    only = run_function.select(SCHEDULE, event=None, start="09:00:00", until="15:00:00", handler="kaipan_hot_capture")
    assert only and {e["handler"] for e in only} == {"kaipan_hot_capture"}


def test_a_function_runs_alone_and_answers_inside_the_contract(tmp_path):
    for sub in ("core-data", "replay", "home"):
        (tmp_path / sub).mkdir()
    from replay_day import _seed_calendar
    dates = _seed_calendar(tmp_path / "core-data")
    event = _event("kaipan_hot_092110")
    result = run_function.run_one(run_function.request_for(event, DAY, dates, SCHEDULE), event["handler"], tmp_path)
    assert result["problems"] == [] and result["reply"]["status"] == "ok" and result["exit"] == 0
    assert result["seconds"] < 30
