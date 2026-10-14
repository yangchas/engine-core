from engine_core.plate_registry import (
    build_registry,
    build_relation_arrays,
    plate_degree_by_symbol,
)


def test_classification_priority_and_all_matching_flags():
    registry = build_registry(
        {"801001": {"000001"}},
        {"801001": "一季报增长"},
        generic_names={"一季报增长"},
    )
    row = registry["plates"][0]
    assert row["class"] == "EVENT"
    assert row["flags"] == ["EVENT", "GENERIC_FACTOR", "NICHE"]


def test_family_union_find_threshold_and_largest_representative():
    members = {
        "801001": set("abcde"),
        "801002": set("abxyz"),  # Below threshold and outside the family.
        "801003": set("abcde"),
        "801004": set("abcde"),  # Equal-size family representative tie by code.
        "801005": set("abcdefgh"),
        "801006": set("abcdefghij"),  # Jaccard is exactly 8/10.
    }
    rows = {row["code"]: row for row in build_registry(members, {})["plates"]}
    assert rows["801001"]["family"] == "801001"
    assert rows["801003"]["family"] == rows["801004"]["family"] == "801001"
    assert rows["801002"]["family"] == "801002"
    assert rows["801005"]["family"] == rows["801006"]["family"] == "801006"


def test_parent_children_broad_classification():
    members = {"801000": set(f"{i:06d}" for i in range(150)), "801001": {"900001"}}
    row = {row["code"]: row for row in build_registry(
        members, {"801000": "芯片", "801001": "先进封装"}, {"801001": "801000"}
    )["plates"]}
    assert row["801000"]["children"] == ["801001"]
    assert row["801000"]["class"] == "BROAD"
    assert row["801001"]["parent"] == "801000"


def test_relation_arrays_and_degree_are_stably_sorted():
    relation = {"801002": {"000002", "000001"}, "801001": {"000002"}}
    arrays = build_relation_arrays(relation)
    assert arrays == {
        "symbols": ["000001", "000002"],
        "plates": ["801001", "801002"],
        "pairs": [(0, 1), (1, 0), (1, 1)],
    }
    assert plate_degree_by_symbol(relation) == {"000001": 1, "000002": 2}
