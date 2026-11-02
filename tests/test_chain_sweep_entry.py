"""P1/C4-C5: the scheduled entry -- exit codes in, contract payload out; the registry runner; the args route; the events."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from chain_support import TODAY, read_manifest, snapshot  # noqa: E402
from engine_core.guardian import chain_sweep  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = json.loads((ROOT / "src/engine_core/guardian/schedule.json").read_text(encoding="utf-8"))
SNAP = snapshot()


def request(**chain):
    return {"trade_date": "2026-11-02", "args": {"chain": {"name": "daily_kline", "lease": "off", **chain}}}


def run(runner=None, **kwargs):
    return chain_sweep.run(request(), ROOT, runner=runner, snapshot_fn=lambda: SNAP, **kwargs)


def test_plan_only_returns_the_plan_without_running_anything(monkeypatch):
    monkeypatch.setattr(chain_sweep, "read_manifest_file", read_manifest)
    code, payload = chain_sweep.run(request(plan_only=True), ROOT, runner=lambda *_a: pytest.fail("no builds"),
                                    snapshot_fn=lambda: SNAP)
    assert (code, payload["status"]) == (0, "ok") and len(payload["detail"]["plan"]) == 22
    assert payload["detail"]["days"][-1] == TODAY


@pytest.mark.parametrize("scripted,expected", [
    (30, (75, "failed", True, "CHAIN_RETRY")),
    (40, (40, "failed", False, "CHAIN_FAILED")),
])
def test_exit_codes_map_to_the_guardian_contract(monkeypatch, scripted, expected):
    monkeypatch.setattr(chain_sweep, "read_manifest_file", read_manifest)
    code, payload = run(runner=lambda dataset, day: scripted)
    assert (code, payload["status"], payload["retryable"], payload["error_code"]) == expected


def test_a_busy_lease_is_not_applicable_and_runs_nothing(monkeypatch):
    class Busy:
        def acquire(self):
            return False

    code, payload = run(runner=lambda *_a: pytest.fail("no builds"), lease=Busy())
    assert (code, payload["status"]) == (20, "not_applicable")
    # guardiand treats exit 20 as done only when the reply says ALREADY_SATISFIED; anything else is a retryable failure
    assert payload["error_code"] == "ALREADY_SATISFIED"


def test_unknown_chain_or_missing_name_is_a_config_error_not_a_crash():
    assert chain_sweep.run({"trade_date": "2026-11-02", "args": {"chain": {"name": "nope"}}}, ROOT)[0] == 64
    assert chain_sweep.run({"trade_date": "2026-11-02", "args": {"chain": {}}}, ROOT)[0] == 64
    assert chain_sweep.run({"args": {"chain": {"name": "daily_kline"}}}, ROOT)[0] == 64


@pytest.mark.parametrize("raised,expected", [("FETCH_EXIT_30", 30), ("FETCH_EXIT_20", 20), ("FETCH_EXIT_40", 40),
                                             ("FETCH_TIMEOUT", 30), ("FETCH_JOB_FAILED", 40), (None, 0)])
def test_the_registry_runner_answers_with_an_exit_code_only(monkeypatch, raised, expected):
    from engine_core.guardian import startup

    def fake(item, day, timeout, root=None, schedule_args=None):
        assert item["id"] == "ladder_daily" and day == TODAY          # the registry fetch_job of the node, for that day
        if raised:
            raise startup.FetchJobError("x", raised)
        return {}

    monkeypatch.setattr(startup, "_run_fetch_job", fake)
    assert chain_sweep.registry_runner(ROOT)("ladder_daily", TODAY) == expected


def test_every_borrowed_job_route_has_a_scheduled_event_and_the_chain_events_reach_the_sweep():
    from examples import guardian_handler

    events = [e for e in SCHEDULE["events"] if e["handler"] == "boot_data_health"]
    for field, module_name, function in guardian_handler.BORROWED_JOBS:
        assert any((e.get("args") or {}).get(field) for e in events), field
        assert guardian_handler._borrowed_job("boot_data_health", {field: {"x": 1}}) == (module_name, function)
    assert guardian_handler._borrowed_job("boot_data_health", {}) is None
    assert guardian_handler._borrowed_job("premarket_data", {"chain": {"name": "daily_kline"}}) is None


def test_the_chain_events_sit_after_the_producers_and_before_the_consumers_of_the_evening():
    events = {e["event_id"]: e for e in SCHEDULE["events"]}
    sweeps = [e for e in SCHEDULE["events"] if e["event_id"].startswith("chain_sweep_")]
    assert [e["at"] for e in sweeps] == ["18:30:00", "20:30:00", "01:30:00", "08:20:00"]
    for event in sweeps:
        assert event["handler"] == "boot_data_health" and event["args"]["chain"]["name"] == "daily_kline"
        assert event["deadline"] > event["at"] and event["late_until"] > event["deadline"]   # a real start window
        ladder = SCHEDULE["profiles"][event["profile"]]["ladder"][0]
        assert event["args"]["chain"]["budget_seconds"] < int(ladder["timeout"].rstrip("m")) * 60   # never killed mid-build
    assert events["chain_sweep_2030"]["late_until"] <= events["daily_dde_postclose"]["at"]
    assert events["chain_sweep_0820"]["late_until"] < events["preopen_check_0835"]["at"]
    assert events["chain_sweep_0130"]["at"] > events["daily_kline_backfill"]["at"]                # after the 01:00 fill
