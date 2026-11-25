import math

from engine_core.plate_enrichment import (benjamini_hochberg, hypergeometric_upper_tail,
                                          infer_plate_enrichment, new_emerging_symbols)


def test_hypergeometric_tail_bh_and_plate_filters():
    # N=10, n=4, K=3, k=3: four of C(10,3) samples fall in the plate.
    assert hypergeometric_upper_tail(10, 4, 3, 3) == 1 / 30
    # k=K is a valid boundary, and k=0 is the whole probability space.
    assert hypergeometric_upper_tail(10, 4, 3, 0) == 1.0
    assert math.isclose(hypergeometric_upper_tail(10, 4, 3, 2), 1 / 3)
    assert benjamini_hochberg([0.01, 0.04, 0.03]) == [0.03, 0.04, 0.04]

    auction = {f"s{i}": {"pct_change": -1, "price_milli": 100_000} for i in range(100)}
    current = {f"s{i}": {"pct_change": 2 if i < 3 else -1,
                          "price_milli": 102_000 if i < 3 else 99_000} for i in range(100)}
    relations = {"good": {"s0", "s1", "s2", "s3", "s4"},
                 "generic": {"s0", "s1", "s2", "s5", "s6"},
                 "small": {"s0", "s1", "s7", "s8"},
                 "universe": {f"s{i}" for i in range(100)}}
    result = infer_plate_enrichment(auction, current, relations,
                                    plate_names={"good": "主题", "generic": "ST板块"},
                                    generic_names={"ST板块"}, member_snapshot_date="PIT 20261013")
    assert result["emerging_count"] == 3
    assert [row["code"] for row in result["plates"]] == ["good"]
    assert result["plates"][0]["N"] == 100 and result["plates"][0]["n"] == 5
    assert result["plates"][0]["K"] == 3 and result["plates"][0]["intersection"] == 3
    assert result["plates"][0]["member_snapshot_date"] == "PIT 20261013"


def test_emerging_thresholds_require_observed_auction_price():
    auction = {
        "below": {"pct_change": 1, "price_milli": 100_000},
        "edge": {"pct_change": 1, "price_milli": 100_000},
        "missing": {"pct_change": 1},
        "at_three": {"pct_change": 3, "price_milli": 100_000},
    }
    current = {
        "below": {"pct_change": 2.99, "price_milli": 102_990},
        "edge": {"pct_change": 3.0, "price_milli": 103_000},
        "missing": {"pct_change": 4, "price_milli": 104_000},
        "at_three": {"pct_change": 5, "price_milli": 105_000},
    }
    assert [row["symbol"] for row in new_emerging_symbols(auction, current)] == ["edge"]
    current["edge"]["pct_change"] = 2.99
    assert new_emerging_symbols(auction, current) == []
