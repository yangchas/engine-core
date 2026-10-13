import pytest

from engine_core import derive_td_auction_summary


def _r(symbol, tag="0925", px=10000, bp=0, amt=0, bid=0, ask=0, ls=0):
    return {"symbol": symbol, "auction_tag": tag, "px_milli": px, "chg_bp": bp,
            "match_amt_yuan": amt, "rest_bid_amt_yuan": bid, "rest_ask_amt_yuan": ask,
            "limit_state": ls}


def test_candidate_valid_breadth_amount_limit_and_seal():
    rows = [
        _r("000001", bp=120, amt=1000, bid=5),
        _r("000002", bp=-30, amt=2000, ask=7),
        _r("000003", bp=0, amt=300),
        _r("000004", px=0, bp=None, bid=9),             # candidate, no match price -> unavailable
        _r("000005", bp=1000, amt=500, bid=800, ls=1),  # limit up with seal
        _r("000006", bp=-1000, amt=400, ask=20, ls=-1),
        _r("000007", px=0, bp=None),                    # not a candidate (all zero)
        _r("000008", tag="0924", amt=999),              # other tag ignored
    ]
    out = derive_td_auction_summary(rows, tag="0925")
    assert out["stock_count"] == 6
    assert (out["valid_stock_count"], out["unavailable_stock_count"]) == (5, 1)
    assert (out["positive_count"], out["negative_count"], out["flat_count"]) == (2, 2, 1)
    assert out["auction_amount_yuan"] == 1000 + 2000 + 300 + 0 + 500 + 400
    assert (out["limit_up_count"], out["limit_down_count"]) == (1, 1)
    assert out["limit_up_seal_amount_yuan"] == 800
    assert out["unknown_amount_rows"] == 0


def test_missing_amount_is_not_summed_as_zero_and_duplicates_are_counted():
    rows = [_r("000001", bp=5, amt=None, bid=3), _r("000002", bp=1, amt=100), _r("000002", bp=9, amt=7)]
    out = derive_td_auction_summary(rows, tag="0925")
    assert out["unknown_amount_rows"] == 1
    assert out["auction_amount_yuan"] == 100
    assert out["duplicate_symbol_rows"] == 1


def test_unknown_tag_rejected():
    with pytest.raises(ValueError):
        derive_td_auction_summary([], tag="0930")
