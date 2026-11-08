# DATA: spec (guardian timeline/schedule config)
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
    done = subprocess.run(["go", "run", "-buildvcs=false", ".", "migrate", "--from", str(source), "--to", str(target)],
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


# ---- P2.3: validation ------------------------------------------------------------------------------------------

from engine_core.guardian.timeline_check import validate_timeline


def test_the_shipped_schedule_has_no_validation_problems():
    assert validate_timeline(to_timeline(SCHEDULE)) == []


def _doc():
    return to_timeline(copy.deepcopy(SCHEDULE))


def _first(doc, fn):
    return next(item for point in doc["timeline"] for item in point["run"] if item["fn"] == fn)


@pytest.mark.parametrize("break_it,expected", [
    (lambda d: _first(d, "limit_up_today").update(fn="no_such_function"), "unknown_function:no_such_function"),
    (lambda d: _first(d, "kaipan_hot").update(policy="no_such_policy"), "unknown_policy:no_such_policy"),
    (lambda d: _first(d, "limit_up_today").update(deadline="09:00:00"), "deadline_before_start"),
    (lambda d: _first(d, "limit_up_today").update(available_after="23:00:00"), "available_after_later_than_start"),
    (lambda d: _first(d, "limit_up_today").update(late_until="00:01:00", deadline="09:40:00"), "late_until_before_deadline"),
    (lambda d: d["functions"]["limit_up_today"].update(phases=["POST_MARKET"]), "start_in_a_phase_the_event_does_not_allow"),
    (lambda d: d["functions"]["limit_up_today"].update(handler="no_such_handler"), "unknown_handler:no_such_handler"),
    (lambda d: d["functions"]["limit_up_today"].update(gate="no_such_gate"), "unknown_gate:no_such_gate"),
    (lambda d: _first(d, "limit_up_today").update(depends_on=["ghost_event"]), "depends_on_unknown_event:ghost_event"),
    (lambda d: _first(d, "limit_up_today").update(after=["close_recap"]), "after_runs_later:close_recap"),
    (lambda d: d["policies"]["policy_05"].update(ladder=[]), "empty_ladder"),
    (lambda d: d["policies"]["daily_kline_close"]["ladder"][1].update(name="wencai"), "duplicate_tier_name"),
    (lambda d: d["policies"]["policy_05"]["ladder"][0].update(attempts=0), "attempts_below_1"),
    (lambda d: _first(d, "limit_up_today").update(id=_first(d, "kaipan_hot")["id"]), "duplicate_id"),
    (lambda d: d["order"].pop(), "does_not_list_each_trigger_once"),
    (lambda d: d["timeline"][3].update(at="25:61:00"), "bad_clock:at"),
])
def test_each_class_of_broken_timeline_is_rejected(break_it, expected):
    doc = _doc()
    break_it(doc)
    assert any(expected in problem for problem in validate_timeline(doc)), validate_timeline(doc)


def test_premarket_prep_degradation_is_scoped_to_startup_and_slice():
    events = {event["event_id"]: event for event in SCHEDULE["events"]}
    for event_id in ("startup_0830", "startup_0900", "premarket_slice_0850"):
        assert events[event_id]["args"]["prep_degradable_steps"] == ["plate_amount_baseline"]
    for event_id in ("static_data_refresh_0850", "kaipan_refresh_0850"):
        assert "prep_degradable_steps" not in events[event_id].get("args", {})


def test_plate_amount_baseline_event_and_policy_are_scheduled_independently():
    events = {event["event_id"]: event for event in SCHEDULE["events"]}
    event = events["plate_amount_baseline_0815"]
    assert (event["at"], event["deadline"], event["handler"]) == ("08:15:00", "08:25:00", "fetch_item")
    assert event["args"]["item_id"] == "plate_amount_baseline"
    assert event["calendar"] == "trading_day" and event["gate"] == "calendar_ready"
    assert event["phases"] == ["NIGHT", "PRE_MARKET"] and event["on_deadline"] == "degrade"
    assert event["profile"] == "policy_12"
    policy = SCHEDULE["profiles"]["policy_12"]
    assert policy["retry_backoff"] == "" and len(policy["ladder"]) == 1
    tier = policy["ladder"][0]
    assert (tier["attempts"], tier["timeout"], tier["backoff"]) == (1, "540s", "")
    for event_id in ("startup_0830", "premarket_slice_0850", "startup_0900"):
        assert "plate_amount_baseline" in events[event_id]["args"]["prep_degradable_steps"]


def test_auction_amount_facts_uses_trading_day_calendar():
    events = {event["event_id"]: event for event in SCHEDULE["events"]}
    event = events["auction_amount_facts_0530"]
    baseline = events["plate_amount_baseline_0815"]
    assert (event["calendar"], event["trigger_type"], event["gate"]) == (
        baseline["calendar"], baseline["trigger_type"], baseline["gate"])
    assert event["calendar"] == "trading_day"
    assert "NIGHT" in event["phases"] and "NON_TRADING" not in event["phases"]


def test_auction_amount_facts_daily_schedule_and_policy():
    events = {event["event_id"]: event for event in SCHEDULE["events"]}
    event = events["auction_amount_facts_0530"]
    assert (event["at"], event["deadline"], event["calendar"], event["gate"]) == (
        "05:30:00", "07:30:00", "trading_day", "calendar_ready")
    assert event["handler"] == "postmarket_data"
    assert event["cost"] == "heavy"
    assert event["args"] == {"dataset": "auction_amount_facts", "window": 64, "max_days": 64}
    assert event["profile"] == "policy_13"
    policy = SCHEDULE["profiles"]["policy_13"]
    assert policy["retry_until_late"] is True
    tier, = policy["ladder"]
    assert (tier["attempts"], tier["backoff"], tier["backoff_cap"], tier["timeout"], tier["until"]) == (
        3, "10m*2", "20m", "3600s", "late_until")
    for event_id in ("startup_0830", "startup_0900", "premarket_slice_0850"):
        assert events[event_id]["args"]["prep_degradable_steps"] == ["plate_amount_baseline"]
    assert (events["plate_amount_baseline_0815"]["at"], events["plate_amount_baseline_0815"]["deadline"],
            events["plate_amount_baseline_0815"]["profile"]) == ("08:15:00", "08:25:00", "policy_12")


def test_stock_listing_precedes_backfill_on_trading_days():
    events = {event["event_id"]: event for event in SCHEDULE["events"]}
    listing = events["stock_listing_0045"]
    backfill = events["daily_kline_backfill"]
    assert listing["calendar"] == "trading_day"
    assert (listing["trigger_type"], listing["phases"]) == (backfill["trigger_type"], backfill["phases"])
    assert listing["args"]["target"] == backfill["args"]["target"] == "prev_trade_day"
    assert listing["at"] < backfill["at"]
    assert listing.get("cost") != "heavy"


def test_daily_dde_runs_at_0200_for_previous_trade_day():
    events = {event["event_id"]: event for event in SCHEDULE["events"]}
    event = events["daily_dde_0200"]
    backfill = events["daily_kline_backfill"]
    assert (event["at"], event["deadline"]) == ("02:00:00", "06:30:00")
    assert (event["calendar"], event["trigger_type"]) == (backfill["calendar"], backfill["trigger_type"])
    assert event["args"]["target"] == backfill["args"]["target"] == "prev_trade_day"
    assert event["phases"] == backfill["phases"] == ["any"]
    assert event["args"]["max_workers"] == 1
    assert not event.get("gate") and not event.get("after")
    assert "daily_dde_postclose" not in events


def test_daily_dde_registry_uses_new_event():
    registry = json.loads((ROOT / 'src/engine_core/guardian/datasets.registry.json').read_text())
    entry = next(item for item in registry['items'] if item['id'] == 'daily_dde')
    assert 'daily_dde_postclose' not in entry['cadence'] + entry['retry']
    assert entry['guardian_event'] == 'daily_dde_0200'


def test_adjust_factor_events_follow_kline_windows():
    events = {event['event_id']: event for event in SCHEDULE['events']}
    post = events['adjust_factor_postclose']
    night = events['adjust_factor_0115']
    assert not post.get('after') and not post.get('depends_on')
    assert not night.get('after') and not night.get('depends_on')
    assert post['at'] == '17:10:00' and post['late_until'] == '20:30:00'
    assert post['calendar'] == 'trading_day' and post['data_date_offset'] == 0
    assert night['at'] == '01:15:00' and night['late_until'] == '05:00:00'
    assert night['calendar'] == 'every_day' and night['args']['target'] == 'prev_trade_day'
    assert all(event['cost'] != 'heavy' and not event.get('gate') for event in (post, night))


def test_price_volume_events_follow_kline_windows():
    events = {event['event_id']: event for event in SCHEDULE['events']}
    post = events['price_volume_postclose']
    night = events['price_volume_0130']
    assert post['at'] == '17:25:00' and post['late_until'] == '20:45:00'
    assert post['calendar'] == 'trading_day' and post['data_date_offset'] == 0
    assert night['at'] == '01:30:00' and night['late_until'] == '05:30:00'
    assert night['calendar'] == 'every_day' and night['args']['target'] == 'prev_trade_day'
    assert all(event['cost'] != 'heavy' and not event.get('gate') and not event.get('after') and not event.get('depends_on') for event in (post, night))


def test_chip_and_dde_events_use_independent_time_windows():
    events = {event['event_id']: event for event in SCHEDULE['events']}
    for name, at, late, calendar in (
        ('chip_peak_postclose', '17:40:00', '20:50:00', 'trading_day'),
        ('chip_peak_0145', '01:45:00', '05:30:00', 'every_day'),
        ('dde_derived_0445', '04:45:00', '08:00:00', 'every_day'),
    ):
        event = events[name]
        assert (event['at'], event['late_until'], event['calendar']) == (at, late, calendar)
        assert event['data_date_offset'] == 0
        if calendar == 'every_day':
            assert event['args']['target'] == 'prev_trade_day'
        assert event['cost'] != 'heavy' and not any(event.get(k) for k in ('gate', 'after', 'depends_on'))
