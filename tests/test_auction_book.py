from datetime import datetime

from engine_core.auction_book import AuctionBookTick, derive_auction_book


def ms(clock):
    return int(datetime.fromisoformat(f"2026-10-12T{clock}+08:00").timestamp() * 1000)


def test_auction_book_uses_match_and_unmatched_levels():
    state = derive_auction_book(AuctionBookTick(
        ts_ms=ms("09:20:00"), px_milli=760000, bp1=774100, ap1=774100,
        bv1=238, av1=238, bv2=0, av2=50, amt_yuan=0, vol_units=0,
    ))
    assert state.effective_price_milli == 774100
    assert state.matched_volume == 238
    assert state.match_amt_yuan == 18423580
    assert state.rest_bid_amt_yuan == 0
    assert state.rest_ask_amt_yuan == 3870500
    assert state.phase == "CALL"


def test_auction_book_has_no_price_without_matching_top_levels_and_uses_post_call_amount():
    no_match = derive_auction_book(AuctionBookTick(
        ts_ms=ms("09:24:57"), px_milli=760000, bp1=760000, ap1=760500,
        bv1=10, av1=20, bv2=3, av2=4, amt_yuan=999, vol_units=2,
    ))
    assert no_match.effective_price_milli == 0
    assert no_match.match_amt_yuan == no_match.rest_bid_amt_yuan == no_match.rest_ask_amt_yuan == 0
    assert "NO_MATCH_PRICE" in no_match.flags

    result = derive_auction_book(AuctionBookTick(
        ts_ms=ms("09:25:00"), px_milli=760000, bp1=759000, ap1=761000,
        bv1=10, av1=20, bv2=3, av2=4, amt_yuan=121639504, vol_units=99,
    ))
    assert result.effective_price_milli == 760000
    assert result.match_amt_yuan == 121639504
    assert result.phase == "RESULT"


def test_auction_book_post_call_falls_back_to_volume_and_rest_formula():
    state = derive_auction_book(AuctionBookTick(
        ts_ms=ms("09:25:01"), px_milli=760000, bp1=750000, ap1=770000,
        bv1=10, av1=20, bv2=3, av2=4, amt_yuan=0, vol_units=2,
    ))
    assert state.match_amt_yuan == 152000
    assert state.rest_bid_amt_yuan == 225000
    assert state.rest_ask_amt_yuan == 308000
