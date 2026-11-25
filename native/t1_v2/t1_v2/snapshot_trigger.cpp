#include "snapshot_trigger.h"

#include <ctime>

namespace t1_v2 {

namespace {

constexpr int64_t kMillisPerSecond = 1000;
constexpr int64_t kMillisPerDay = 24LL * 60LL * 60LL * kMillisPerSecond;
constexpr int64_t kShanghaiOffsetMs = 8LL * 60LL * 60LL * kMillisPerSecond;

bool shanghai_tm_from_timestamp_ms(int64_t ts_ms, std::tm& out) {
    if (ts_ms <= 0) {
        return false;
    }
    // The market clock is fixed to Asia/Shanghai.  Derive it from the input
    // epoch only; never consult the host's current date or timezone setting.
    const std::time_t shanghai_seconds = static_cast<std::time_t>(ts_ms / kMillisPerSecond + 8 * 60 * 60);
#if defined(_WIN32)
    return gmtime_s(&out, &shanghai_seconds) == 0;
#else
    return gmtime_r(&shanghai_seconds, &out) != nullptr;
#endif
}

}  // namespace

int64_t opening_cutoff_timestamp_ms(int64_t ts_ms) {
    if (ts_ms <= 0) {
        return 0;
    }
    const int64_t shanghai_day = (ts_ms + kShanghaiOffsetMs) / kMillisPerDay;
    return shanghai_day * kMillisPerDay - kShanghaiOffsetMs +
        (9LL * 60LL * 60LL + 32LL * 60LL + 10LL) * kMillisPerSecond;
}

SnapshotTrigger::SnapshotTrigger(const ConfigV2& config) : config_(config) {}

SnapshotTriggerState SnapshotTrigger::update(int64_t logical_ts_ms, MarketPhase phase) {
    SnapshotTriggerState state;
    const int trade_date = trade_date_from_timestamp_ms(logical_ts_ms);
    if (trade_date <= 0) {
        return state;
    }
    if (current_trade_date_ > 0 && trade_date < current_trade_date_) {
        // A late/out-of-order tick from an older session must not rewind
        // day-local state or advance any interval marker.
        return state;
    }
    if (trade_date != current_trade_date_) {
        current_trade_date_ = trade_date;
        emitted_a20_ = false;
        emitted_a24_ = false;
        emitted_a25_ = false;
        emitted_opening_cutoff_ = false;
        last_logical_ts_ms_ = 0;
        last_latest_auction_ts_ms_ = 0;
        last_runtime_ts_ms_ = 0;
    }
    const int hms = hms_from_timestamp_ms(logical_ts_ms);
    const bool logical_ts_is_newer = last_logical_ts_ms_ <= 0 || logical_ts_ms >= last_logical_ts_ms_;
    const int previous_hms = last_logical_ts_ms_ > 0
        ? hms_from_timestamp_ms(last_logical_ts_ms_)
        : 0;

    if (!emitted_a20_ && hms >= 92003 && hms < 92400) {
        emitted_a20_ = true;
        state.emit_a20 = true;
    }
    if (!emitted_a24_ && hms >= 92410 && hms < 92500) {
        emitted_a24_ = true;
        state.emit_a24 = true;
    }
    // Freeze the formal 0925 snapshot only after the required six-second
    // settling delay.  Auction state continues updating during this window;
    // this barrier only controls when the 0925 projection is emitted.
    if (!emitted_a25_ && hms >= 92506 && hms < 93000) {
        emitted_a25_ = true;
        state.emit_a25 = true;
    }

    // Opening is frozen at the formal 09:32:10 cutoff.  The event is
    // deliberately separate from auction snapshots so a delayed/empty
    // interval can advance it through the explicit replay clock path without
    // fabricating a market-data tick.  A live/replay clock may cross the
    // exact second between observations, so emit once when an accepted
    // logical timestamp crosses the barrier.  A first observation arriving
    // after the barrier remains fail-closed and does not fabricate a cutoff.
    const bool at_opening_cutoff = hms == 93210;
    const bool crossed_opening_cutoff = logical_ts_is_newer &&
        last_logical_ts_ms_ > 0 && previous_hms < 93210 && hms > 93210;
    if (!emitted_opening_cutoff_ && logical_ts_is_newer &&
        (at_opening_cutoff || crossed_opening_cutoff)) {
        emitted_opening_cutoff_ = true;
        state.emit_opening_cutoff = true;
    }

    if (phase == MarketPhase::Auction &&
        (last_latest_auction_ts_ms_ <= 0 ||
         logical_ts_ms - last_latest_auction_ts_ms_ >= config_.processing.a2_latest_interval_ms)) {
        last_latest_auction_ts_ms_ = logical_ts_ms;
        state.emit_latest_auction = true;
    }

    if (last_runtime_ts_ms_ <= 0 ||
        logical_ts_ms - last_runtime_ts_ms_ >= config_.processing.runtime_interval_ms) {
        last_runtime_ts_ms_ = logical_ts_ms;
        state.emit_runtime = true;
    }

    if (logical_ts_is_newer) {
        last_logical_ts_ms_ = logical_ts_ms;
    }

    return state;
}

int SnapshotTrigger::hms_from_timestamp_ms(int64_t ts_ms) const {
    std::tm market_tm{};
    if (!shanghai_tm_from_timestamp_ms(ts_ms, market_tm)) {
        return 0;
    }
    return market_tm.tm_hour * 10000 + market_tm.tm_min * 100 + market_tm.tm_sec;
}

int SnapshotTrigger::trade_date_from_timestamp_ms(int64_t ts_ms) const {
    std::tm market_tm{};
    if (!shanghai_tm_from_timestamp_ms(ts_ms, market_tm)) {
        return 0;
    }
    return (market_tm.tm_year + 1900) * 10000 + (market_tm.tm_mon + 1) * 100 + market_tm.tm_mday;
}

}  // namespace t1_v2
