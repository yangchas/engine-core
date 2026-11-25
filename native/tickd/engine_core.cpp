#include "engine_core.h"

namespace tickd {

EngineCore::EngineCore(const ConfigV2& config) : config_(config) {}

EngineCore::~EngineCore() {
    shutdown();
}

bool EngineCore::initialize() {
    quote_store_ = std::make_unique<QuoteStateStore>();
    snapshot_trigger_ = std::make_unique<SnapshotTrigger>(config_);
    last_batch_phase_ = MarketPhase::Premarket;
    has_last_batch_phase_ = false;
    initialized_ = true;
    return true;
}

void EngineCore::shutdown() {
    quote_store_.reset();
    snapshot_trigger_.reset();
    last_batch_phase_ = MarketPhase::Premarket;
    has_last_batch_phase_ = false;
    initialized_ = false;
}

EngineProcessStats EngineCore::on_batch(const TickBatch& batch) {
    EngineProcessStats stats = advance_clock(batch.logical_ts_ms);
    stats.tick_count = static_cast<uint32_t>(batch.ticks.size());
    const int batch_trade_date = snapshot_trigger_
        ? snapshot_trigger_->trade_date_for_timestamp_ms(batch.logical_ts_ms)
        : 0;
    const int previous_trade_date = snapshot_trigger_
        ? snapshot_trigger_->current_trade_date()
        : 0;
    // A late batch from an older session must not mutate today's QuoteState.
    // The session date is derived from the input clock, not the host clock.
    // advance_clock returns without changing the trigger for such a batch;
    // detect that same condition before applying its ticks.
    if (previous_trade_date > 0 &&
        batch_trade_date > 0 &&
        batch_trade_date < previous_trade_date) {
        return stats;
    }

    if (!initialized_ || !quote_store_) {
        return stats;
    }

    quote_store_->begin_batch();
    if (!has_last_batch_phase_ || stats.phase != last_batch_phase_) {
        refresh_phase_for_active_quotes(stats.phase);
        last_batch_phase_ = stats.phase;
        has_last_batch_phase_ = true;
    }
    for (const RawTick& tick : batch.ticks) {
        QuoteState& state = quote_store_->get_or_create(tick);
        const int tick_trade_date = snapshot_trigger_
            ? snapshot_trigger_->trade_date_for_timestamp_ms(tick.ts_ms)
            : 0;
        const int state_trade_date = snapshot_trigger_
            ? snapshot_trigger_->trade_date_for_timestamp_ms(state.ts_ms)
            : 0;
        if (tick_trade_date > 0 &&
            state_trade_date > 0 &&
            tick_trade_date > state_trade_date) {
            state.auction_fields_reset_pending = true;
            state.dirty_mask |= DIRTY_RUNTIME;
        }
        const int64_t previous_bid1_amt_yuan = state.bid1_amt_yuan;
        const int64_t previous_ask1_amt_yuan = state.ask1_amt_yuan;
        const int previous_px = state.px_milli;
        const int64_t previous_amt = state.amt_yuan;
        const int64_t previous_vol = state.vol_units;
        const int previous_spd1m = state.spd1m_bp;
        const int64_t previous_amt2m = state.amt2m_yuan;
        const int64_t previous_amt5m = state.amt5m_yuan;
        const int previous_vec3m = state.vec3m_bp;
        const int previous_vec5m = state.vec5m_bp;
        const int64_t previous_inst_vol = state.inst_vol;
        const int64_t previous_inst_amt_yuan = state.inst_amt_yuan;
        const int64_t previous_large_net_yuan = state.large_net_yuan;

        tick_delta_calculator_.apply_tick(
            state,
            tick,
            stats.phase,
            static_cast<int64_t>(config_.thresholds.large_order_threshold)
        );
        quote_calculator_.apply_base_tick(state, tick, stats.phase);
        const bool auction_changed = auction_calculator_.apply_tick(state, tick, stats.phase,
            tick_trade_date > 0 && tick_trade_date == previous_trade_date, true,
            tick_trade_date > 0 && tick_trade_date == batch_trade_date);
        minute_ring_updater_.apply_tick(state.minute_ring, tick);
        quote_calculator_.recompute_fast_metrics(state);
        dirty_marker_.mark_after_tick(
            state,
            previous_px,
            previous_amt,
            previous_vol,
            previous_spd1m,
            previous_amt2m,
            previous_amt5m,
            previous_vec3m,
            previous_vec5m,
            previous_inst_vol,
            previous_inst_amt_yuan,
            previous_large_net_yuan,
            auction_changed
        );
        if (state.bid1_amt_yuan != previous_bid1_amt_yuan ||
            state.ask1_amt_yuan != previous_ask1_amt_yuan) {
            state.dirty_mask |= DIRTY_QUOTE_CORE;
        }
    }
    stats.new_symbol_count = quote_store_->last_batch_symbol_count();
    return stats;
}

EngineProcessStats EngineCore::on_clock(int64_t logical_ts_ms) {
    return advance_clock(logical_ts_ms);
}

EngineProcessStats EngineCore::advance_clock(int64_t logical_ts_ms) {
    EngineProcessStats stats;
    stats.logical_ts_ms = logical_ts_ms;
    stats.phase = phase_resolver_.resolve(logical_ts_ms);
    const int clock_trade_date = snapshot_trigger_
        ? snapshot_trigger_->trade_date_for_timestamp_ms(logical_ts_ms)
        : 0;
    const int previous_trade_date = snapshot_trigger_
        ? snapshot_trigger_->current_trade_date()
        : 0;
    if (previous_trade_date > 0 &&
        clock_trade_date > 0 &&
        clock_trade_date < previous_trade_date) {
        return stats;
    }
    if (snapshot_trigger_) {
        stats.snapshot_trigger = snapshot_trigger_->update(logical_ts_ms, stats.phase);
        if (clock_trade_date > previous_trade_date) {
            clear_auction_state_for_new_trade_date();
        }
    }
    if (initialized_ && quote_store_ &&
        (!has_last_batch_phase_ || stats.phase != last_batch_phase_)) {
        refresh_phase_for_active_quotes(stats.phase);
        last_batch_phase_ = stats.phase;
        has_last_batch_phase_ = true;
    }
    return stats;
}

void EngineCore::clear_auction_state_for_new_trade_date() {
    if (!quote_store_) {
        return;
    }
    quote_store_->for_each_active([](QuoteState& state) {
        state.auction = AuctionState{};
    });
}

void EngineCore::refresh_phase_for_active_quotes(MarketPhase phase) {
    if (!quote_store_) {
        return;
    }
    quote_store_->for_each_active([phase](QuoteState& state) {
        if (state.phase == phase) {
            return;
        }
        state.phase = phase;
        state.dirty_mask |= DIRTY_RUNTIME;
    });
}

const QuoteStateStore& EngineCore::quote_store() const {
    return *quote_store_;
}

QuoteStateStore& EngineCore::mutable_quote_store() {
    return *quote_store_;
}

}  // namespace tickd
