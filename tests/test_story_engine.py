import json
from datetime import date, timedelta

from engine_core.anomaly_detector import detect_anomalies
from engine_core.story_composer import compose_story
from engine_core.story_features import build_story_features
from engine_core.story_memory import append_memory, attach_next_day, make_daily_memory
from engine_core.tradability_funnel import filter_candidates


def test_missing_zero_and_small_sample_anomaly_policy():
    current = [{"metric_id": "x", "stage": "auction", "value": 0, "unit": "count"},
               {"metric_id": "missing", "stage": "auction", "value": None, "missing_reason": "not captured"}]
    history = [{"metric_id": "x", "stage": "auction", "value": n} for n in range(1, 6)]
    found = detect_anomalies(current, history)
    assert found[0]["kind"] == "OBSERVED_RANK"
    assert found[0]["n"] == 5
    assert found[1]["kind"] == "UNKNOWN"
    assert found[1]["value"] is None


def test_stage_projection_and_funnel_ignore_future_results():
    facts = {"trade_date": "2026-10-15", "auction_median_pct": -1.0,
             "close_median_pct": -3.0, "stage": "auction"}
    early = build_story_features(facts, stage="auction", cutoff="09:25",
                                 observed_at="2026-10-15T09:25:00+08:00", provenance={})
    poisoned = dict(facts, close_median_pct=99.0, next_day_winner="Winners")
    early_poisoned = build_story_features(poisoned, stage="auction", cutoff="09:25",
                                          observed_at="2026-10-15T09:25:00+08:00", provenance={})
    assert early == early_poisoned
    candidates = [{"symbol": "000001", "name": "甲", "bucket": "watch", "auction_pct": 1,
                   "close_pct": 10}, {"symbol": "000002", "name": "乙", "bucket": "wait",
                   "auction_pct": 2, "close_pct": -10}]
    one = filter_candidates(candidates, early, asof="09:25")
    two = filter_candidates([dict(candidates[0], close_pct=-99), dict(candidates[1], close_pct=99)],
                            early_poisoned, asof="09:25")
    assert one == two
    open_facts = {"trade_date": "2026-10-15", "p0945_median": {"value": -1.2, "unit": "pp",
                                                                    "event_cutoff": "09:44:58"},
                  "p0945_late_bucket": {"value": 9, "event_cutoff": "09:45:01"},
                  "close_median_pct": -3.0}
    open_a = build_story_features(open_facts, stage="intraday", cutoff="09:45:00",
                                  observed_at="2026-10-15T09:45:00+08:00", provenance={})
    open_b = build_story_features(dict(open_facts, close_median_pct=100.0, next_day_winner="胜者"),
                                  stage="intraday", cutoff="09:45:00",
                                  observed_at="2026-10-15T09:45:00+08:00", provenance={})
    assert open_a == open_b
    assert "p0945_median" in open_a["metrics"] and "p0945_late_bucket" not in open_a["metrics"]
    assert filter_candidates(candidates, open_a, asof="09:45") == filter_candidates(candidates, open_b,
                                                                                   asof="09:45")


def test_story_caps_unobserved_act_and_json_safety():
    bundle = build_story_features({"trade_date": "2026-10-15", "auction_up_share": 0.2},
                                  stage="auction", cutoff="09:25", observed_at=None,
                                  provenance={"source": "fixture"})
    story = compose_story(bundle, [], {"shown": {"watch": [], "wait": [], "exclude": []}},
                          previous_acts=[], templates=[])
    assert len(story["top_events"]) <= 3
    assert len(story["acts"]) <= 4
    assert story["acts"][-1]["status"] == "UNOBSERVED"
    json.dumps(story, allow_nan=False)


def test_memory_next_day_is_separate_and_append_is_opt_in(tmp_path):
    bundle = build_story_features({"trade_date": "2026-10-15"}, stage="close", cutoff="15:00",
                                  observed_at="2026-10-15T15:00:00+08:00", provenance={})
    story = compose_story(bundle, [], {"shown": {}}, previous_acts=[], templates=[])
    memory = make_daily_memory(story, bundle, input_refs=[], rule_version="test")
    outcome = attach_next_day("2026-10-15", [{"name": "甲", "result": "observed"}],
                              observed_at="2026-10-16T15:00:00+08:00")
    assert "next_day_ref" in memory and memory["next_day_ref"] is None
    assert outcome["memory_ref"] == "2026-10-15"
    assert memory["acts"] == story["acts"]
    assert not list(tmp_path.iterdir())
    path = append_memory(memory, directory=tmp_path)
    assert path.endswith("2026-10-15.json")
    assert json.loads(open(path, encoding="utf-8").read()) == memory
    for offset in range(129):
        record = dict(memory, trade_date=(date(2026, 1, 1) + timedelta(days=offset)).isoformat())
        append_memory(record, directory=tmp_path)
    assert len(list(tmp_path.glob("????-??-??.json"))) == 128


def test_anomaly_sample_cutoffs_mad_zero_equal_rank_and_denominator_policy():
    def run(n, value, denominator=None):
        row = {"metric_id": "breadth_share", "stage": "close", "value": value}
        if denominator is not None:
            row["denominator"] = denominator
        history = [{"metric_id": "breadth_share", "stage": "close", "value": 1.0}
                   for _ in range(n)]
        return detect_anomalies([row], history)[0]

    assert run(4, 1.0)["kind"] == "CONTEXT"
    assert run(5, 1.0)["kind"] == "OBSERVED_RANK"
    assert run(19, 1.0)["kind"] == "OBSERVED_RANK"
    tied = run(20, 1.0)
    assert tied["p"] == 0.5 and tied["robust_z"] is None
    assert run(20, 2.0)["kind"] == "TAIL"
    assert run(20, 2.0, denominator=4)["quality"] == ["SMALL_DENOMINATOR_RANK_ONLY"]
    assert run(20, 2.0, denominator=5)["kind"] == "TAIL"


def test_cutoff_excludes_late_final_and_funnel_deduplicates_without_outcome_ordering():
    facts = {"trade_date": "2026-10-15", "metrics": {
        "virtual_match_amount_yuan": {"value": 123, "unit": "yuan", "event_cutoff": "09:24:00"},
        "final_amount_yuan": {"value": 456, "unit": "yuan", "event_cutoff": "09:26:00"},
        "late_tick": {"value": 1, "event_cutoff": "not-a-time"}}}
    early = build_story_features(facts, stage="auction", cutoff="09:25:00",
                                 observed_at="2026-10-15T09:25:00+08:00", provenance={})
    assert "virtual_match_amount_yuan" in early["metrics"]
    assert "final_amount_yuan" not in early["metrics"]
    assert "late_tick" not in early["metrics"]
    rows = [{"symbol": "000001", "name": "甲", "bucket": "watch", "theme": "x",
             "price_status": "INDICATIVE", "condition": "观察下一截点"},
            {"symbol": "000001", "name": "甲", "bucket": "exclude", "theme": "y",
             "price_status": "FINAL", "condition": "等待重新核验", "risk": "晚到终价"}]
    funnel = filter_candidates(rows, early, asof="09:25:00")
    assert funnel["counts"] == {"watch": 0, "wait": 0, "exclude": 1}
    assert [x["name"] for x in funnel["shown"]["exclude"]] == ["甲"]
