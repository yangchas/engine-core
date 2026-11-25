#pragma once

#include <cstdint>

#include "runtime_mode.h"

namespace t1_v2 {

struct AuctionFullCutoff {
    int64_t ts_ms = 0;
    int latest_pc_milli = 0;
    int64_t match_ts_ms = 0;
    int match_px_milli = 0;
    int match_pc_milli = 0;
    int64_t fallback_ts_ms = 0;
    int fallback_px_milli = 0;
    int fallback_pc_milli = 0;
    int64_t match_amt_yuan = 0;
    int64_t rest_bid_amt_yuan = 0;
    int64_t rest_ask_amt_yuan = 0;
};

struct AuctionState {
    AuctionFullCutoff full_cutoffs[3];
    int a20_px_milli = 0;
    int a24_px_milli = 0;
    int a25_px_milli = 0;
    int a25_match_px_milli = 0;
    int64_t a25_match_ts_ms = 0;
    int a25_match_pc_milli = 0;
    int a25_fb_milli = 0;
    int64_t a25_fb_ts_ms = 0;
    int a25_fb_pc_milli = 0;

    int64_t match_amt_yuan = 0;
    int64_t rest_bid_amt_yuan = 0;
    int64_t rest_ask_amt_yuan = 0;

    int64_t ts_ms = 0;
    LimitState limit_state = LimitState::Normal;
};

}  // namespace t1_v2
