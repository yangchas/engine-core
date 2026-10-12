from engine_core.theme_auction_delta import build_theme_auction_delta_facts
from engine_core.theme_auction_delta_rows import (
    build_theme_delta_rows,
    theme_weights_from_plate_mapping,
)

DATE = "2026-10-09"


def _r(symbol, tag, amt, bid, bp):
    return {
        "auction_tag": tag, "symbol": symbol, "trade_date": "20261009",
        "match_amt_yuan": amt, "rest_bid_amt_yuan": bid, "chg_bp": bp,
    }


def test_rows_use_both_tags_and_never_zero_fill_missing_operands():
    rows = build_theme_delta_rows(
        DATE,
        [
            _r("000001", "0924", 100, 10, 100), _r("000001", "0925", 300, 40, 250),
            _r("000002", "0924", None, 5, None), _r("000002", "0925", 50, 9, 70),
            _r("000003", "0925", 10, 1, 1),  # only one tag -> no row
        ],
    )
    by_symbol = {row["symbol"]: row for row in rows.rows}
    assert set(by_symbol) == {"000001", "000002"}
    first = by_symbol["000001"]
    assert first["amount_delta_yuan"] == 200 and first["bid_amount_delta_yuan"] == 30
    assert first["change_pct_delta"] == 1.5 and first["amount_ratio"] == 3.0
    second = by_symbol["000002"]
    assert second["amount_delta_yuan"] is None and second["amount_ratio"] is None
    assert second["change_pct_delta"] is None and second["bid_amount_delta_yuan"] == 4


def test_ratio_with_non_positive_previous_amount_is_undefined_not_zero():
    rows = build_theme_delta_rows(DATE, [_r("000001", "0924", 0, 0, 0), _r("000001", "0925", 5, 1, 0)])
    assert rows.rows[0]["amount_ratio"] is None
    assert rows.rows[0]["amount_delta_yuan"] == 5


def test_rows_feed_theme_facts_with_legacy_weights():
    rows = build_theme_delta_rows(
        DATE,
        [
            _r("000001", "0924", 100, 10, 0), _r("000001", "0925", 300, 40, 100),
            _r("000002", "0924", 200, 20, 0), _r("000002", "0925", 200, 20, 0),
        ],
    )
    weights = theme_weights_from_plate_mapping({"000001": "光模块", "000002": "光模块"}, ["000001", "000002"])
    facts = build_theme_auction_delta_facts(rows.rows, weights)
    assert len(facts) == 1 and facts[0].theme_id == "光模块"
    assert facts[0].symbol_count == 2 and facts[0].amount_delta_yuan == 200
