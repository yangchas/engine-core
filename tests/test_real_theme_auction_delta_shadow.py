from __future__ import annotations

from examples.run_real_theme_auction_delta_shadow import build_normalized_theme_delta_rows


def test_real_theme_shadow_row_builder_preserves_missing_and_source_scope():
    rows = build_normalized_theme_delta_rows(
        (
            {
                "symbol": "000001",
                "auction_amount_yuan": 100,
                "bid_amount_yuan": 50,
                "change_ratio": 0.01,
            },
        ),
        (
            {
                "symbol": "000001",
                "auction_amount_yuan": 150,
                "bid_amount_yuan": None,
                "change_ratio": 0.02,
            },
            {"symbol": "000002", "auction_amount_yuan": 1},
        ),
        evidence_ref_prefix="redis://test/0924-0925",
    )
    assert rows == (
        {
            "symbol": "000001",
            "tag": "0925",
            "previous_tag": "0924",
            "amount_yuan": 150,
            "amount_delta_yuan": 50.0,
            "bid_amount_delta_yuan": None,
            "change_pct_delta": 1.0,
            "amount_ratio": 1.5,
            "evidence_ref": "redis://test/0924-0925/000001",
        },
    )
