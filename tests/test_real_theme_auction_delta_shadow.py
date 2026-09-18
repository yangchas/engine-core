from __future__ import annotations

import json

from examples.run_real_theme_auction_delta_shadow import (
    build_normalized_theme_delta_rows,
    run_real_theme_shadow,
)


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


class _FakeRedis:
    def __init__(self, hashes):
        self.hashes = hashes
        self.reads = []

    def hgetall(self, key):
        self.reads.append(key)
        return self.hashes.get(key, {})


def _projection(top_rows):
    return {
        "meta": json.dumps({"tag": "test"}),
        "summary": json.dumps({"tag": "test"}),
        "top_amount": json.dumps(top_rows),
    }


def test_real_theme_shadow_fails_closed_when_an_anchor_projection_is_missing():
    client = _FakeRedis(
        {"market:auction:20260918:0925": _projection([])}
    )
    result = run_real_theme_shadow(client=client, trade_date="2026-09-18")
    assert result["status"] == "UNAVAILABLE"
    assert result["facts"] == ()
    assert "market:stock_plate" not in client.reads


def test_real_theme_shadow_builds_observed_fact_from_both_projection_rows():
    client = _FakeRedis(
        {
            "market:auction:20260918:0924": _projection(
                [{"symbol": "000001", "auction_amount_yuan": 100, "bid_amount_yuan": 50, "change_pct": 0.01}]
            ),
            "market:auction:20260918:0925": _projection(
                [{"symbol": "000001", "auction_amount_yuan": 150, "bid_amount_yuan": 70, "change_pct": 0.02}]
            ),
            "market:stock_plate": {"000001": "银行"},
            "config:plate_mapping:s2p": {"000001": '["银行"]'},
        }
    )
    result = run_real_theme_shadow(client=client, trade_date="2026-09-18")
    assert result["status"] == "OBSERVED"
    assert result["row_count"] == 1
    assert result["mapping_count"] == 1
    assert result["facts"][0]["theme_id"] == "银行"
    assert result["facts"][0]["amount_delta_yuan"] == 50.0
    assert result["facts"][0]["bid_amount_delta_yuan"] == 20.0
    assert result["facts"][0]["change_pct_delta_avg"] == 1.0
