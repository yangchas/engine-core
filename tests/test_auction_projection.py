import json

import pytest

from engine_core import (
    REDIS_AUCTION_PROJECTION_SCOPE,
    read_redis_auction_projection,
)


class FakeRedis:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def hgetall(self, key):
        self.calls.append(key)
        return self.values.get(key, {})


def _payload(*, rows=None, tag="0920", ts=1789348803146):
    return {
        "meta": json.dumps({"tag": tag, "ts": ts, "n": len(rows or [])}),
        "summary": json.dumps(
            {
                "tag": tag,
                "ts": ts,
                "total_stocks": 2,
                "valid_stock_count": 2,
                "unavailable_stock_count": 0,
                "high_open_count": 1,
                "low_open_count": 1,
                "flat_open_count": 0,
                "total_auction_amount_yuan": 300,
                "total_limit_up_bid_amount_yuan": 100,
            }
        ),
        "top_amount": json.dumps(rows or []),
    }


def test_reads_only_documented_top_amount_projection_and_freezes_rows():
    redis = FakeRedis(
        {
            "market:auction:20260918:0920": _payload(
                rows=[
                    {
                        "symbol": "600519",
                        "price": 1277.27,
                        "change_pct": 0.0016,
                        "auction_amount_yuan": 200,
                        "bid_amount_yuan": 100,
                    },
                    {
                        "symbol": "000001",
                        "price": 10.0,
                        "change_pct": 0,
                        "auction_amount_yuan": 100,
                        "bid_amount_yuan": 0,
                    },
                ]
            )
        }
    )

    projection = read_redis_auction_projection(
        redis,
        trade_date="2026-09-18",
        observed_at_ms=1789714800000,
        tags=("0920",),
        symbols=("600519", "000001", "300750"),
    )[0]

    assert redis.calls == ["market:auction:20260918:0920"]
    assert projection.status == "READY"
    assert projection.scope == REDIS_AUCTION_PROJECTION_SCOPE
    assert projection.row_count == 2
    assert projection.missing_requested_symbols == ("300750",)
    assert projection.selected_rows[0]["symbol"] == "000001"
    assert projection.selected_rows[0]["auction_amount_yuan"] == 100
    assert projection.selected_rows[1]["change_ratio"] == pytest.approx(0.0016)
    serialized = json.dumps(projection.as_mapping(), sort_keys=True)
    assert '"rows":' in serialized
    assert "MappingProxyType" not in serialized
    with pytest.raises(TypeError):
        projection.rows[0]["auction_amount_yuan"] = 999  # type: ignore[index]


def test_missing_tag_is_not_replaced_by_another_tag_or_zero_filled():
    redis = FakeRedis(
        {
            "market:auction:20260918:0920": _payload(
                rows=[{"symbol": "600519", "auction_amount_yuan": 100}]
            )
        }
    )

    projections = read_redis_auction_projection(
        redis,
        trade_date="2026-09-18",
        observed_at_ms=1789714800000,
        tags=("0920", "0924"),
        symbols=("600519",),
    )

    assert [item.status for item in projections] == ["READY", "MISSING"]
    assert projections[1].rows == ()
    assert projections[1].missing_requested_symbols == ("600519",)
    assert redis.calls == [
        "market:auction:20260918:0920",
        "market:auction:20260918:0924",
    ]


def test_duplicate_projection_rows_fail_closed_as_invalid():
    redis = FakeRedis(
        {
            "market:auction:20260918:0925": _payload(
                tag="0925",
                rows=[
                    {"symbol": "600519", "auction_amount_yuan": 100},
                    {"symbol": "600519", "auction_amount_yuan": 101},
                ],
            )
        }
    )

    projection = read_redis_auction_projection(
        redis,
        trade_date="2026-09-18",
        observed_at_ms=1789714800000,
        tags=("0925",),
    )[0]

    assert projection.status == "INVALID"
    assert projection.rows == ()


def test_semantic_hash_excludes_query_scope_but_evidence_hash_records_it():
    redis = FakeRedis(
        {
            "market:auction:20260918:0920": _payload(
                rows=[{"symbol": "600519", "auction_amount_yuan": 100}]
            )
        }
    )
    all_rows, selected = (
        read_redis_auction_projection(
            redis,
            trade_date="2026-09-18",
            observed_at_ms=1789714800000,
            tags=("0920",),
        )[0],
        read_redis_auction_projection(
            redis,
            trade_date="2026-09-18",
            observed_at_ms=1789714800000,
            tags=("0920",),
            symbols=("600519",),
        )[0],
    )

    assert all_rows.content_hash == selected.content_hash
    assert all_rows.evidence_hash != selected.evidence_hash


def test_invalid_shape_is_reported_without_fallback():
    redis = FakeRedis(
        {
            "market:auction:20260918:0920": {
                "top_amount": json.dumps({"not": "a list"}),
            }
        }
    )

    projection = read_redis_auction_projection(
        redis,
        trade_date="2026-09-18",
        observed_at_ms=1789714800000,
        tags=("0920",),
    )[0]

    assert projection.status == "INVALID"
    assert projection.evidence_ref == "redis://market-auction/2026-09-18/0920"


def test_malformed_meta_is_invalid_instead_of_silently_repaired():
    redis = FakeRedis(
        {
            "market:auction:20260918:0920": {
                "meta": json.dumps(["not", "an", "object"]),
                "top_amount": json.dumps([]),
            }
        }
    )

    projection = read_redis_auction_projection(
        redis,
        trade_date="2026-09-18",
        observed_at_ms=1789714800000,
        tags=("0920",),
    )[0]

    assert projection.status == "INVALID"
