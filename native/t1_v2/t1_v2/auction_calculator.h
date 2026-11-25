#pragma once

#include <string>

#include "quote_state.h"
#include "raw_tick.h"
#include "runtime_mode.h"

namespace t1_v2 {

// A formal auction snapshot price is anchor-specific.  Keep availability
// separate from the numeric value so a missing anchor can never be rendered
// as a factual zero by a downstream writer.
struct AuctionSnapshotPriceFact {
    int price_milli = 0;
    bool available = false;
    const char* source = "unavailable";
};

struct AuctionChangeFact {
    int change_bp = 0;
    bool available = false;
};

AuctionSnapshotPriceFact auction_snapshot_price_fact(
    const QuoteState& state,
    const std::string& tag
);

AuctionChangeFact auction_change_fact(
    const QuoteState& state,
    const AuctionSnapshotPriceFact& price_fact
);

class AuctionCalculator {
public:
    bool apply_tick(QuoteState& state, const RawTick& tick, MarketPhase phase, bool capture_current_date = true, bool capture_outside_auction = false, bool capture_full = false) const;

private:
    bool capture_anchor_prices(AuctionState& auction, const RawTick& tick, MarketPhase phase) const;
    bool update_match_and_rest(AuctionState& auction, const RawTick& tick, MarketPhase phase) const;
    int64_t calc_match_amt_yuan(const RawTick& tick, MarketPhase phase) const;
    int64_t calc_rest_bid_amt_yuan(const RawTick& tick) const;
    int64_t calc_rest_ask_amt_yuan(const RawTick& tick) const;
    int effective_matching_price_milli(const RawTick& tick, MarketPhase phase) const;
    int hms_from_timestamp_ms(int64_t ts_ms) const;
};

}  // namespace t1_v2
