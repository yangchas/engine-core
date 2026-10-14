from copy import deepcopy

from engine_core.plate_assignment import (
    FeatureEvidence,
    build_feature_sets,
    summarize_feature_sets,
)


def _registry(rows):
    return {"plates": rows}


def _row(code, plate_class="THEME", *, size=100, parent=None, family=None, name=None):
    return {
        "code": code,
        "name": name or code,
        "size": size,
        "class": plate_class,
        "parent": parent,
        "children": [],
        "family": family or code,
        "flags": [],
    }


def test_pool_reason_then_static_precedence_and_primary_source():
    registry = _registry([
        _row("801001"),
        _row("801002"),
        _row("801003"),
    ])
    members = {code: {"000001"} for code in ("801001", "801002", "801003")}
    result = build_feature_sets(
        registry,
        members,
        FeatureEvidence(
            yesterday_pool_plates={"000001": "801001"},
            recent_reason_plates={"000001": [("801002", 0)]},
        ),
    )["000001"]
    assert [plate["code"] for plate in result.plates] == ["801001", "801002", "801003"]
    assert result.primary == "801001"
    assert result.primary_source == "POOL"
    assert result.plates[0]["score"] > result.plates[1]["score"] > result.plates[2]["score"]


def test_child_plate_covers_ancestor_for_same_stock():
    registry = _registry([
        _row("801000", "BROAD", size=500, name="芯片"),
        _row("801001", "THEME", size=100, parent="801000", name="先进封装"),
    ])
    result = build_feature_sets(registry, {"801000": {"000001"}, "801001": {"000001"}}, None)
    assert [plate["code"] for plate in result["000001"].plates] == ["801001"]


def test_family_keeps_highest_scoring_plate_and_code_breaks_ties():
    registry = _registry([
        _row("801002", family="fam"),
        _row("801001", family="fam"),
        _row("801003", family="fam2"),
    ])
    result = build_feature_sets(
        registry,
        {code: {"000001"} for code in ("801001", "801002", "801003")},
        FeatureEvidence(yesterday_pool_plates={"000001": "801002"}),
    )["000001"]
    assert [plate["code"] for plate in result.plates] == ["801002", "801003"]


def test_generic_and_event_are_excluded_but_recorded():
    registry = _registry([
        _row("801001", "GENERIC_FACTOR"),
        _row("801002", "EVENT"),
        _row("801003", "THEME"),
    ])
    result = build_feature_sets(
        registry,
        {code: {"000001"} for code in ("801001", "801002", "801003")},
        None,
    )["000001"]
    assert [plate["code"] for plate in result.plates] == ["801003"]
    assert result.generic_plates == ["801001", "801002"]
    assert result.degree_total == 3


def test_missing_evidence_and_no_candidate_flags_and_summary():
    result = build_feature_sets(
        _registry([_row("801001", "GENERIC_FACTOR")]),
        {"801001": {"000001"}},
        None,
    )
    row = result["000001"]
    assert row.plates == []
    assert row.primary is None
    assert "EVIDENCE_MISSING" in row.flags
    assert "NO_CANDIDATE" in row.flags
    assert summarize_feature_sets(result) == {
        "symbols": 1,
        "with_primary": 0,
        "primary_source_counts": {},
        "avg_plates": 0.0,
        "no_candidate": 1,
        "evidence_missing": 1,
    }


def test_output_is_stable_when_input_mapping_order_changes():
    rows = [_row("801002", size=30), _row("801001", size=30), _row("801003", size=60)]
    registry = _registry(rows)
    members = {"801002": {"000002", "000001"}, "801001": {"000001"}, "801003": {"000002"}}
    evidence = FeatureEvidence(hot_prior_rank={"801003": 1})
    first = build_feature_sets(registry, members, evidence)
    second = build_feature_sets(
        {"plates": list(reversed(deepcopy(rows)))},
        dict(reversed(list(members.items()))),
        evidence,
    )
    assert first == second
    assert [plate["code"] for plate in first["000001"].plates] == ["801001", "801002"]
