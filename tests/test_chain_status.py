"""P1/C2-C3: freshness status and build plan, judged on the REAL catalog snapshot copied from the live system."""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from chain_support import TODAY, read_manifest, snapshot  # noqa: E402
from engine_core.data_pipeline.chain import (  # noqa: E402
    FRESH, MISSING, STALE, UNDECIDABLE, ChainConfigError, chain_days, load_chain_config, make_status_fn, plan)
from engine_core.data_pipeline.derived_daily import select_best  # noqa: E402

CONFIG = load_chain_config("daily_kline")
STATUS = make_status_fn(read_manifest)
SNAP = snapshot()
DAYS = chain_days(SNAP, CONFIG, TODAY)


def statuses(dataset):
    return {day: STATUS(SNAP, dataset, day)[0] for day in DAYS}


def test_the_window_is_the_newest_ten_sessions_that_have_daily_kline():
    assert len(DAYS) == 10 and DAYS[-1] == TODAY and DAYS == sorted(DAYS)


def test_status_counts_on_the_real_snapshot():
    # regression lock for the captured moment (11-02 11:3x): see fw/design/26-P1-TEST-DESIGN.md section 3
    assert Counter(statuses("daily_indicators").values()) == {FRESH: 7, UNDECIDABLE: 3}
    assert Counter(statuses("daily_report_features").values()) == {FRESH: 7, UNDECIDABLE: 3}
    assert Counter(statuses("ladder_daily").values()) == {FRESH: 4, STALE: 6}
    assert Counter(statuses("stock_day_facts").values()) == {UNDECIDABLE: 8, MISSING: 2}


def test_stale_ladder_days_are_stale_for_a_fact_visible_in_the_raw_catalog():
    # independent of the function under test: the recorded daily_kline version differs from the best catalogued one
    day = "2026-10-30"
    best_ladder = select_best([e for e in SNAP["datasets"]["ladder_daily"]["dates"][day] if isinstance(e, dict)])
    recorded = {ref["dataset"]: ref["version"] for ref in read_manifest(best_ladder)["input_refs"]
                if ref["trade_date"] == day}
    best_kline = select_best([e for e in SNAP["datasets"]["daily_kline"]["dates"][day] if isinstance(e, dict)])
    assert recorded["daily_kline"] != best_kline["version"] and best_kline["business_status"] == "FORMAL"
    assert STATUS(SNAP, "ladder_daily", day)[0] == STALE


def test_a_partition_without_recorded_inputs_is_undecidable_not_fresh():
    day = next(d for d, s in statuses("stock_day_facts").items() if s == UNDECIDABLE)
    best = select_best([e for e in SNAP["datasets"]["stock_day_facts"]["dates"][day] if isinstance(e, dict)])
    manifest = read_manifest(best)
    assert not manifest.get("input_refs") and not (manifest.get("inputs") or {}).get("daily_kline")
    assert STATUS(SNAP, "stock_day_facts", day) == (UNDECIDABLE, "no_recorded_inputs")


def test_a_day_without_any_partition_is_missing():
    assert STATUS(SNAP, "stock_day_facts", "2026-10-30") == (MISSING, "no_partition")


def test_the_plan_puts_the_newest_day_first_and_requirements_before_dependents():
    items, blocked = plan(SNAP, CONFIG, DAYS, STATUS)
    assert [item.day for item in items] == sorted((item.day for item in items), reverse=True)
    for day in DAYS:
        today_items = [item.dataset for item in items if item.day == day]
        if "daily_indicators" in today_items and "daily_report_features" in today_items:
            assert today_items.index("daily_indicators") < today_items.index("daily_report_features")
    assert len(items) == 3 + 3 + 6 + 10 and blocked == []     # every requirement exists for every day in this snapshot
    assert {item.status for item in items} == {STALE, UNDECIDABLE, MISSING}


def test_a_node_whose_requirement_has_no_partition_is_blocked_not_planned():
    snap = {"datasets": {name: {**body, "dates": {d: v for d, v in body["dates"].items() if not (
        name == "limit_up_pool" and d == "2026-10-29")}} for name, body in SNAP["datasets"].items()}}
    items, blocked = plan(snap, CONFIG, DAYS, STATUS)
    assert any(b["dataset"] == "ladder_daily" and b["day"] == "2026-10-29" and b["missing"] == "limit_up_pool"
               for b in blocked)
    assert not any(i.dataset == "ladder_daily" and i.day == "2026-10-29" for i in items)


def test_the_plan_key_changes_when_the_input_version_changes():
    items, _ = plan(SNAP, CONFIG, DAYS, STATUS)
    keys = {(i.dataset, i.day): i.key for i in items}
    snap = {"datasets": {**SNAP["datasets"], "daily_kline": {**SNAP["datasets"]["daily_kline"], "dates": {
        **SNAP["datasets"]["daily_kline"]["dates"], TODAY: [{**e, "version": "sha256-other"} if isinstance(e, dict) else e
                                                         for e in SNAP["datasets"]["daily_kline"]["dates"][TODAY]]}}}}
    changed, _ = plan(snap, CONFIG, DAYS, STATUS)
    assert {(i.dataset, i.day): i.key for i in changed}[("ladder_daily", TODAY)] != keys[("ladder_daily", TODAY)]


# ---- configuration: small hand-written configs are fine, a config is not data
def cfg(**chain):
    base = {"base": "daily_kline", "window_days": 5, "max_builds_per_run": 3, "external": ["daily_kline"],
            "nodes": {"a": ["daily_kline"], "b": ["a"]}}
    return {"chains": {"c": {**base, **chain}}}


@pytest.mark.parametrize("override,message", [
    ({"nodes": {"a": ["b"], "b": ["a"]}}, "dependency_cycle"),
    ({"nodes": {"a": ["ghost"]}}, "unknown_requirement:a->ghost"),
    ({"window_days": 0}, "positive"),
    ({"max_builds_per_run": True}, "positive"),
    ({"base": "other"}, "base_must_be_an_external_dataset"),
    ({"nodes": {}}, "no_nodes"),
])
def test_bad_chain_configuration_is_rejected_at_load(override, message):
    with pytest.raises(ChainConfigError, match=message):
        load_chain_config("c", cfg(**override))


def test_unknown_chain_and_nodes_without_a_registry_fetch_job_are_rejected():
    with pytest.raises(ChainConfigError, match="unknown_chain"):
        load_chain_config("nope", cfg())
    with pytest.raises(ChainConfigError, match="node_without_registry_fetch_job:b"):
        load_chain_config("c", cfg(), registry_ids={"a"})


def test_the_shipped_chain_is_valid_and_every_node_has_a_registry_fetch_job():
    import json
    registry = json.loads((Path(__file__).resolve().parents[1] / "src/engine_core/guardian/datasets.registry.json")
                          .read_text(encoding="utf-8"))["items"]
    with_job = {item["id"] for item in registry if item.get("fetch_job")}
    config = load_chain_config("daily_kline", registry_ids=with_job)
    assert config.order().index("daily_indicators") < config.order().index("daily_report_features")
