#include "auction_calculator.h"

#include <algorithm>
#include <ctime>

namespace t1_v2 {
namespace {

constexpr int64_t kBoardLotShares = 100;

int64_t order_book_amount_yuan(int price_milli, int64_t lot_volume) {
    if (price_milli <= 0 || lot_volume <= 0) {
        return 0;
    }
    return (static_cast<int64_t>(price_milli) * lot_volume * kBoardLotShares) / 1000;
}

}  // namespace

AuctionSnapshotPriceFact auction_snapshot_price_fact(
    const QuoteState& state,
    const std::string& tag
) {
    int price_milli = 0;
    const char* source = "unavailable";
    if (tag == "0920") {
        price_milli = state.auction.a20_px_milli;
        source = "anchor_0920";
    } else if (tag == "0924") {
        price_milli = state.auction.a24_px_milli;
        source = "anchor_0924";
    } else if (tag == "0925") {
        price_milli = state.auction.a25_px_milli;
        source = "anchor_0925";
    } else if (tag == "latest") {
        // `latest` is a legacy alias rather than a formal snapshot tag.  Keep
        // its existing newest-known behavior without weakening formal tags.
        if (state.auction.a25_px_milli > 0) {
            price_milli = state.auction.a25_px_milli;
            source = "anchor_0925";
        } else if (state.auction.a24_px_milli > 0) {
            price_milli = state.auction.a24_px_milli;
            source = "anchor_0924";
        } else if (state.auction.a20_px_milli > 0) {
            price_milli = state.auction.a20_px_milli;
            source = "anchor_0920";
        } else {
            price_milli = state.px_milli;
            source = "quote_px";
        }
    }
    return {price_milli, price_milli > 0, price_milli > 0 ? source : "unavailable"};
}

AuctionChangeFact auction_change_fact(
    const QuoteState& state,
    const AuctionSnapshotPriceFact& price_fact
) {
    if (!price_fact.available || state.pc_milli <= 0) {
        return {};
    }
    return {
        static_cast<int>((static_cast<int64_t>(price_fact.price_milli) - state.pc_milli) * 10000 / state.pc_milli),
        true,
    };
}

bool AuctionCalculator::apply_tick(QuoteState& state, const RawTick& tick, MarketPhase phase, bool capture_current_date, bool capture_outside_auction, bool capture_full) const {
    AuctionState& auction = state.auction;
    bool changed = false;
    // The 0925 archive is frozen at 09:25:06 Shanghai, independently of
    // when the batch that triggers publication happens to arrive.
    constexpr int64_t day_ms = 86400000;
    const int64_t local_ms = (tick.ts_ms + 8LL * 3600000) % day_ms;
    const int64_t start_ms = (9LL * 3600 + 15 * 60) * 1000;
    const int64_t match_ms = (9LL * 3600 + 25 * 60) * 1000;
    const int64_t cutoff_ms = match_ms + 6000;
    if (capture_current_date && (phase == MarketPhase::Auction || capture_outside_auction) && tick.ts_ms > 0 && local_ms >= start_ms && local_ms <= cutoff_ms) {
        if (tick.bp_milli[0] > 0 && tick.ap_milli[0] > 0 && tick.ts_ms >= auction.a25_fb_ts_ms) {
            auction.a25_fb_milli = static_cast<int>((static_cast<int64_t>(tick.bp_milli[0]) + tick.ap_milli[0]) / 2);
            auction.a25_fb_ts_ms = tick.ts_ms;
            auction.a25_fb_pc_milli = tick.pc_milli;
            changed = true;
        }
        if (local_ms >= match_ms && tick.amt_yuan > 0 && tick.px_milli > 0 && tick.ts_ms >= auction.a25_match_ts_ms) {
            auction.a25_match_px_milli = tick.px_milli;
            auction.a25_match_ts_ms = tick.ts_ms;
            auction.a25_match_pc_milli = tick.pc_milli;
            changed = true;
        }
    }
    // Keep separate as-of states for the full views. Existing anchor and
    // Top200 state deliberately retain their original update semantics.
    const int64_t full_cutoffs[] = {(9LL * 3600 + 20 * 60 + 3) * 1000,
                                    (9LL * 3600 + 24 * 60 + 10) * 1000,
                                    (9LL * 3600 + 25 * 60 + 6) * 1000};
    if (capture_full && tick.ts_ms > 0 && local_ms >= start_ms) {
        for (int i = 0; i < 3; ++i) {
            AuctionFullCutoff& snap = auction.full_cutoffs[i];
            if (local_ms >= full_cutoffs[i]) continue;
            const int match_px = effective_matching_price_milli(tick, MarketPhase::Auction);
            if (match_px > 0 && (i != 2 || (local_ms >= match_ms && tick.amt_yuan > 0)) && tick.ts_ms >= snap.match_ts_ms) {
                snap.match_ts_ms = tick.ts_ms;
                snap.match_px_milli = match_px;
                snap.match_pc_milli = tick.pc_milli;
            }
            if (tick.bp_milli[0] > 0 && tick.ap_milli[0] > 0 && tick.ts_ms >= snap.fallback_ts_ms) {
                snap.fallback_ts_ms = tick.ts_ms;
                snap.fallback_px_milli = static_cast<int>((static_cast<int64_t>(tick.bp_milli[0]) + tick.ap_milli[0]) / 2);
                snap.fallback_pc_milli = tick.pc_milli;
            }
            if (tick.ts_ms >= snap.ts_ms) {
                if (i != 2 && match_px <= 0) { snap.match_px_milli = 0; snap.match_ts_ms = tick.ts_ms; }
                snap.ts_ms = tick.ts_ms;
                snap.latest_pc_milli = tick.pc_milli;
                snap.match_amt_yuan = calc_match_amt_yuan(tick, MarketPhase::Auction);
                const bool final_tick = local_ms >= match_ms && tick.amt_yuan > 0;
                snap.rest_bid_amt_yuan = final_tick ? order_book_amount_yuan(tick.bp_milli[0], tick.bv[0]) : calc_rest_bid_amt_yuan(tick);
                snap.rest_ask_amt_yuan = final_tick ? order_book_amount_yuan(tick.ap_milli[0], tick.av[0]) : calc_rest_ask_amt_yuan(tick);
            }
        }
    }
    if (phase != MarketPhase::Auction) {
        return changed;
    }
    auction.ts_ms = tick.ts_ms;
    auction.limit_state = state.limit_state;
    changed = capture_anchor_prices(auction, tick, phase) || changed;
    changed = update_match_and_rest(auction, tick, phase) || changed;
    return changed;
}

bool AuctionCalculator::capture_anchor_prices(AuctionState& auction, const RawTick& tick, MarketPhase phase) const {
    bool changed = false;
    const int hms = hms_from_timestamp_ms(tick.ts_ms);
    const int price_milli = effective_matching_price_milli(tick, phase);
    if (hms >= 92000 && hms <= 92020) {
        changed = changed || auction.a20_px_milli != price_milli;
        auction.a20_px_milli = price_milli;
    }
    if (hms >= 92400 && hms <= 92420) {
        changed = changed || auction.a24_px_milli != price_milli;
        auction.a24_px_milli = price_milli;
    }
    if (hms >= 92500 && hms <= 92520) {
        changed = changed || auction.a25_px_milli != price_milli;
        auction.a25_px_milli = price_milli;
    }
    return changed;
}

bool AuctionCalculator::update_match_and_rest(AuctionState& auction, const RawTick& tick, MarketPhase phase) const {
    const int64_t match_amt = calc_match_amt_yuan(tick, phase);
    const int64_t rest_bid = calc_rest_bid_amt_yuan(tick);
    const int64_t rest_ask = calc_rest_ask_amt_yuan(tick);
    const bool changed = auction.match_amt_yuan != match_amt ||
                         auction.rest_bid_amt_yuan != rest_bid ||
                         auction.rest_ask_amt_yuan != rest_ask;
    auction.match_amt_yuan = match_amt;
    auction.rest_bid_amt_yuan = rest_bid;
    auction.rest_ask_amt_yuan = rest_ask;
    return changed;
}

int64_t AuctionCalculator::calc_match_amt_yuan(const RawTick& tick, MarketPhase phase) const {
    const int price_milli = effective_matching_price_milli(tick, phase);
    if (phase != MarketPhase::Auction || price_milli <= 0) {
        return 0;
    }

    const int hms = hms_from_timestamp_ms(tick.ts_ms);
    if (hms >= 92500 && tick.amt_yuan > 0) {
        return tick.amt_yuan;
    }
    if (hms >= 92500 && tick.vol_units > 0) {
        return order_book_amount_yuan(price_milli, tick.vol_units);
    }

    const int64_t bid1_amt = order_book_amount_yuan(price_milli, tick.bv[0]);
    const int64_t ask1_amt = order_book_amount_yuan(price_milli, tick.av[0]);
    return std::max<int64_t>(0, std::min(bid1_amt, ask1_amt));
}

int AuctionCalculator::effective_matching_price_milli(const RawTick& tick, MarketPhase phase) const {
    if (phase != MarketPhase::Auction) {
        return 0;
    }

    // Before the auction is fixed, px_milli may be a virtual match price and
    // is not the authoritative side price for the auction book.  Use the
    // level-1 prices until 09:25; after that boundary px_milli may represent
    // the actual matched price and is preferred.
    const int hms = hms_from_timestamp_ms(tick.ts_ms);
    if (hms >= 92500 && tick.px_milli > 0) {
        return tick.px_milli;
    }

    const int bid1 = tick.bp_milli[0];
    const int ask1 = tick.ap_milli[0];
    if (bid1 > 0 && ask1 > 0 && bid1 == ask1) {
        return bid1;
    }
    return 0;
}

int64_t AuctionCalculator::calc_rest_bid_amt_yuan(const RawTick& tick) const {
    // During auction level 1 carries the price while level 2 carries the
    // unmatched quantity.  A level-2 price of zero is therefore not a zero
    // amount when level-2 quantity is present.
    const int64_t bid2_amt = order_book_amount_yuan(tick.bp_milli[0], tick.bv[1]);
    return std::max<int64_t>(0, bid2_amt);
}

int64_t AuctionCalculator::calc_rest_ask_amt_yuan(const RawTick& tick) const {
    const int64_t ask2_amt = order_book_amount_yuan(tick.ap_milli[0], tick.av[1]);
    return std::max<int64_t>(0, ask2_amt);
}

int AuctionCalculator::hms_from_timestamp_ms(int64_t ts_ms) const {
    if (ts_ms <= 0) {
        return 0;
    }
    const std::time_t seconds = static_cast<std::time_t>(ts_ms / 1000);
    std::tm local_tm{};
#if defined(_WIN32)
    localtime_s(&local_tm, &seconds);
#else
    localtime_r(&seconds, &local_tm);
#endif
    return local_tm.tm_hour * 10000 + local_tm.tm_min * 100 + local_tm.tm_sec;
}

}  // namespace t1_v2
