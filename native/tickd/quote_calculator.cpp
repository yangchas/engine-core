#include "quote_calculator.h"

#include <algorithm>
#include <cstdlib>
#include <cstdint>
#include <ctime>

namespace t1_v2 {

void QuoteCalculator::apply_base_tick(QuoteState& state, const RawTick& tick, MarketPhase phase) const {
    state.ts_ms = tick.ts_ms;
    state.phase = phase;
    state.px_milli = tick.px_milli;
    state.pc_milli = tick.pc_milli;
    state.amt_yuan = tick.amt_yuan;
    state.vol_units = tick.vol_units;
    state.bid1_amt_yuan = tick.bp_milli[0] > 0 && tick.bv[0] > 0
        ? (static_cast<int64_t>(tick.bp_milli[0]) * tick.bv[0] * 100) / 1000 : 0;
    state.ask1_amt_yuan = tick.ap_milli[0] > 0 && tick.av[0] > 0
        ? (static_cast<int64_t>(tick.ap_milli[0]) * tick.av[0] * 100) / 1000 : 0;

    if (state.mx_milli <= 0 || tick.px_milli > state.mx_milli) {
        state.mx_milli = tick.px_milli;
    }
    if (state.mn_milli <= 0 || (tick.px_milli > 0 && tick.px_milli < state.mn_milli)) {
        state.mn_milli = tick.px_milli;
    }

    const int limit_up_milli = tick.limit_up_milli > 0
        ? tick.limit_up_milli
        : calc_limit_price_milli(tick, true);
    const int limit_down_milli = tick.limit_down_milli > 0
        ? tick.limit_down_milli
        : calc_limit_price_milli(tick, false);

    if (limit_up_milli > 0 &&
        near_price(limit_reference_price_milli(tick, true, phase), limit_up_milli) &&
        has_limit_seal_order(tick, limit_up_milli, true, phase)) {
        state.limit_state = LimitState::Up;
    } else if (limit_down_milli > 0 &&
               near_price(limit_reference_price_milli(tick, false, phase), limit_down_milli) &&
               has_limit_seal_order(tick, limit_down_milli, false, phase)) {
        state.limit_state = LimitState::Down;
    } else {
        state.limit_state = LimitState::Normal;
    }
}

void QuoteCalculator::recompute_fast_metrics(QuoteState& state) const {
    state.spd1m_bp = calc_speed_bp(state, 1);
    state.vec3m_bp = calc_speed_bp(state, 3);
    state.vec5m_bp = calc_speed_bp(state, 5);
    state.amt2m_yuan = calc_amount_delta_yuan(state, 2);
    state.amt5m_yuan = calc_amount_delta_yuan(state, 5);
}

int QuoteCalculator::calc_speed_bp(const QuoteState& state, int lookback_minutes) const {
    if (state.px_milli <= 0 || state.minute_ring.latest_minute < 0) {
        return 0;
    }
    const int target_minute = state.minute_ring.latest_minute - lookback_minutes;
    const MinuteSlot* slot = ring_reader_.get_slot(state.minute_ring, target_minute);
    if (!slot || slot->px_milli <= 0) {
        return 0;
    }
    const int64_t delta = static_cast<int64_t>(state.px_milli) - slot->px_milli;
    return static_cast<int>((delta * 10000) / slot->px_milli);
}

int64_t QuoteCalculator::calc_amount_delta_yuan(const QuoteState& state, int lookback_minutes) const {
    if (state.amt_yuan <= 0 || state.minute_ring.latest_minute < 0) {
        return 0;
    }
    const MinuteSlot* slot = ring_reader_.find_rolling_amount_reference(
        state.minute_ring,
        state.minute_ring.latest_minute,
        lookback_minutes
    );
    if (!slot) {
        return 0;
    }
    return std::max<int64_t>(0, state.amt_yuan - slot->amt_yuan);
}

int QuoteCalculator::calc_limit_price_milli(const RawTick& tick, bool upper) {
    if (tick.pc_milli <= 0 || tick.no_price_limit) {
        return 0;
    }
    const int band_bp = limit_band_bp(tick);
    if (band_bp <= 0) {
        return 0;
    }
    const int ratio_bp = upper ? (10000 + band_bp) : (10000 - band_bp);
    const int target_cent = static_cast<int>((static_cast<int64_t>(tick.pc_milli) * ratio_bp + 50000) / 100000);
    return target_cent * 10;
}

int QuoteCalculator::limit_band_bp(const RawTick& tick) {
    if (tick.no_price_limit || tick.limit_band_bp < 0) {
        return 0;
    }
    if (tick.limit_band_bp > 0) {
        return tick.limit_band_bp;
    }
    if (is_30pct_market(tick.market)) {
        return 3000;
    }
    if (is_20pct_symbol(tick.symbol)) {
        return 2000;
    }
    return 1000;
}

bool QuoteCalculator::is_20pct_symbol(const char* symbol) {
    if (!symbol || symbol[0] == '\0' || symbol[1] == '\0') {
        return false;
    }
    return (symbol[0] == '3' && symbol[1] == '0') ||
           (symbol[0] == '6' && symbol[1] == '8');
}

bool QuoteCalculator::is_30pct_market(const char* market) {
    if (!market || market[0] == '\0') {
        return false;
    }
    return (market[0] == 'b' && market[1] == 'j') ||
           (market[0] == 'b' && market[1] == 's') ||
           (market[0] == 'n' && market[1] == 'q');
}

bool QuoteCalculator::near_price(int lhs_milli, int rhs_milli) {
    if (lhs_milli <= 0 || rhs_milli <= 0) {
        return false;
    }
    return std::abs(lhs_milli - rhs_milli) <= 10;
}

int QuoteCalculator::limit_reference_price_milli(const RawTick& tick, bool upper, MarketPhase phase) {
    if (phase == MarketPhase::Auction && hms_from_timestamp_ms(tick.ts_ms) < 92500) {
        // Before the auction is fixed, use the side-specific level-1 price;
        // px_milli may be a virtual match price and is not the auction book
        // price used by the seal contract.
        return upper ? tick.bp_milli[0] : tick.ap_milli[0];
    }
    return tick.px_milli;
}

bool QuoteCalculator::has_limit_seal_order(
    const RawTick& tick,
    int limit_price_milli,
    bool upper,
    MarketPhase phase
) {
    if (limit_price_milli <= 0) {
        return false;
    }
    // During auction, level 1 carries the price and level 2 carries the
    // unmatched limit quantity. During continuous trading both price and
    // visible seal quantity come from level 1.
    const int price_level = 0;
    const int volume_level = phase == MarketPhase::Auction ? 1 : 0;
    const int price = upper ? tick.bp_milli[price_level] : tick.ap_milli[price_level];
    const int64_t volume = upper ? tick.bv[volume_level] : tick.av[volume_level];
    return volume > 0 && near_price(price, limit_price_milli);
}

int QuoteCalculator::hms_from_timestamp_ms(int64_t ts_ms) {
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
