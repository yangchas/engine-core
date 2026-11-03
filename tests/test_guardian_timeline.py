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


# ---- P2.2: generator and equivalence gate -------------------------------------------------------------------

import shutil
import subprocess
import tempfile

import pytest

from engine_core.guardian.timeline import diff_schedules, generate, render_schedule

ROOT = Path(__file__).resolve().parents[1]


def test_generated_schedule_is_the_shipped_schedule_up_to_key_order():
    text = generate(to_timeline(SCHEDULE))
    assert diff_schedules(SCHEDULE, json.loads(text)) == []
    assert canon(json.loads(text)) == canon(SCHEDULE)


def test_generation_is_stable_text_not_just_equal_data():
    text = generate(to_timeline(SCHEDULE))
    assert generate(to_timeline(json.loads(text))) == text and render_schedule(SCHEDULE) == text


def test_diff_names_the_event_and_the_field():
    doc = to_timeline(SCHEDULE)
    doc["functions"]["limit_up_today"]["cost"] = "heavy"
    doc["timeline"][0]["run"][0]["id"] = doc["timeline"][0]["run"][0]["id"]
    found = diff_schedules(SCHEDULE, expand(doc))
    assert found and all(item.startswith("limit_up_today_") and item.endswith(":cost") for item in found)
    assert len(found) == 18


def test_an_unknown_event_key_is_rejected_instead_of_dropped():
    schedule = json.loads(json.dumps(SCHEDULE))
    schedule["events"][0]["surprise"] = 1
    with pytest.raises(ValueError, match="event_keys_not_in_canonical_order"):
        render_schedule(schedule)


def _go_config(schedule_text: str, workdir: Path, name: str) -> dict:
    source = workdir / f"{name}.schedule.json"
    source.write_text(schedule_text, encoding="utf-8")
    target = workdir / f"{name}.config.json"
    done = subprocess.run(["go", "run", ".", "migrate", "--from", str(source), "--to", str(target)],
                          cwd=ROOT / "native" / "guardiand", capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-400:]
    return json.loads(target.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not available")
def test_guardiand_migrate_reads_the_generated_schedule_as_the_shipped_one():
    shipped = (ROOT / "src" / "engine_core" / "guardian" / "schedule.json").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        expected = _go_config(shipped, work, "shipped")
        actual = _go_config(generate(to_timeline(SCHEDULE)), work, "generated")
        assert expected["events"] and canon(actual) == canon(expected)
        doc = to_timeline(SCHEDULE)
        doc["functions"]["limit_up_today"]["cost"] = "heavy"
        assert canon(_go_config(generate(doc), work, "mutant")) != canon(expected)
