"""P1/C4: what the sweep does with exit codes. The catalog is the REAL captured snapshot; only the build command is
replaced by a runner that returns scripted exit codes (a build that exits 0 is modelled as the node becoming fresh)."""
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from chain_support import TODAY, read_manifest, snapshot  # noqa: E402
from engine_core.data_pipeline.chain import FRESH, exit_code, load_chain_config, make_status_fn, sweep  # noqa: E402

REAL_STATUS = make_status_fn(read_manifest)
SNAP = snapshot()
ALL = dataclasses.replace(load_chain_config("daily_kline"), max_builds_per_run=100)


class World:
    def __init__(self, codes=None, config=ALL, lease=None, builds_nothing=()):
        self.codes, self.config, self.lease, self.builds_nothing = codes or {}, config, lease, set(builds_nothing)
        self.calls, self.rebuilt = [], set()

    def runner(self, dataset, day):
        self.calls.append((dataset, day))
        code = self.codes.get((dataset, day), 0)
        if code == 0 and (dataset, day) not in self.builds_nothing:
            self.rebuilt.add((dataset, day))             # a successful build makes the node fresh
        return code

    def status(self, snapshot_, dataset, day):
        return (FRESH, "") if (dataset, day) in self.rebuilt else REAL_STATUS(snapshot_, dataset, day)

    def run(self, **kwargs):
        return sweep(lambda: SNAP, self.config, today=TODAY, runner=self.runner, status_fn=self.status,
                     lease=self.lease, **kwargs)


class Lease:
    def __init__(self, free=True, renew=True):
        self.free, self.renewable, self.released = free, renew, False

    def acquire(self):
        return self.free

    def renew(self):
        return self.renewable

    def release(self):
        self.released = True


def test_a_sweep_builds_everything_due_newest_day_first_then_the_next_one_has_nothing_to_do():
    world = World()
    result = world.run()
    assert len(world.calls) == 22 and exit_code(result) == 0 and result["remaining"] == []
    assert world.calls[0][1] == TODAY and [day for _, day in world.calls] == sorted((d for _, d in world.calls), reverse=True)
    for day in {d for _, d in world.calls}:
        order = [name for name, d in world.calls if d == day]
        if "daily_indicators" in order and "daily_report_features" in order:
            assert order.index("daily_indicators") < order.index("daily_report_features")
    again = world.run()
    assert again["built"] == [] and exit_code(again) == 20 and len(world.calls) == 22      # no new calls


def test_the_per_run_cap_stops_the_sweep_and_asks_for_another_run():
    world = World(config=dataclasses.replace(ALL, max_builds_per_run=5))
    result = world.run()
    assert len(world.calls) == 5 and result["capped"] is True and exit_code(result) == 30
    assert len(result["remaining"]) == 17


def test_a_dependent_is_not_built_on_an_upstream_that_just_failed_or_is_not_ready():
    # a day where the real snapshot needs BOTH daily_indicators and daily_report_features rebuilt
    planned = World().run(dry_run=True)["plan"]
    day = next(i["day"] for i in planned if i["dataset"] == "daily_indicators"
               and any(j["day"] == i["day"] and j["dataset"] == "daily_report_features" for j in planned))
    world = World(codes={("daily_indicators", day): 30})
    result = world.run()
    assert ("daily_indicators", day) in world.calls and ("daily_report_features", day) not in world.calls
    assert [(i["dataset"], i["day"]) for i in result["waiting_on_failed_upstream"]] == [("daily_report_features", day)]
    assert exit_code(result) == 30 and result["retry"] == [{**result["retry"][0], "dataset": "daily_indicators", "day": day}]
    assert len(world.calls) > 2                      # the other days still got built


def test_a_deterministic_failure_is_recorded_other_nodes_continue_and_the_code_is_40():
    world = World(codes={("ladder_daily", TODAY): 40})
    result = world.run()
    assert [item["dataset"] for item in result["failed"]] == ["ladder_daily"] and exit_code(result) == 40
    assert len(world.calls) > 1 and ("ladder_daily", TODAY) in world.calls
    assert world.calls.count(("ladder_daily", TODAY)) == 1                                # not retried inside the run


def test_a_build_that_exits_zero_but_leaves_the_node_unfresh_is_not_repeated_and_is_reported():
    world = World(builds_nothing={("stock_day_facts", TODAY)})
    result = world.run()
    assert world.calls.count(("stock_day_facts", TODAY)) == 1
    assert [item["dataset"] for item in result["not_converged"]] == ["stock_day_facts"] and exit_code(result) == 40


def test_a_busy_lease_means_no_build_and_exit_20():
    lease = Lease(free=False)
    world = World(lease=lease)
    result = world.run()
    assert result == {"status": "BUSY"} and world.calls == [] and exit_code(result) == 20


def test_the_lease_is_released_and_a_lost_lease_stops_the_sweep_retryably():
    lease = Lease(renew=False)
    world = World(lease=lease)
    result = world.run()
    assert lease.released and world.calls == [] and exit_code(result) == 30
    assert result["retry"][0]["reason"] == "lease_lost"


def test_a_plan_only_run_calls_no_builder():
    world = World()
    result = world.run(dry_run=True)
    assert result["status"] == "PLAN" and len(result["plan"]) == 22 and world.calls == [] and exit_code(result) == 0
