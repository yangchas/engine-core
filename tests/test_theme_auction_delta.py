from __future__ import annotations

import pytest

from engine_core import (
    FactStatus,
    build_legacy_theme_auction_delta_compat_facts,
    build_theme_auction_delta_facts,
)


def _rows():
    return [
        {
            "symbol": "600519",
            "tag": "0925",
            "previous_tag": "0924",
            "amount_yuan": 100.0,
            "amount_delta_yuan": 50.0,
            "bid_amount_delta_yuan": 10.0,
            "change_pct_delta": 1.0,
            "amount_ratio": 1.5,
            "evidence_ref": "td://delta/600519",
        },
        {
            "symbol": "000001",
            "tag": "0925",
            "previous_tag": "0924",
            "amount_yuan": 200.0,
            "amount_delta_yuan": -20.0,
            "bid_amount_delta_yuan": -5.0,
            "change_pct_delta": -0.5,
            "amount_ratio": 0.9,
            "evidence_ref": "td://delta/000001",
        },
    ]


def test_theme_delta_aggregates_explicit_weighted_facts_without_strategy_signal():
    facts = build_theme_auction_delta_facts(
        _rows(),
        {
            "600519": (("theme-a", 1.0), ("theme-b", 0.5)),
            "000001": (("theme-a", 1.0),),
        },
    )

    assert [fact.theme_id for fact in facts] == ["theme-a", "theme-b"]
    theme_a, theme_b = facts
    assert theme_a.status is FactStatus.READY
    assert theme_a.symbol_count == 2
    assert theme_a.amount_yuan == 300.0
    assert theme_a.amount_delta_yuan == 30.0
    assert theme_a.bid_amount_delta_yuan == 5.0
    assert theme_a.change_pct_delta_avg == pytest.approx(0.25)
    assert theme_a.amount_ratio_avg == pytest.approx(1.2)
    assert theme_a.positive_delta_count == 1
    assert theme_b.amount_yuan == 50.0
    assert theme_b.amount_delta_yuan == 25.0
    assert theme_b.status is FactStatus.READY


def test_theme_delta_missing_metric_is_partial_and_not_zero_filled():
    rows = _rows()
    rows[1] = dict(rows[1], amount_delta_yuan=None)
    fact = build_theme_auction_delta_facts(
        rows,
        {"600519": (("theme-a", 1.0),), "000001": (("theme-a", 1.0),)},
    )[0]

    assert fact.status is FactStatus.PARTIAL
    assert fact.amount_delta_yuan is None
    assert "amount_delta_yuan" in fact.missing_fields
    assert fact.field_counts["amount_delta_yuan"] == 1


def test_theme_delta_semantic_hash_excludes_evidence_refs():
    rows = _rows()
    left = build_theme_auction_delta_facts(
        rows,
        {"600519": (("theme-a", 1.0),), "000001": (("theme-a", 1.0),)},
    )[0]
    right_rows = [dict(row, evidence_ref="fixture://different/" + row["symbol"]) for row in rows]
    right = build_theme_auction_delta_facts(
        right_rows,
        {"600519": (("theme-a", 1.0),), "000001": (("theme-a", 1.0),)},
    )[0]

    assert left.content_hash == right.content_hash
    assert left.evidence_hash != right.evidence_hash


def test_theme_delta_rejects_duplicate_symbols_and_duplicate_weights():
    with pytest.raises(ValueError, match="duplicate normalized"):
        build_theme_auction_delta_facts(_rows() + [_rows()[0]], {"600519": (("theme-a", 1.0),)})
    with pytest.raises(ValueError, match="duplicate theme weight"):
        build_theme_auction_delta_facts(
            _rows(),
            {"600519": (("theme-a", 1.0), ("theme-a", 0.5))},
        )


def test_theme_delta_ignores_other_anchor_rows_without_inventing_facts():
    rows = _rows() + [dict(_rows()[0], tag="0924", previous_tag="0920", symbol="300001")]
    facts = build_theme_auction_delta_facts(
        rows,
        {"600519": (("theme-a", 1.0),), "000001": (("theme-a", 1.0),), "300001": (("theme-b", 1.0),)},
    )
    assert [fact.theme_id for fact in facts] == ["theme-a"]


def test_legacy_compat_fact_preserves_old_nonuniform_weighting_and_rounding():
    rows = [
        dict(_rows()[0], change_pct_delta=2.0, amount_ratio=2.0),
        dict(_rows()[1], change_pct_delta=1.0, amount_ratio=1.0),
    ]
    facts = build_legacy_theme_auction_delta_compat_facts(
        rows,
        {
            "600519": (("theme-a", 0.6),),
            "000001": (("theme-a", 1.0),),
        },
    )
    fact = facts[0]
    # Legacy: weighted change sum is divided by symbol count, while ratio
    # ignores theme weight and is averaged only over positive values.
    assert fact.change_pct_delta_avg == 1.1
    assert fact.amount_ratio_avg == 1.5
    assert fact.amount_delta_24_25 == 10.0
    assert fact.bid_amount_delta_24_25 == 1.0


def test_legacy_compat_fact_zero_fills_only_inside_explicit_compatibility_type():
    rows = [dict(_rows()[0], amount_ratio=None, change_pct_delta=None)]
    legacy = build_legacy_theme_auction_delta_compat_facts(
        rows,
        {"600519": (("theme-a", 1.0),)},
    )[0]
    conservative = build_theme_auction_delta_facts(
        rows,
        {"600519": (("theme-a", 1.0),)},
    )[0]
    assert legacy.amount_ratio_avg == 0.0
    assert legacy.change_pct_delta_avg == 0.0
    assert conservative.amount_ratio_avg is None
    assert conservative.change_pct_delta_avg is None
