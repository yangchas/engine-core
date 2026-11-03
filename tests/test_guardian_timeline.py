import copy
import json
from pathlib import Path

from engine_core.guardian.timeline import expand, family_of, to_timeline

SCHEDULE = json.loads((Path(__file__).resolve().parents[1] / "src" / "engine_core" / "guardian"
                       / "schedule.json").read_text(encoding="utf-8"))


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def test_the_shipped_schedule_survives_the_timeline_round_trip_unchanged():
    assert canon(expand(to_timeline(SCHEDULE))) == canon(SCHEDULE)


def test_every_event_appears_once_on_the_time_axis_or_in_startup():
    doc = to_timeline(SCHEDULE)
    ids = [item["id"] for point in doc["timeline"] for item in point["run"]] + [item["id"] for item in doc["startup"]]
    assert sorted(ids) == sorted(event["event_id"] for event in SCHEDULE["events"]) and doc["order"] == [
        event["event_id"] for event in SCHEDULE["events"]]
    assert [point["at"] for point in doc["timeline"]] == sorted(point["at"] for point in doc["timeline"])
    assert len(doc["startup"]) == sum(1 for event in SCHEDULE["events"] if event["trigger_type"] == "startup")


def test_a_family_of_repeated_triggers_is_one_function_with_thin_triggers():
    doc = to_timeline(SCHEDULE)
    assert family_of("limit_up_today_091600") == "limit_up_today" and family_of("fetch_item") == "fetch_item"
    function = doc["functions"]["limit_up_today"]
    assert function["handler"] == "intraday_data" and function["deadline_after_seconds"] == 120
    triggers = [item for point in doc["timeline"] for item in point["run"] if item["fn"] == "limit_up_today"]
    assert len(triggers) == 18 and all(set(item) <= {"fn", "id", "args", "deadline"} for item in triggers)
    assert len(json.dumps(doc)) < len(json.dumps(SCHEDULE)) * 0.7


def test_a_tampered_function_body_no_longer_expands_to_the_schedule():
    doc = to_timeline(SCHEDULE)
    doc["functions"]["limit_up_today"]["cost"] = "heavy"
    assert canon(expand(doc)) != canon(SCHEDULE)


def test_clocks_without_seconds_keep_their_absolute_deadline():
    schedule = {"schema_version": 3, "revision": 1, "timezone": "Asia/Shanghai", "global": {}, "profiles": {},
                "events": [{"event_id": "a_0900", "handler": "h", "at": "09:00", "deadline": "09:02:00"},
                           {"event_id": "a_1000", "handler": "h", "at": "10:00", "deadline": "10:02:00"}]}
    doc = to_timeline(copy.deepcopy(schedule))
    assert "deadline_after_seconds" not in doc["functions"]["a"]
    assert canon(expand(doc)) == canon(schedule)
