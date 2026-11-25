from engine_core.auction_limit_facts import (
    aggregate_plate_auction_limit_facts,
    derive_stock_auction_limit_fact,
)
from engine_core.plate_membership import PlateHierarchy, PlateMembership, StockPlateMemberships


def _snap(price, match, bid, ask, state=0):
    return {"px_milli": price, "match_amt_yuan": match,
            "rest_bid_amt_yuan": bid, "rest_ask_amt_yuan": ask,
            "limit_state": state}


def test_lock_ratio_uses_0920_match_amount_and_exact_limit_rounding():
    fact = derive_stock_auction_limit_fact(
        symbol="300308", trade_date="2026-10-14", pre_close=10.03,
        prev_day_amount=100_000_000,
        snapshots={"0920": _snap(12040, 18_423_580, 0, 3_870_500),
                   "0924": _snap(12040, 80, 0, 0),
                   "0925": _snap(12040, 100, 50, 0, 1)},
    )
    assert fact.lock_ratio_0920 == 184_235.8
    assert fact.rest_ask_0920 == 3_870_500
    assert fact.at_limit_up_0925 is True  # 10.03 * 1.20 rounds half-up to 12.04
    assert fact.seal_amt_0925 == 50


def test_limit_down_and_seal_amount_follow_market_direction():
    fact = derive_stock_auction_limit_fact(
        symbol="600001", trade_date="2026-10-14", pre_close=10.05,
        prev_day_amount=1_000, snapshots={
            "0920": _snap(9050, 1, 0, 7), "0924": _snap(9050, 2, 0, 8),
            "0925": _snap(9050, 3, 4, 90, -1),
        })
    assert fact.at_limit_down_0925 is True  # 10.05 * 0.9 => 9.05
    assert fact.seal_amt_0920 == 7
    assert fact.seal_amt_0924 == 8
    assert fact.seal_amt_0925 == 90


def test_missing_prior_amount_is_none_and_flagged_without_zero_fill():
    fact = derive_stock_auction_limit_fact(
        symbol="600001", trade_date="2026-10-14", pre_close=None,
        prev_day_amount=None,
        snapshots={"0925": _snap(11000, 20, 30, 0, 1)},
    )
    assert fact.at_limit_up_0925 is None
    assert fact.seal_strength_prev_amt is None
    assert fact.seal_trajectory == (None, None, None)
    assert {"NO_PRE_CLOSE", "NO_PREV_AMOUNT", "NO_0920"} <= set(fact.flags)


def test_plate_members_are_union_deduplicated_and_missing_stocks_are_not_zero_filled():
    memberships = StockPlateMemberships(
        by_symbol={
            "600001": (PlateMembership("600001", "芯片", 3, 1, "static", False, False),),
            "600002": (PlateMembership("600002", "芯片", 3, 1, "static", False, False),
                       PlateMembership("600002", "半导体", 3, 2, "static", False, False)),
        }, policy="test", rule_version="test", content_hash="test", legacy_weights={},
    )
    facts = {
        "600001": derive_stock_auction_limit_fact(
            symbol="600001", trade_date="2026-10-14", pre_close=10,
            prev_day_amount=1000, snapshots={"0925": _snap(11000, 100, 40, 0, 1)}),
    }
    plates = aggregate_plate_auction_limit_facts(
        facts, memberships, PlateHierarchy({"半导体": "芯片"}, {}),
    )
    chip = next(row for row in plates if row.plate == "芯片")
    assert chip.member_count == 2
    assert chip.limit_up_count == 1
    assert chip.seal_mass_up == 40
    assert chip.seal_to_match_ratio == 0.4
    assert chip.flags
