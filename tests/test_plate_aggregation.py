from engine_core.plate_aggregation import (
    StockFact,
    aggregate_plates,
    market_summary,
)
from engine_core.plate_membership import (
    PlateHierarchy,
    build_memberships,
)


def _inputs():
    memberships = build_memberships(
        symbols=["a", "b", "c"],
        pool_plate_by_symbol={"a": ["芯片", "半导体"], "c": ["昨日涨停"]},
        reason_candidates_by_symbol={"b": ["芯片"]},
        static_themes_by_symbol={"a": ["先进封装"], "b": ["通信"], "c": ["AI"]},
    )
    hierarchy = PlateHierarchy(
        parent_by_child={"先进封装": "芯片", "芯片": "半导体", "通信": "泛科技"},
        source_by_child={"先进封装": "supplied", "芯片": "supplied", "通信": "supplied"},
    )
    facts = {
        "a": StockFact("a", 10, 11, 100, 1, 3, True, 80),
        "b": StockFact("b", 20, 18, 300, -1, 2, False, None),
        "c": StockFact("c", None, None, None, None, None, True, 4),
    }
    return memberships, hierarchy, facts


def test_ancestor_uses_deduplicated_union_and_not_child_sums():
    memberships, hierarchy, facts = _inputs()
    result = aggregate_plates(memberships, hierarchy, facts)
    stats = {row.plate: row for row in result.stats}
    # a is explicitly in both parent and child, but is counted once.
    assert stats["半导体"].member_count == 2
    assert stats["半导体"].amount_sum == 400
    assert stats["半导体"].amount_sum != stats["芯片"].amount_sum + 100


def test_missing_values_are_none_and_excluded_from_coverage():
    memberships, hierarchy, facts = _inputs()
    result = aggregate_plates(memberships, hierarchy, facts)
    stat = {row.plate: row for row in result.stats}["昨日涨停"]
    assert (stat.member_count, stat.covered_count) == (1, 0)
    assert stat.amount_sum is None
    assert stat.amount_share_of_market is None
    assert stat.pct_equal_weight is None
    assert stat.pct_amount_weighted is None
    assert stat.excess_vs_market is None
    assert stat.limit_state_unknown_count == 1


def test_member_without_a_fact_has_unknown_limit_state_but_stays_uncovered():
    memberships, hierarchy, _ = _inputs()
    stats = {row.plate: row for row in aggregate_plates(memberships, hierarchy, {}).stats}
    stat = stats["昨日涨停"]
    assert stat.member_count == 1
    assert stat.covered_count == 0
    assert stat.limit_state_unknown_count == 1


def test_market_and_plate_rates_and_amount_share():
    memberships, hierarchy, facts = _inputs()
    result = aggregate_plates(memberships, hierarchy, facts)
    stat = {row.plate: row for row in result.stats}["先进封装"]
    assert result.market.covered_count == 2
    assert result.market.amount_sum == 400
    assert abs(result.market.pct_equal_weight) < 1e-12
    assert abs(stat.pct_equal_weight - 0.1) < 1e-12
    assert abs(stat.pct_amount_weighted - 0.1) < 1e-12
    assert stat.amount_share_of_market == 0.25
    assert abs(stat.excess_vs_market - 0.1) < 1e-12


def test_seal_counts_pool_reason_and_tiers():
    memberships, hierarchy, facts = _inputs()
    stats = {row.plate: row for row in aggregate_plates(memberships, hierarchy, facts).stats}
    chip = stats["芯片"]
    assert (chip.up_count, chip.down_count, chip.flat_count) == (1, 1, 0)
    assert (chip.limit_up_count, chip.limit_down_count, chip.limit_state_unknown_count) == (1, 1, 0)
    assert chip.max_lb_days == 3
    assert chip.yest_pool_member_count == 1
    assert chip.reason_hit_count == 1
    assert dict(chip.tier_counts) == {1: 1, 2: 1, 3: 0, 4: 0}


def test_amount_delta_only_uses_members_with_both_values():
    memberships, hierarchy, facts = _inputs()
    stat = {row.plate: row for row in aggregate_plates(memberships, hierarchy, facts).stats}["芯片"]
    assert stat.amount_delta_sum == 20


def test_amount_coverage_is_independent_from_price_coverage():
    memberships, hierarchy, facts = _inputs()
    facts = {**facts, "a": StockFact("a", 10, None, 100, None, None),
             "b": StockFact("b", None, 20, 200, None, None)}
    result = aggregate_plates(memberships, hierarchy, facts)
    assert result.market.covered_count == 0
    assert result.market.amount_sum == 300
    assert result.market.amount_covered_count == 2
    stat = next(row for row in result.stats if row.plate == "芯片")
    assert stat.covered_count == 0
    assert stat.amount_covered_count == 2
    assert stat.amount_sum == 300


def test_generic_plates_are_sorted_after_specific_but_still_aggregated():
    memberships, hierarchy, facts = _inputs()
    result = aggregate_plates(memberships, hierarchy, facts, include_ancestors=False)
    rows = list(result.stats)
    assert all(not row.generic for row in rows[: next((i for i, row in enumerate(rows) if row.generic), len(rows))])
    generic = {row.plate: row for row in rows if row.generic}
    assert "昨日涨停" in generic
    assert generic["昨日涨停"].member_count == 1


def test_kind_is_exposed_and_rows_sort_theme_event_generic_noise():
    memberships = build_memberships(
        symbols=["a", "b", "c", "d"],
        pool_plate_by_symbol={"a": ["机器人"], "b": ["业绩增长"], "c": ["人工智能"], "d": ["320万元"]},
        reason_candidates_by_symbol={}, static_themes_by_symbol={},
    )
    rows = aggregate_plates(memberships, PlateHierarchy({}, {}), {}, include_ancestors=False).stats
    assert [(row.plate, row.kind) for row in rows] == [
        ("人工智能", "THEME"), ("业绩增长", "EVENT"), ("机器人", "GENERIC"), ("320万元", "NOISE")
    ]


def test_empty_input_and_market_summary():
    empty = market_summary({})
    assert empty.covered_count == 0
    assert empty.amount_sum is None
    assert empty.pct_equal_weight is None
    memberships = build_memberships(
        symbols=[], pool_plate_by_symbol={}, reason_candidates_by_symbol={}, static_themes_by_symbol={}
    )
    hierarchy = PlateHierarchy({}, {})
    result = aggregate_plates(memberships, hierarchy, {})
    assert result.stats == ()
    assert len(result.content_hash) == 64
    assert result.rule_version == memberships.rule_version
