from engine_core.plate_membership import (
    PlateHierarchy,
    apply_hierarchy,
    build_memberships,
    default_alias_hierarchy,
    members_of,
)


def test_user_priority_merges_sources_uses_best_tier_and_marks_hot():
    result = build_memberships(
        symbols=["000001"],
        pool_plate_by_symbol={"000001": "芯片/人工智能"},
        reason_candidates_by_symbol={"000001": ["人工智能", "机器人"]},
        static_themes_by_symbol={"000001": ["通信", "算力"]},
        hot_plate_names=["算力", "热板独有"],
    )
    rows = result.by_symbol["000001"]
    by_name = {row.plate: row for row in rows}
    assert by_name["芯片"].tier == 1
    assert by_name["人工智能"].tier == 1
    assert by_name["人工智能"].source == "pool,reason"
    assert by_name["算力"].source == "static,hot_plate"
    assert "热板独有" not in by_name


def test_empty_and_other_pool_values_are_ignored():
    result = build_memberships(
        symbols=["a", "b"],
        pool_plate_by_symbol={"a": "其他", "b": ""},
        reason_candidates_by_symbol={},
        static_themes_by_symbol={},
    )
    assert result.by_symbol == {"a": (), "b": ()}


def test_memberships_are_not_truncated():
    result = build_memberships(
        symbols=["s"], pool_plate_by_symbol={}, reason_candidates_by_symbol={},
        static_themes_by_symbol={"s": [f"主题{i}" for i in range(7)]},
    )
    assert len(result.by_symbol["s"]) == 7


def test_generic_members_rank_after_specific_members():
    result = build_memberships(
        symbols=["s"], pool_plate_by_symbol={"s": "昨日涨停"},
        reason_candidates_by_symbol={"s": ["芯片"]}, static_themes_by_symbol={},
    )
    rows = result.by_symbol["s"]
    assert rows[0].plate == "芯片"
    assert rows[0].rank_in_stock == 1


def test_user_priority_orders_by_kind_before_source_tier_and_keeps_events():
    result = build_memberships(
        symbols=["s"], pool_plate_by_symbol={"s": "昨日涨停"},
        reason_candidates_by_symbol={"s": ["中报增长"]},
        static_themes_by_symbol={"s": ["基础建设"]},
    )
    rows = result.by_symbol["s"]
    assert [(row.plate, row.kind, row.rank_in_stock) for row in rows] == [
        ("基础建设", "THEME", 1),
        ("昨日涨停", "GENERIC", 2),
        ("中报增长", "EVENT", 3),
    ]


def test_user_priority_allows_event_primary_when_no_theme_or_generic_exists():
    result = build_memberships(
        symbols=["s"], pool_plate_by_symbol={},
        reason_candidates_by_symbol={"s": ["中报增长", "合同"]},
        static_themes_by_symbol={},
    )
    rows = result.by_symbol["s"]
    assert rows[0].plate == "中报增长"
    assert rows[0].kind == "EVENT"
    assert rows[0].rank_in_stock == 1


def test_legacy_weights_match_old_two_theme_weighting():
    result = build_memberships(
        symbols=["s"], pool_plate_by_symbol={},
        reason_candidates_by_symbol={"s": ["国企", "芯片", "通信"]},
        static_themes_by_symbol={}, policy="LEGACY_V1",
    )
    assert result.legacy_weights["s"] == (("芯片", 1.0), ("国企", 0.108))


def test_hierarchy_ancestors_and_cycles_are_reported_without_raising():
    source = build_memberships(
        symbols=["s"], pool_plate_by_symbol={}, reason_candidates_by_symbol={},
        static_themes_by_symbol={"s": ["先进封装"]},
    )
    hierarchy = PlateHierarchy(
        parent_by_child={"先进封装": "芯片", "芯片": "半导体", "半导体": "芯片"},
        source_by_child={"先进封装": "supplied", "芯片": "supplied", "半导体": "supplied"},
    )
    result = apply_hierarchy(source, hierarchy)
    row = result.by_symbol["s"][0]
    assert row.ancestors == ("芯片", "半导体")
    assert "cycle" in row.flags


def test_members_of_returns_deduplicated_descendant_union():
    result = build_memberships(
        symbols=["a", "b"], pool_plate_by_symbol={}, reason_candidates_by_symbol={},
        static_themes_by_symbol={"a": ["先进封装", "芯片"], "b": ["存储"]},
    )
    hierarchy = PlateHierarchy(
        parent_by_child={"先进封装": "芯片", "存储": "芯片"},
        source_by_child={"先进封装": "supplied", "存储": "supplied"},
    )
    assert members_of("芯片", result, hierarchy) == {"a", "b"}


def test_default_alias_hierarchy_has_semiconductor_aliases():
    hierarchy = default_alias_hierarchy()
    assert hierarchy.parent_by_child["先进封装"] == "芯片"
    assert hierarchy.source_by_child["先进封装"] == "static_alias"
