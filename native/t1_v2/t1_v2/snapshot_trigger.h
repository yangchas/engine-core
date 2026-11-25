#pragma once

#include <cstdint>

#include "config_v2.h"
#include "runtime_mode.h"

namespace t1_v2 {

// Returns the 09:32:10 Asia/Shanghai barrier for the trade date carried by
// ts_ms.  This is a control-plane timestamp, not a market-data tick.
int64_t opening_cutoff_timestamp_ms(int64_t ts_ms);

struct SnapshotTriggerState {
    bool emit_a20 = false;
    bool emit_a24 = false;
    bool emit_a25 = false;
    bool emit_latest_auction = false;
    bool emit_runtime = false;
    bool emit_opening_cutoff = false;
};

class SnapshotTrigger {
public:
    explicit SnapshotTrigger(const ConfigV2& config);

    SnapshotTriggerState update(int64_t logical_ts_ms, MarketPhase phase);

    bool a20_done() const { return emitted_a20_; }
    bool a24_done() const { return emitted_a24_; }
    bool a25_done() const { return emitted_a25_; }
    bool opening_cutoff_done() const { return emitted_opening_cutoff_; }
    int current_trade_date() const { return current_trade_date_; }
    int trade_date_for_timestamp_ms(int64_t ts_ms) const { return trade_date_from_timestamp_ms(ts_ms); }

private:
    int hms_from_timestamp_ms(int64_t ts_ms) const;
    int trade_date_from_timestamp_ms(int64_t ts_ms) const;

private:
    ConfigV2 config_;
    bool emitted_a20_ = false;
    bool emitted_a24_ = false;
    bool emitted_a25_ = false;
    bool emitted_opening_cutoff_ = false;
    int current_trade_date_ = 0;
    int64_t last_logical_ts_ms_ = 0;
    int64_t last_latest_auction_ts_ms_ = 0;
    int64_t last_runtime_ts_ms_ = 0;
};

}  // namespace t1_v2
