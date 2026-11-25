#include "self_test.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <iostream>
#include <memory>
#include <regex>
#include <sstream>
#include <string>
#include <vector>

#include "config_v2.h"
#include "auction_calculator.h"
#include "engine_core.h"
#include "payload_decompressor.h"
#include "protobuf_tick_decoder.h"
#include "rabbitmq_batch_decoder.h"
#include "rabbitmq_tick_source.h"
#include "rabbitmq_wire_message.h"
#include "raw_tick_converter.h"
#include "redis_command_executor.h"
#include "redis_command_formatter.h"
#include "redis_v2_writer.h"
#include "replay_slice_scheduler.h"
#include "runtime_batch_stats.h"
#include "runtime_execution_coordinator.h"
#include "runtime_loop.h"
#include "runtime_pipeline.h"
#include "source_tick_batch_builder.h"
#include "snapshot_trigger.h"
#include "tdengine_command_executor.h"
#include "td_replay_query.h"
#include "td_replay_row_converter.h"
#include "td_replay_tick_source.h"
#include "tdengine_row_adapter.h"
#include "tdengine_v2_writer.h"

#if defined(T1_V2_ENABLE_ZLIB)
#include <zlib.h>
#endif

#if defined(T1_V2_ENABLE_PROTOBUF)
#include "schema.pb.h"
#endif

namespace t1_v2 {
namespace {

int64_t make_local_ts_ms(int year, int month, int day, int hour, int minute, int second) {
    std::tm tm{};
    tm.tm_year = year - 1900;
    tm.tm_mon = month - 1;
    tm.tm_mday = day;
    tm.tm_hour = hour;
    tm.tm_min = minute;
    tm.tm_sec = second;
    tm.tm_isdst = -1;
    return static_cast<int64_t>(std::mktime(&tm)) * 1000;
}

void set_symbol(RawTick& tick, const char* symbol) {
    std::memset(tick.symbol, 0, sizeof(tick.symbol));
    std::memcpy(tick.symbol, symbol, sizeof(tick.symbol) - 1);
}

void set_market(RawTick& tick, const char* market) {
    std::memset(tick.market, 0, sizeof(tick.market));
    if (market) {
        std::memcpy(tick.market, market, std::min<std::size_t>(std::strlen(market), sizeof(tick.market) - 1));
    }
}

// This builds a normalized RawTick, not a wire/protobuf StockData record.
// Source adapters must separately prove float/int64 feed fields are converted
// into these fixed-point units without unit drift.
RawTick make_tick(const char* symbol, int64_t ts_ms, int px_milli, int64_t amt_yuan, const char* market = "") {
    RawTick tick;
    set_symbol(tick, symbol);
    set_market(tick, market);
    tick.ts_ms = ts_ms;
    tick.px_milli = px_milli;
    tick.pc_milli = 10000;
    tick.o_milli = px_milli;
    tick.h_milli = px_milli;
    tick.l_milli = px_milli;
    tick.amt_yuan = amt_yuan;
    tick.vol_units = 10000;
    tick.bp_milli[0] = px_milli;
    tick.ap_milli[0] = px_milli;
    tick.bp_milli[1] = px_milli;
    tick.ap_milli[1] = px_milli;
    tick.bv[0] = 1000;
    tick.av[0] = 800;
    tick.bv[1] = 3000;
    tick.av[1] = 2000;
    return tick;
}

class FakeTickSource final : public ITickSource {
public:
    struct Counters {
        uint32_t ack_count = 0;
        uint32_t reject_count = 0;
        uint64_t last_delivery_tag = 0;
        bool last_requeue = false;
    };

    explicit FakeTickSource(std::vector<TickSourceResult> results, Counters* counters = nullptr)
        : results_(std::move(results)), counters_(counters) {}

    bool start() override {
        started_ = true;
        index_ = 0;
        return true;
    }

    void stop() override {
        started_ = false;
    }

    TickSourceResult next_batch() override {
        if (!started_) {
            TickSourceResult result;
            result.status = TickSourceStatus::Error;
            result.error_msg = "fake source not started";
            return result;
        }
        if (index_ >= results_.size()) {
            TickSourceResult result;
            result.status = TickSourceStatus::EndOfStream;
            return result;
        }
        return results_[index_++];
    }

    RuntimeMode mode() const override {
        return RuntimeMode::Replay;
    }

    bool ack(const TickSourceResult& result) override {
        if (!counters_) {
            return true;
        }
        ++counters_->ack_count;
        counters_->last_delivery_tag = result.delivery_tag;
        return true;
    }

    bool reject(const TickSourceResult& result, bool requeue) override {
        if (!counters_) {
            return true;
        }
        ++counters_->reject_count;
        counters_->last_delivery_tag = result.delivery_tag;
        counters_->last_requeue = requeue;
        return true;
    }

private:
    std::vector<TickSourceResult> results_;
    Counters* counters_ = nullptr;
    size_t index_ = 0;
    bool started_ = false;
};

class FailingRedisCommandExecutor final : public IRedisCommandExecutor {
public:
    RedisExecutionResult preflight() override {
        return {};
    }

    RedisExecutionResult execute(const std::vector<RedisCommand>& commands) override {
        RedisExecutionResult result;
        result.command_count = static_cast<int>(commands.size());
        result.ok = commands.empty();
        if (!result.ok) {
            result.error = "intentional redis failure";
        }
        return result;
    }
};

class RecordingRedisCommandExecutor final : public IRedisCommandExecutor {
public:
    RedisExecutionResult preflight() override {
        return {};
    }

    RedisExecutionResult execute(const std::vector<RedisCommand>& commands) override {
        executed.insert(executed.end(), commands.begin(), commands.end());
        RedisExecutionResult result;
        result.command_count = static_cast<int>(commands.size());
        return result;
    }

    std::vector<RedisCommand> executed;
};

bool expect(bool condition, const char* message) {
    if (!condition) {
        std::cerr << "self-test failed: " << message << std::endl;
        return false;
    }
    return true;
}

int count_dirty_quotes(const QuoteStateStore& store) {
    int count = 0;
    store.for_each_active([&](const QuoteState& state) {
        if (state.dirty_mask != DIRTY_NONE) {
            ++count;
        }
    });
    return count;
}

bool test_c1574_real_book_amounts() {
    std::ifstream input("/home/exedev/fw/evidence/C1574/real_ticks.json");
    if (!expect(input.good(), "C1574 real TD fixture available")) return false;
    const std::string json((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    const std::regex row(R"re(\[\s*"2026-11-25 09:35:0([01])"\s*,\s*"(603960|301569)"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\])re");
    std::vector<RawTick> ticks;
    for (auto it = std::sregex_iterator(json.begin(), json.end(), row); it != std::sregex_iterator(); ++it) {
        const auto& m = *it;
        RawTick tick;
        set_symbol(tick, m[2].str().c_str());
        set_market(tick, m[2].str() == "603960" ? "sh" : "sz");
        tick.ts_ms = make_local_ts_ms(2026, 11, 25, 9, 35, std::stoi(m[1]));
        tick.px_milli = std::stoi(m[3]); tick.pc_milli = std::stoi(m[4]);
        tick.amt_yuan = std::stoll(m[5]); tick.vol_units = std::stoll(m[6]);
        tick.bp_milli[0] = std::stoi(m[7]); tick.ap_milli[0] = std::stoi(m[8]);
        tick.bv[0] = std::stoll(m[9]); tick.av[0] = std::stoll(m[10]);
        ticks.push_back(tick);
    }
    if (!expect(ticks.size() == 2 && std::string(ticks[0].symbol) == "603960" &&
                std::string(ticks[1].symbol) == "301569", "C1574 two real TD rows")) return false;
    ConfigV2 config;
    EngineCore engine(config);
    RedisV2Writer writer(config);
    if (!expect(engine.initialize() && writer.initialize(), "C1574 initialize")) return false;
    TickBatch batch;
    batch.logical_ts_ms = ticks[0].ts_ms;
    batch.ticks = ticks;
    engine.on_batch(batch);
    const auto field_is = [](const RedisFieldList& fields, const char* key, const char* value) {
        for (const auto& field : fields) if (field.first == key) return field.second == value;
        return false;
    };
    bool sealed = false, ordinary = false;
    for (const auto& cmd : writer.build_q2_commands(engine.quote_store(), batch.logical_ts_ms)) {
        if (cmd.type != RedisCommandType::HSetMulti) continue;
        if (cmd.key == "q2:603960") sealed = field_is(cmd.fields, "ls", "1") && field_is(cmd.fields, "b1a", "641160600") && field_is(cmd.fields, "a1a", "0");
        if (cmd.key == "q2:301569") ordinary = field_is(cmd.fields, "ls", "0") && field_is(cmd.fields, "b1a", "14688") && field_is(cmd.fields, "a1a", "372324");
    }
    if (!expect(sealed && ordinary, "C1574 real book q2 amounts and limit state")) return false;
    QuoteState& state = engine.mutable_quote_store().get_or_create(ticks[0]);
    writer.mark_q2_written(state, batch.logical_ts_ms);
    RawTick changed = ticks[0];
    changed.bv[0] = 5000;
    changed.ts_ms += 1000;
    batch.logical_ts_ms = changed.ts_ms; batch.ticks = {changed};
    engine.on_batch(batch);
    auto fields = writer.build_q2_fields(state);
    if (!expect((state.dirty_mask & DIRTY_QUOTE_CORE) && writer.should_write_q2(state, batch.logical_ts_ms) &&
                field_is(fields, "b1a", "9300000") && field_is(fields, "a1a", "0"), "C1574 cancellation dirties q2")) return false;
    writer.mark_q2_written(state, batch.logical_ts_ms);
    changed.ap_milli[0] = 18600; changed.av[0] = 120; changed.ts_ms += 1000;
    batch.logical_ts_ms = changed.ts_ms; batch.ticks = {changed};
    engine.on_batch(batch);
    fields = writer.build_q2_fields(state);
    if (!expect((state.dirty_mask & DIRTY_QUOTE_CORE) && writer.should_write_q2(state, batch.logical_ts_ms) &&
                field_is(fields, "b1a", "9300000") && field_is(fields, "a1a", "223200"), "C1574 ask appearance dirties q2")) return false;
    return true;
}

}  // namespace

bool run_self_test() {
    if (!test_c1574_real_book_amounts()) return false;
    const std::string wire_header =
        "{\"record_count\":2,\"compression\":\"GZIP\",\"original_size\":100,"
        "\"compressed_size\":60,\"timestamp\":1777425845000,"
        "\"batch_id\":\"b1\",\"proto_version\":\"v1\"}";
    const std::string wire_payload = "protobuf-bytes";
    std::vector<char> wire_body;
    const uint32_t header_len = static_cast<uint32_t>(wire_header.size());
    wire_body.push_back(static_cast<char>((header_len >> 24) & 0xFF));
    wire_body.push_back(static_cast<char>((header_len >> 16) & 0xFF));
    wire_body.push_back(static_cast<char>((header_len >> 8) & 0xFF));
    wire_body.push_back(static_cast<char>(header_len & 0xFF));
    wire_body.insert(wire_body.end(), wire_header.begin(), wire_header.end());
    wire_body.insert(wire_body.end(), wire_payload.begin(), wire_payload.end());

    RabbitMqWireMessageView wire_view;
    std::string wire_error;
    if (!expect(RabbitMqWireMessageParser::parse(wire_body, wire_view, &wire_error), "rabbit wire parses")) return false;
    if (!expect(wire_view.header.record_count == 2, "rabbit wire record count")) return false;
    if (!expect(wire_view.header.compression == "GZIP", "rabbit wire compression")) return false;
    if (!expect(wire_view.payload_size == wire_payload.size(), "rabbit wire payload size")) return false;

    std::vector<char> decompressed;
    const PayloadDecompressResult none_decompress = PayloadDecompressor::decompress(
        PayloadCompression::None,
        wire_payload.data(),
        wire_payload.size(),
        decompressed
    );
    if (!expect(none_decompress.ok, "none payload decompress")) return false;
    if (!expect(std::string(decompressed.begin(), decompressed.end()) == wire_payload, "none payload passthrough")) return false;

    std::vector<char> gzip_out;
    const PayloadDecompressResult gzip_decompress = PayloadDecompressor::decompress(
        PayloadDecompressor::parse_compression(wire_view.header.compression),
        wire_view.payload,
        wire_view.payload_size,
        gzip_out
    );
#if defined(T1_V2_ENABLE_ZLIB)
    if (!expect(!gzip_decompress.ok, "invalid gzip payload rejected")) return false;

    const std::string zlib_plain = "decoded-protobuf-batch";
    std::vector<char> zlib_payload(compressBound(static_cast<uLong>(zlib_plain.size())));
    uLongf zlib_payload_size = static_cast<uLongf>(zlib_payload.size());
    const int compress_ret = compress2(
        reinterpret_cast<Bytef*>(zlib_payload.data()),
        &zlib_payload_size,
        reinterpret_cast<const Bytef*>(zlib_plain.data()),
        static_cast<uLong>(zlib_plain.size()),
        Z_BEST_SPEED
    );
    if (!expect(compress_ret == Z_OK, "zlib test compression")) return false;
    zlib_payload.resize(static_cast<std::size_t>(zlib_payload_size));
    std::vector<char> zlib_decoded;
    const PayloadDecompressResult zlib_result = PayloadDecompressor::decompress(
        PayloadCompression::Gzip,
        zlib_payload.data(),
        zlib_payload.size(),
        zlib_decoded
    );
    if (!expect(zlib_result.ok, "zlib payload decompress")) return false;
    if (!expect(std::string(zlib_decoded.begin(), zlib_decoded.end()) == zlib_plain, "zlib payload roundtrip")) return false;
#else
    if (!expect(!gzip_decompress.ok && gzip_decompress.unsupported, "gzip unsupported without zlib")) return false;
#endif

    ConfigV2 trigger_config;
    SnapshotTrigger trigger(trigger_config);
    const SnapshotTriggerState before_a25 =
        trigger.update(make_local_ts_ms(2026, 4, 29, 9, 24, 59), MarketPhase::Auction);
    if (!expect(!before_a25.emit_a25, "a25 not emitted before 09:25:06")) return false;
    const SnapshotTriggerState before_a25_settle =
        trigger.update(make_local_ts_ms(2026, 4, 29, 9, 25, 0), MarketPhase::Auction);
    if (!expect(!before_a25_settle.emit_a25, "a25 waits for settling delay")) return false;
    const SnapshotTriggerState at_a25 =
        trigger.update(make_local_ts_ms(2026, 4, 29, 9, 25, 6), MarketPhase::Auction);
    if (!expect(at_a25.emit_a25, "a25 emitted after six-second settling delay")) return false;
    const SnapshotTriggerState repeat_a25 =
        trigger.update(make_local_ts_ms(2026, 4, 29, 9, 25, 8), MarketPhase::Auction);
    if (!expect(!repeat_a25.emit_a25, "a25 emitted only once")) return false;

    SnapshotTrigger delayed_cutoff_trigger(trigger_config);
    const SnapshotTriggerState before_cutoff_barrier = delayed_cutoff_trigger.update(
        make_local_ts_ms(2026, 4, 29, 9, 32, 9), MarketPhase::Intraday
    );
    if (!expect(!before_cutoff_barrier.emit_opening_cutoff,
                "opening cutoff stays pending before exact barrier")) return false;
    const SnapshotTriggerState crossed_cutoff_barrier = delayed_cutoff_trigger.update(
        make_local_ts_ms(2026, 4, 29, 9, 32, 11), MarketPhase::Intraday
    );
    if (!expect(crossed_cutoff_barrier.emit_opening_cutoff,
                "opening cutoff emits when logical time crosses barrier")) return false;
    const SnapshotTriggerState repeated_crossed_cutoff = delayed_cutoff_trigger.update(
        make_local_ts_ms(2026, 4, 29, 9, 32, 12), MarketPhase::Intraday
    );
    if (!expect(!repeated_crossed_cutoff.emit_opening_cutoff,
                "crossed opening cutoff emits only once")) return false;
    SnapshotTrigger late_cutoff_trigger(trigger_config);
    const SnapshotTriggerState late_cutoff = late_cutoff_trigger.update(
        make_local_ts_ms(2026, 4, 29, 9, 32, 11), MarketPhase::Intraday
    );
    if (!expect(!late_cutoff.emit_opening_cutoff,
                "first post-cutoff observation stays fail-closed")) return false;
    const SnapshotTriggerState older_exact_cutoff = late_cutoff_trigger.update(
        make_local_ts_ms(2026, 4, 29, 9, 32, 10), MarketPhase::Intraday
    );
    if (!expect(!older_exact_cutoff.emit_opening_cutoff,
                "out-of-order exact cutoff does not fabricate event")) return false;

    const SnapshotTriggerState next_day_a20 =
        trigger.update(make_local_ts_ms(2026, 4, 30, 9, 20, 5), MarketPhase::Auction);
    if (!expect(next_day_a20.emit_a20, "a20 resets on next input trade date")) return false;
    const SnapshotTriggerState next_day_a24 =
        trigger.update(make_local_ts_ms(2026, 4, 30, 9, 24, 15), MarketPhase::Auction);
    if (!expect(next_day_a24.emit_a24, "a24 resets on next input trade date")) return false;
    const SnapshotTriggerState next_day_a25 =
        trigger.update(make_local_ts_ms(2026, 4, 30, 9, 25, 6), MarketPhase::Auction);
    if (!expect(next_day_a25.emit_a25, "a25 resets on next input trade date")) return false;
    const SnapshotTriggerState late_previous_day =
        trigger.update(make_local_ts_ms(2026, 4, 29, 9, 25, 5), MarketPhase::Auction);
    if (!expect(!late_previous_day.emit_a20 && !late_previous_day.emit_a24 && !late_previous_day.emit_a25,
                "older trade date does not rewind snapshot state")) return false;

    ConfigV2 default_replay_config;
    if (!expect(default_replay_config.tdengine.replay_table == "stock_tick_v2", "default replay table is v2 ticks")) return false;
    if (!expect(default_replay_config.replay.write_redis, "replay writes redis by default")) return false;
    if (!expect(!default_replay_config.replay.write_tdengine, "replay suppresses tdengine by default")) return false;

    ConfigV2 td_replay_local_output;
    td_replay_local_output.runtime_mode = RuntimeMode::Replay;
    td_replay_local_output.replay.q2frame_path = "/tmp/t1_v2_p1_q2frame.jsonl";
    td_replay_local_output.replay.write_redis = true;
    td_replay_local_output.replay.write_tdengine = true;
    td_replay_local_output.processing.dry_run = true;
    if (!expect(configure_local_replay_output(td_replay_local_output),
                "td replay q2frame selects local output")) return false;
    if (!expect(!td_replay_local_output.replay.write_redis,
                "td replay q2frame suppresses production redis")) return false;
    if (!expect(!td_replay_local_output.replay.write_tdengine,
                "td replay q2frame suppresses tdengine writes")) return false;
    if (!expect(!td_replay_local_output.processing.dry_run,
                "td replay q2frame keeps local projection enabled")) return false;

    ConfigV2 live_output_config;
    live_output_config.runtime_mode = RuntimeMode::Live;
    live_output_config.replay.q2frame_path = "/tmp/should_not_activate.jsonl";
    if (!expect(!configure_local_replay_output(live_output_config),
                "live mode cannot activate replay file sink")) return false;

    ConfigV2 replay_config;
    replay_config.tdengine.replay_table = "stock_data";
    replay_config.replay.start_time = "2026-04-29 09:20:00";
    replay_config.replay.end_time = "2026-04-29 09:20:06";
    replay_config.replay.tick_interval_ms = 3000;
    TdReplayQueryBuilder replay_query(replay_config);
    const int64_t aligned = replay_query.align_to_tick_interval(1777425845123);
    if (!expect(aligned == 1777425843000LL, "td replay interval align")) return false;
    const std::string replay_sql = replay_query.build_slice_query(aligned, aligned + 3000);
    if (!expect(replay_sql.find("FROM stock_data") != std::string::npos, "td replay table")) return false;
    if (!expect(replay_sql.find("inst_vol, inst_amt, large_net") != std::string::npos, "td replay derived fields")) return false;
    ConfigV2 replay_v2_config = replay_config;
    replay_v2_config.tdengine.replay_table = "stock_tick_v2";
    TdReplayQueryBuilder replay_v2_query(replay_v2_config);
    const std::string replay_v2_sql = replay_v2_query.build_slice_query(aligned, aligned + 3000);
    if (!expect(replay_v2_sql.find("FROM stock_tick_v2") != std::string::npos, "td replay v2 table")) return false;
    if (!expect(replay_v2_sql.find("px_milli/1000.0 AS lp") != std::string::npos, "td replay v2 price alias")) return false;
    if (!expect(replay_v2_sql.find("inst_amt_yuan AS inst_amt") != std::string::npos, "td replay v2 amount alias")) return false;
    ReplaySliceScheduler scheduler(replay_config);
    if (!expect(scheduler.start(), "replay scheduler start")) return false;
    const ReplaySlice slice1 = scheduler.next();
    const ReplaySlice slice2 = scheduler.next();
    const ReplaySlice slice3 = scheduler.next();
    if (!expect(slice1.valid && slice2.valid && !slice3.valid, "replay scheduler slices")) return false;
    if (!expect(slice2.start_ms - slice1.start_ms == 3000, "replay scheduler interval")) return false;
    if (!expect(slice1.end_ms == slice2.start_ms, "replay slices are contiguous half-open windows")) return false;
    const std::string exact_slice_sql = replay_query.build_slice_query(slice1.start_ms, slice1.end_ms);
    if (!expect(exact_slice_sql.find("ts >= '2026-04-29 09:20:00'") != std::string::npos,
                "td replay query uses Shanghai slice start")) return false;
    if (!expect(exact_slice_sql.find("ts < '2026-04-29 09:20:03'") != std::string::npos,
                "td replay query uses Shanghai slice end")) return false;
    if (!expect(exact_slice_sql.find("ORDER BY ts ASC, symbol ASC") != std::string::npos,
                "td replay rows are ordered for timestamp grouping")) return false;
    std::vector<SourceTickRecord> replay_group_records(4);
    replay_group_records[0].tss = aligned;
    replay_group_records[1].tss = aligned;
    replay_group_records[2].tss = aligned + 1000;
    replay_group_records[3].tss = aligned + 2000;
    if (!expect(replay_timestamp_group_size(replay_group_records) == 2,
                "td replay emits one complete timestamp group")) return false;
    if (!expect(replay_timestamp_group_size({}) == 0,
                "td replay empty timestamp group")) return false;

    ConfigV2 morning_replay_config = replay_config;
    morning_replay_config.replay.start_time = "2026-04-29 09:15:00";
    morning_replay_config.replay.end_time = "2026-04-29 09:40:00";
    ReplaySliceScheduler morning_scheduler(morning_replay_config);
    if (!expect(morning_scheduler.start(), "morning replay scheduler start")) return false;
    int morning_slice_count = 0;
    int64_t previous_end_ms = 0;
    while (true) {
        const ReplaySlice slice = morning_scheduler.next();
        if (!slice.valid) {
            break;
        }
        if (!expect(slice.start_ms % 3000 == 0 && slice.end_ms % 3000 == 0,
                    "morning replay slices stay on 00/03/06 boundaries")) return false;
        if (previous_end_ms != 0 &&
            !expect(slice.start_ms == previous_end_ms, "morning replay has no overlap or gap")) return false;
        previous_end_ms = slice.end_ms;
        ++morning_slice_count;
    }
    if (!expect(morning_slice_count == 500, "09:15-09:40 has 500 three-second slices")) return false;
    if (!expect(previous_end_ms == ReplaySliceScheduler::parse_local_time_ms("2026-04-29 09:40:00"),
                "morning replay ends exactly at 09:40:00")) return false;

    ConfigV2 unaligned_replay_config = replay_config;
    unaligned_replay_config.replay.start_time = "2026-04-29 09:20:01";
    unaligned_replay_config.replay.end_time = "2026-04-29 09:20:07";
    ReplaySliceScheduler unaligned_scheduler(unaligned_replay_config);
    if (!expect(!unaligned_scheduler.start(), "unaligned replay range fails closed")) return false;

    SourceTickRecord source;
    source.tss = make_local_ts_ms(2026, 4, 29, 9, 24, 5);
    source.symbol = "000070";
    source.market = "sz";
    source.lp = 12.345;
    source.o = 12.010;
    source.h = 12.700;
    source.l = 11.980;
    source.lc = 11.220;
    source.a = 123456789.4;
    source.v = 9876543;
    source.ap[0] = 12.350;
    source.ap[1] = 12.350;
    source.bp[0] = 12.350;
    source.bp[1] = 12.350;
    source.av[0] = 3000;
    source.av[1] = 5000;
    source.bv[0] = 7000;
    source.bv[1] = 11000;
    source.inst_vol = 1000;
    source.inst_amt = 12345;
    source.large_net = -12345;
    source.limit_up = 12.340;
    source.limit_down = 10.100;
    source.limit_band_bp = 500;
    source.is_st = true;

    const std::vector<std::string> replay_fields{
        "ts", "symbol", "lp", "o", "h", "l", "lc", "a", "v",
        "ap1", "ap2", "bp1", "bp2", "av1", "av2", "bv1", "bv2",
        "inst_vol", "inst_amt", "large_net", "limit_up", "limit_down", "limit_band_bp", "is_st"
    };
    const TdReplayRowPlan replay_plan = TdReplayRowConverter::build_plan(replay_fields);
    const std::vector<TdReplayCell> replay_cells{
        TdReplayCell::i64(source.tss), TdReplayCell::string("000070.SZ"),
        TdReplayCell::f64(source.lp), TdReplayCell::f64(source.o), TdReplayCell::f64(source.h),
        TdReplayCell::f64(source.l), TdReplayCell::f64(source.lc), TdReplayCell::f64(source.a),
        TdReplayCell::i64(source.v), TdReplayCell::f64(source.ap[0]), TdReplayCell::f64(source.ap[1]),
        TdReplayCell::f64(source.bp[0]), TdReplayCell::f64(source.bp[1]),
        TdReplayCell::i64(source.av[0]), TdReplayCell::i64(source.av[1]),
        TdReplayCell::i64(source.bv[0]), TdReplayCell::i64(source.bv[1]),
        TdReplayCell::i64(source.inst_vol), TdReplayCell::i64(source.inst_amt), TdReplayCell::i64(source.large_net),
        TdReplayCell::f64(source.limit_up), TdReplayCell::f64(source.limit_down),
        TdReplayCell::i64(source.limit_band_bp), TdReplayCell::i64(1)
    };
    SourceTickRecord replay_record;
    if (!expect(TdReplayRowConverter::convert(replay_plan, replay_cells, replay_record), "td replay row converts")) return false;
    if (!expect(replay_record.symbol == "000070" && replay_record.market == "sz", "td replay symbol normalized")) return false;
    if (!expect(replay_record.inst_amt == source.inst_amt && replay_record.large_net == source.large_net, "td replay derived fields")) return false;
    if (!expect(replay_record.limit_band_bp == 500 && replay_record.is_st, "td replay limit metadata")) return false;
#if defined(T1_V2_ENABLE_TDENGINE)
    if (!expect(TdengineRowAdapter::enabled(), "tdengine adapter enabled")) return false;
#else
    if (!expect(!TdengineRowAdapter::enabled(), "tdengine adapter disabled")) return false;
#endif

    RawTick converted;
    if (!expect(RawTickConverter::from_source_record(source, converted), "source record converts")) return false;
    if (!expect(std::string(converted.symbol) == "000070", "source symbol")) return false;
    if (!expect(std::string(converted.market) == "sz", "source market")) return false;
    if (!expect(converted.px_milli == 12345, "source price milli")) return false;
    if (!expect(converted.pc_milli == 11220, "source previous close milli")) return false;
    if (!expect(converted.amt_yuan == 123456789, "source amount yuan")) return false;
    if (!expect(converted.vol_units == 9876543, "source volume shares")) return false;
    if (!expect(converted.ap_milli[1] == 12350 && converted.bv[1] == 11000, "source book levels")) return false;
    if (!expect(converted.inst_amt_yuan == 12345 && converted.large_net_yuan == -12345, "source derived fields")) return false;
    if (!expect(converted.limit_up_milli == 12340 && converted.limit_down_milli == 10100, "source limit prices")) return false;
    if (!expect(converted.limit_band_bp == 500 && converted.is_st, "source limit metadata")) return false;
    SourceTickRecord sh_index_source = source;
    sh_index_source.symbol = "000001";
    sh_index_source.market = "sh";
    sh_index_source.lp = 4179.952;
    RawTick rejected_index_tick;
    if (!expect(!RawTickConverter::from_source_record(sh_index_source, rejected_index_tick), "sh index rejected from stock tick path")) return false;
    SourceTickRecord sz_index_source = source;
    sz_index_source.symbol = "000170";
    sz_index_source.market = "sz";
    sz_index_source.lp = 6349.69;
    RawTick rejected_sz_index_tick;
    if (!expect(!RawTickConverter::from_source_record(sz_index_source, rejected_sz_index_tick), "sz index-like price rejected from stock tick path")) return false;

    SourceTickRecord bad_source = source;
    bad_source.symbol = "BAD";
    std::vector<SourceTickRecord> source_records{source, bad_source, sh_index_source, sz_index_source};
    TickBatch source_batch;
    const SourceTickBatchBuildStats build_stats = SourceTickBatchBuilder::build(
        source_records,
        RuntimeMode::Replay,
        0,
        7,
        source_batch
    );
    if (!expect(build_stats.input_count == 4, "source batch input count")) return false;
    if (!expect(build_stats.accepted_count == 1 && build_stats.rejected_count == 3, "source batch filtering")) return false;
    if (!expect(source_batch.mode == RuntimeMode::Replay, "source batch mode")) return false;
    if (!expect(source_batch.logical_ts_ms == source.tss, "source batch logical ts fallback")) return false;
    if (!expect(source_batch.ticks.size() == 1 && std::string(source_batch.ticks[0].symbol) == "000070", "source batch tick")) return false;

#if defined(T1_V2_ENABLE_PROTOBUF)
    dataservice::DataBatch proto_batch;
    dataservice::DataRecord* proto_record = proto_batch.add_records();
    proto_record->set_tss(source.tss);
    proto_record->set_symbol(source.symbol);
    proto_record->set_exchange("SZ");
    proto_record->set_market("sz");
    proto_record->set_lp(static_cast<float>(source.lp));
    proto_record->set_o(static_cast<float>(source.o));
    proto_record->set_h(static_cast<float>(source.h));
    proto_record->set_l(static_cast<float>(source.l));
    proto_record->set_lc(static_cast<float>(source.lc));
    proto_record->set_a(static_cast<float>(source.a));
    proto_record->set_v(source.v);
    proto_record->set_ap1(static_cast<float>(source.ap[0]));
    proto_record->set_ap2(static_cast<float>(source.ap[1]));
    proto_record->set_bp1(static_cast<float>(source.bp[0]));
    proto_record->set_bp2(static_cast<float>(source.bp[1]));
    proto_record->set_av1(source.av[0]);
    proto_record->set_av2(source.av[1]);
    proto_record->set_bv1(source.bv[0]);
    proto_record->set_bv2(source.bv[1]);
    std::string proto_batch_bytes;
    if (!expect(proto_batch.SerializeToString(&proto_batch_bytes), "protobuf batch serialize")) return false;

    dataservice::DataRequest proto_request;
    proto_request.set_compression(dataservice::NONE);
    proto_request.set_compressed_data(proto_batch_bytes);
    std::string proto_request_bytes;
    if (!expect(proto_request.SerializeToString(&proto_request_bytes), "protobuf request serialize")) return false;

    std::vector<SourceTickRecord> decoded_records;
    const ProtobufDecodeResult proto_decode = ProtobufTickDecoder::decode_data_request_payload(
        proto_request_bytes.data(),
        proto_request_bytes.size(),
        decoded_records
    );
    if (!expect(proto_decode.ok, "protobuf request decode")) return false;
    if (!expect(decoded_records.size() == 1 && decoded_records[0].symbol == "000070", "protobuf decoded symbol")) return false;

    const std::string live_header =
        "{\"record_count\":1,\"compression\":\"NONE\",\"original_size\":"
        + std::to_string(proto_request_bytes.size())
        + ",\"compressed_size\":" + std::to_string(proto_request_bytes.size())
        + ",\"timestamp\":1777425845000,\"batch_id\":\"live1\",\"proto_version\":\"v1\"}";
    std::vector<char> live_body;
    const uint32_t live_header_len = static_cast<uint32_t>(live_header.size());
    live_body.push_back(static_cast<char>((live_header_len >> 24) & 0xFF));
    live_body.push_back(static_cast<char>((live_header_len >> 16) & 0xFF));
    live_body.push_back(static_cast<char>((live_header_len >> 8) & 0xFF));
    live_body.push_back(static_cast<char>(live_header_len & 0xFF));
    live_body.insert(live_body.end(), live_header.begin(), live_header.end());
    live_body.insert(live_body.end(), proto_request_bytes.begin(), proto_request_bytes.end());
    TickBatch live_decoded_batch;
    const RabbitMqBatchDecodeResult live_decoded = RabbitMqBatchDecoder::decode_body(live_body, 99, live_decoded_batch);
    if (!expect(live_decoded.ok, "rabbit live body decode")) return false;
    if (!expect(live_decoded_batch.seq_no == 99 && live_decoded_batch.ticks.size() == 1, "rabbit live batch output")) return false;

    dataservice::DataBatch skipped_proto_batch;
    dataservice::DataRecord* skipped_record = skipped_proto_batch.add_records();
    skipped_record->set_tss(sh_index_source.tss);
    skipped_record->set_symbol(sh_index_source.symbol);
    skipped_record->set_exchange("SH");
    skipped_record->set_market("sh");
    skipped_record->set_lp(static_cast<float>(sh_index_source.lp));
    skipped_record->set_lc(static_cast<float>(sh_index_source.lc));
    std::string skipped_batch_bytes;
    if (!expect(skipped_proto_batch.SerializeToString(&skipped_batch_bytes), "skipped protobuf batch serialize")) return false;
    dataservice::DataRequest skipped_request;
    skipped_request.set_compression(dataservice::NONE);
    skipped_request.set_compressed_data(skipped_batch_bytes);
    std::string skipped_request_bytes;
    if (!expect(skipped_request.SerializeToString(&skipped_request_bytes), "skipped protobuf request serialize")) return false;
    const std::string skipped_header =
        "{\"record_count\":1,\"compression\":\"NONE\",\"original_size\":"
        + std::to_string(skipped_request_bytes.size())
        + ",\"compressed_size\":" + std::to_string(skipped_request_bytes.size())
        + ",\"timestamp\":1777425845000,\"batch_id\":\"skip1\",\"proto_version\":\"v1\"}";
    std::vector<char> skipped_body;
    const uint32_t skipped_header_len = static_cast<uint32_t>(skipped_header.size());
    skipped_body.push_back(static_cast<char>((skipped_header_len >> 24) & 0xFF));
    skipped_body.push_back(static_cast<char>((skipped_header_len >> 16) & 0xFF));
    skipped_body.push_back(static_cast<char>((skipped_header_len >> 8) & 0xFF));
    skipped_body.push_back(static_cast<char>(skipped_header_len & 0xFF));
    skipped_body.insert(skipped_body.end(), skipped_header.begin(), skipped_header.end());
    skipped_body.insert(skipped_body.end(), skipped_request_bytes.begin(), skipped_request_bytes.end());
    TickBatch skipped_decoded_batch;
    const RabbitMqBatchDecodeResult skipped_decoded =
        RabbitMqBatchDecoder::decode_body(skipped_body, 100, skipped_decoded_batch);
    if (!expect(!skipped_decoded.ok, "rabbit skipped body has no accepted ticks")) return false;
    if (!expect(skipped_decoded.build_stats.input_count == 1 &&
                skipped_decoded.build_stats.accepted_count == 0 &&
                skipped_decoded.build_stats.rejected_count == 1,
                "rabbit skipped body keeps rejection stats")) return false;
    if (!expect(skipped_decoded.error.find("rejected=1") != std::string::npos,
                "rabbit skipped body diagnostic")) return false;
#else
    std::vector<SourceTickRecord> decoded_records;
    const ProtobufDecodeResult proto_decode = ProtobufTickDecoder::decode_data_request_payload(
        wire_payload.data(),
        wire_payload.size(),
        decoded_records
    );
    if (!expect(!proto_decode.ok && proto_decode.unsupported, "protobuf unsupported without flag")) return false;
    TickBatch unsupported_live_batch;
    const RabbitMqBatchDecodeResult unsupported_live = RabbitMqBatchDecoder::decode_body(wire_body, 1, unsupported_live_batch);
    if (!expect(!unsupported_live.ok && unsupported_live.unsupported, "rabbit live decode unsupported without protobuf")) return false;
#endif

    ConfigV2 config;
    config.runtime_mode = RuntimeMode::Live;
    config.processing.dry_run = false;
    config.logging.verbose = false;
    config.logging.enable_file_log = false;
    EngineCore engine(config);
    if (!expect(engine.initialize(), "engine initialize")) {
        return false;
    }

    const int64_t ts_0920 = make_local_ts_ms(2026, 4, 29, 9, 20, 5);
    const int64_t ts_0921 = make_local_ts_ms(2026, 4, 29, 9, 21, 5);
    const int64_t ts_0922 = make_local_ts_ms(2026, 4, 29, 9, 22, 5);

    TickBatch batch1;
    batch1.logical_ts_ms = ts_0920;
    batch1.ticks.push_back(make_tick("000001", ts_0920, 10000, 100000000));
    EngineProcessStats stats1 = engine.on_batch(batch1);
    if (!expect(stats1.phase == MarketPhase::Auction, "0920 phase auction")) return false;

    TickBatch batch2;
    batch2.logical_ts_ms = ts_0921;
    batch2.ticks.push_back(make_tick("000001", ts_0921, 10100, 130000000));
    engine.on_batch(batch2);

    TickBatch batch3;
    batch3.logical_ts_ms = ts_0922;
    batch3.ticks.push_back(make_tick("000001", ts_0922, 10200, 160000000));
    EngineProcessStats stats3 = engine.on_batch(batch3);

    const QuoteState* found = nullptr;
    engine.quote_store().for_each_active([&](const QuoteState& state) {
        if (std::string(state.symbol) == "000001") {
            found = &state;
        }
    });
    if (!expect(found != nullptr, "quote state exists")) return false;
    const QuoteState& state = *found;
    if (!expect(state.auction.a20_px_milli == 10000, "a20 captured")) return false;
    if (!expect(state.auction.match_amt_yuan == 816000, "auction matched amount uses l1 min lots")) return false;
    if (!expect(state.auction.rest_bid_amt_yuan == 3060000, "auction rest bid uses l1 price and l2 quantity")) return false;
    if (!expect(state.auction.rest_ask_amt_yuan == 2040000, "auction rest ask uses l1 price and l2 quantity")) return false;
    if (!expect(state.inst_amt_yuan == 30000000, "auction inst amount diff")) return false;
    if (!expect(state.large_net_yuan == 0, "auction large net stays zero")) return false;
    if (!expect(state.spd1m_bp == 99, "speed_1m minute bucket")) return false;
    if (!expect(state.amt2m_yuan == 60000000, "amount_2m cumulative difference")) return false;
    if (!expect(state.amt5m_yuan == 60000000, "amount_5m fallback inside window")) return false;
    if (!expect(state.vec3m_bp == 0, "speed_3m remains zero without exact 3m slot")) return false;

    // A replay clock event must be sufficient to cross the delayed 09:25
    // barrier when no historical tick exists in the settling interval.  It
    // advances only trigger/phase state: no RawTick or market totals are
    // fabricated and the freeze remains one-shot.
    EngineCore clock_engine(config);
    if (!expect(clock_engine.initialize(), "clock engine initialize")) return false;
    const int64_t ts_0925_05 = make_local_ts_ms(2026, 4, 29, 9, 25, 5);
    const int64_t ts_0925_06 = make_local_ts_ms(2026, 4, 29, 9, 25, 6);
    const int64_t ts_0925_07 = make_local_ts_ms(2026, 4, 29, 9, 25, 7);
    TickBatch last_auction_tick;
    last_auction_tick.logical_ts_ms = ts_0925_05;
    RawTick last_auction_raw = make_tick("000006", ts_0925_05, 10200, 170000000, "sz");
    last_auction_tick.ticks.push_back(last_auction_raw);
    const EngineProcessStats last_tick_stats = clock_engine.on_batch(last_auction_tick);
    const QuoteState* clock_state_before = nullptr;
    clock_engine.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "000006") {
            clock_state_before = &quote;
        }
    });
    if (!expect(clock_state_before != nullptr, "clock quote state exists")) return false;
    const int64_t amount_before_clock = clock_state_before->amt_yuan;
    const int64_t volume_before_clock = clock_state_before->vol_units;
    const EngineProcessStats clock_stats = clock_engine.on_clock(ts_0925_06);
    if (!expect(clock_stats.tick_count == 0, "clock event has no ticks")) return false;
    if (!expect(clock_stats.snapshot_trigger.emit_a25, "clock event emits delayed a25")) return false;
    if (!expect(clock_engine.quote_store().size() == 1, "clock event creates no symbol")) return false;
    const QuoteState* clock_state_after = nullptr;
    clock_engine.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "000006") {
            clock_state_after = &quote;
        }
    });
    if (!expect(clock_state_after != nullptr &&
                clock_state_after->amt_yuan == amount_before_clock &&
                clock_state_after->vol_units == volume_before_clock,
                "clock event does not mutate market totals")) return false;
    const EngineProcessStats repeat_clock_stats = clock_engine.on_clock(ts_0925_07);
    if (!expect(!repeat_clock_stats.snapshot_trigger.emit_a25, "clock a25 emits only once")) return false;
    if (!expect(last_tick_stats.tick_count == 1, "last auction tick count preserved")) return false;
    clock_engine.shutdown();

    // The clock path must reuse the normal writers without emitting a stock
    // tick insert.  This is a structural guard against reintroducing a fake
    // empty TickBatch control record.
    ConfigV2 clock_pipeline_config = config;
    clock_pipeline_config.processing.dry_run = false;
    RuntimePipeline clock_pipeline(clock_pipeline_config);
    if (!expect(clock_pipeline.initialize(), "clock pipeline initialize")) return false;
    const RuntimePipelineResult clock_batch_result = clock_pipeline.process_batch(last_auction_tick, {});
    if (!expect(clock_batch_result.engine_stats.tick_count == 1, "clock pipeline seed tick")) return false;
    const RuntimePipelineResult clock_pipeline_result = clock_pipeline.process_clock(ts_0925_06, 2);
    if (!expect(clock_pipeline_result.engine_stats.tick_count == 0 &&
                clock_pipeline_result.engine_stats.snapshot_trigger.emit_a25,
                "clock pipeline trigger")) return false;
    clock_pipeline_config.replay.write_tdengine = true;
    TDengineV2Writer clock_td_writer(clock_pipeline_config);
    if (!expect(clock_td_writer.initialize(), "clock td writer initialize")) return false;
    const auto clock_td_statements = clock_td_writer.build_clock_statements(
        clock_pipeline.engine().quote_store(),
        clock_pipeline_result.engine_stats.snapshot_trigger,
        ts_0925_06
    );
    bool clock_has_stock_insert = false;
    for (const std::string& statement : clock_td_statements) {
        if (statement.find("stock_tick_v2") != std::string::npos &&
            statement.find("CREATE STABLE") == std::string::npos) {
            clock_has_stock_insert = true;
        }
    }
    if (!expect(!clock_has_stock_insert, "clock path has no stock tick insert")) return false;
    clock_td_writer.shutdown();

    const int64_t ts_0932_10 = make_local_ts_ms(2026, 4, 29, 9, 32, 10);
    const RuntimePipelineResult cutoff_result = clock_pipeline.process_clock(ts_0932_10, 3);
    if (!expect(cutoff_result.engine_stats.snapshot_trigger.emit_opening_cutoff,
                "opening cutoff trigger emits once at 09:32:10")) return false;
    RedisV2Writer cutoff_writer(clock_pipeline_config);
    if (!expect(cutoff_writer.initialize(), "cutoff writer initialize")) return false;
    const auto cutoff_commands = cutoff_writer.build_opening_cutoff_commands(
        clock_pipeline.engine().quote_store(),
        cutoff_result.engine_stats.snapshot_trigger,
        ts_0932_10
    );
    bool has_cutoff_publish = false;
    for (const RedisCommand& command : cutoff_commands) {
        if (command.type == RedisCommandType::AtomicCutoffPublish &&
            command.key.find("market:opening:cutoff:20260429:0932:payload") == 0 &&
            command.secondary_key.find("market:opening:cutoff:20260429:0932:meta") == 0 &&
            command.value.find("\"rows\":[") != std::string::npos) {
            has_cutoff_publish = true;
            bool published = false;
            bool has_sha = false;
            bool has_exact_cutoff = false;
            for (const auto& field : command.fields) {
                published = published || (field.first == "published" && field.second == "1");
                has_sha = has_sha || (field.first == "payload_sha256" && field.second.size() == 64);
                has_exact_cutoff = has_exact_cutoff ||
                    (field.first == "cutoff_ts_ms" && field.second == std::to_string(ts_0932_10));
            }
            RedisArgvCommand atomic_argv;
            if (!expect(RedisCommandFormatter::format(command, atomic_argv), "atomic cutoff publish formats")) return false;
            if (!expect(atomic_argv.argv.size() == 7 + command.fields.size() * 2 &&
                        atomic_argv.argv[0] == "EVAL" && atomic_argv.argv[2] == "2" &&
                        atomic_argv.argv[3] == command.key && atomic_argv.argv[4] == command.secondary_key,
                        "atomic cutoff publish uses one EVAL with both keys")) return false;
            if (!expect(atomic_argv.argv[1].find("redis.error_reply") != std::string::npos,
                        "atomic cutoff publish rejects partial existing keys")) return false;
            if (!expect(published && has_sha && has_exact_cutoff,
                        "cutoff metadata has exact cutoff and sha")) return false;
        }
    }
    if (!expect(cutoff_commands.size() == 1 && has_cutoff_publish,
                "cutoff payload and metadata publish as one command")) return false;
    const RuntimePipelineResult repeat_cutoff_result = clock_pipeline.process_clock(
        make_local_ts_ms(2026, 4, 29, 9, 32, 11),
        4
    );
    if (!expect(!repeat_cutoff_result.engine_stats.snapshot_trigger.emit_opening_cutoff,
                "opening cutoff trigger emits only once")) return false;
    cutoff_writer.shutdown();
    clock_pipeline.shutdown();

    // MiniQMT auction ticks can have no last trade while level-1 carries the
    // virtual match price.  The existing RawTick contract must still produce
    // anchors and matched amount without using lastClose as a substitute.
    EngineCore qmt_auction_engine(config);
    if (!expect(qmt_auction_engine.initialize(), "qmt auction engine initialize")) return false;
    RawTick qmt_auction_tick = make_tick("000004", ts_0920, 0, 0, "sz");
    qmt_auction_tick.ap_milli[0] = 10000;
    qmt_auction_tick.bp_milli[0] = 10000;
    qmt_auction_tick.ap_milli[1] = 10000;
    qmt_auction_tick.bp_milli[1] = 10000;
    qmt_auction_tick.av[0] = 100;
    qmt_auction_tick.bv[0] = 200;
    qmt_auction_tick.av[1] = 50;
    qmt_auction_tick.bv[1] = 25;
    qmt_auction_tick.ap_milli[1] = 0;
    qmt_auction_tick.bp_milli[1] = 0;
    TickBatch qmt_auction_batch;
    qmt_auction_batch.logical_ts_ms = ts_0920;
    qmt_auction_batch.ticks.push_back(qmt_auction_tick);
    qmt_auction_engine.on_batch(qmt_auction_batch);
    const QuoteState* qmt_auction_state = nullptr;
    qmt_auction_engine.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "000004") {
            qmt_auction_state = &quote;
        }
    });
    if (!expect(qmt_auction_state != nullptr, "qmt auction quote state exists")) return false;
    if (!expect(qmt_auction_state->auction.a20_px_milli == 10000, "qmt auction a20 uses l1 virtual price")) return false;
    if (!expect(qmt_auction_state->auction.match_amt_yuan == 100000, "qmt auction match uses l1 virtual price")) return false;
    if (!expect(qmt_auction_state->auction.rest_bid_amt_yuan == 25000, "qmt auction rest bid uses l1 price with l2 quantity")) return false;
    if (!expect(qmt_auction_state->auction.rest_ask_amt_yuan == 50000, "qmt auction rest ask uses l1 price with l2 quantity")) return false;

    // Before the auction is fixed, level-1 is authoritative even when
    // lastPrice is populated with a different virtual price.
    AuctionCalculator auction_calculator;
    // TD market_data1.stock_tick_v2, 2026-11-12: SELECT ts,px_milli,pc_milli,
    // amt_yuan,bp1_milli,ap1_milli,bv1,bv2,av1,av2 WHERE symbol='600103'
    // AND ts >= '2026-11-12 09:24:59' AND ts < '2026-11-12 09:25:03'.
    RawTick real_pre{}; set_symbol(real_pre, "600103");
    real_pre.ts_ms = make_local_ts_ms(2026, 11, 12, 9, 24, 59); real_pre.px_milli = 3610;
    real_pre.pc_milli = 3280; real_pre.bp_milli[0] = 3610; real_pre.ap_milli[0] = 3610;
    real_pre.bv[0] = 107798; real_pre.bv[1] = 2245798; real_pre.av[0] = 107798; real_pre.av[1] = 0;
    QuoteState real_full_state;
    auction_calculator.apply_tick(real_full_state, real_pre, MarketPhase::Auction, true, false, true);
    if (!expect(real_full_state.auction.full_cutoffs[2].rest_bid_amt_yuan == 361LL * 2245798,
                "real pre-match full cutoff uses level-2 volume")) return false;
    RawTick real_final{}; set_symbol(real_final, "600103");
    real_final.ts_ms = make_local_ts_ms(2026, 11, 12, 9, 25, 2); real_final.px_milli = 3610; real_final.amt_yuan = 39077900;
    real_final.pc_milli = 3280; real_final.bp_milli[0] = 3610; real_final.ap_milli[0] = 0;
    real_final.bv[0] = 2246022; real_final.bv[1] = 3758; real_final.av[0] = 0; real_final.av[1] = 0;
    auction_calculator.apply_tick(real_full_state, real_final, MarketPhase::Auction, true, false, true);
    if (!expect(real_full_state.auction.full_cutoffs[2].rest_bid_amt_yuan == 810813942,
                "real final full cutoff uses level-1 volume")) return false;
    if (!expect(real_full_state.auction.rest_bid_amt_yuan == 1356638,
                "legacy rest bid remains level-2 volume")) return false;
    RawTick valid_last_price = make_tick("000005", ts_0920, 9900, 0, "sz");
    valid_last_price.ap_milli[0] = 10000;
    valid_last_price.bp_milli[0] = 10000;
    valid_last_price.ap_milli[1] = 10000;
    valid_last_price.bp_milli[1] = 10000;
    QuoteState valid_last_state;
    if (!expect(auction_calculator.apply_tick(valid_last_state, valid_last_price, MarketPhase::Auction), "valid last price auction update")) return false;
    if (!expect(valid_last_state.auction.a20_px_milli == 10000, "pre-auction last price does not override level-1")) return false;
    if (!expect(valid_last_state.auction.match_amt_yuan == 800000, "pre-auction match amount uses level-1 price")) return false;

    RawTick level1_price_with_empty_level2 = make_tick("000010", ts_0920, 9900, 0, "sz");
    level1_price_with_empty_level2.bp_milli[0] = 10000;
    level1_price_with_empty_level2.ap_milli[0] = 10000;
    level1_price_with_empty_level2.bp_milli[1] = 0;
    level1_price_with_empty_level2.ap_milli[1] = 0;
    level1_price_with_empty_level2.bv[1] = 3000;
    level1_price_with_empty_level2.av[1] = 2000;
    QuoteState level1_price_state;
    if (!expect(auction_calculator.apply_tick(level1_price_state, level1_price_with_empty_level2, MarketPhase::Auction), "level-1 auction price update")) return false;
    if (!expect(level1_price_state.auction.rest_bid_amt_yuan == 3000000, "level-2 bid quantity is valued at level-1 price")) return false;
    if (!expect(level1_price_state.auction.rest_ask_amt_yuan == 2000000, "level-2 ask quantity is valued at level-1 price")) return false;

    // Once the auction is fixed, lastPrice is allowed to represent the
    // actual matched price for the 09:25 anchor.
    const int64_t ts_0925_boundary = make_local_ts_ms(2026, 4, 29, 9, 25, 0);
    RawTick post_auction_last_price = make_tick("000011", ts_0925_boundary, 9900, 0, "sz");
    post_auction_last_price.bp_milli[0] = 10000;
    post_auction_last_price.ap_milli[0] = 10000;
    QuoteState post_auction_state;
    if (!expect(auction_calculator.apply_tick(post_auction_state, post_auction_last_price, MarketPhase::Auction), "post-auction last price update")) return false;
    if (!expect(post_auction_state.auction.a25_px_milli == 9900, "post-auction last price is accepted")) return false;

    QuoteState tagged_price_state;
    tagged_price_state.px_milli = 13000;
    tagged_price_state.pc_milli = 10000;
    tagged_price_state.auction.a20_px_milli = 10000;
    tagged_price_state.auction.a24_px_milli = 11000;
    tagged_price_state.auction.a25_px_milli = 12000;
    const AuctionSnapshotPriceFact tagged_0920 = auction_snapshot_price_fact(tagged_price_state, "0920");
    const AuctionSnapshotPriceFact tagged_0924 = auction_snapshot_price_fact(tagged_price_state, "0924");
    const AuctionSnapshotPriceFact tagged_0925 = auction_snapshot_price_fact(tagged_price_state, "0925");
    if (!expect(tagged_0920.available && tagged_0920.price_milli == 10000 &&
                std::string(tagged_0920.source) == "anchor_0920",
                "0920 price uses its own anchor")) return false;
    if (!expect(tagged_0924.available && tagged_0924.price_milli == 11000 &&
                std::string(tagged_0924.source) == "anchor_0924",
                "0924 price uses its own anchor")) return false;
    if (!expect(tagged_0925.available && tagged_0925.price_milli == 12000 &&
                std::string(tagged_0925.source) == "anchor_0925",
                "0925 price uses its own anchor")) return false;
    tagged_price_state.auction.a24_px_milli = 0;
    const AuctionSnapshotPriceFact missing_0924 = auction_snapshot_price_fact(tagged_price_state, "0924");
    if (!expect(!missing_0924.available && missing_0924.price_milli == 0 &&
                std::string(missing_0924.source) == "unavailable",
                "missing formal anchor does not fall back to another price")) return false;
    if (!expect(!auction_change_fact(tagged_price_state, missing_0924).available,
                "missing anchor change is unavailable")) return false;
    tagged_price_state.pc_milli = 0;
    const AuctionSnapshotPriceFact price_without_previous_close = auction_snapshot_price_fact(tagged_price_state, "0925");
    if (!expect(price_without_previous_close.available && price_without_previous_close.price_milli == 12000,
                "missing previous close does not erase auction price")) return false;
    if (!expect(!auction_change_fact(tagged_price_state, price_without_previous_close).available,
                "missing previous close makes change unavailable only")) return false;

    RawTick mismatched_book = valid_last_price;
    mismatched_book.symbol[5] = '6';
    mismatched_book.px_milli = 0;
    mismatched_book.ap_milli[0] = 10000;
    mismatched_book.bp_milli[0] = 10100;
    QuoteState mismatched_book_state;
    auction_calculator.apply_tick(mismatched_book_state, mismatched_book, MarketPhase::Auction);
    if (!expect(mismatched_book_state.auction.a20_px_milli == 0 && mismatched_book_state.auction.match_amt_yuan == 0, "mismatched book remains invalid")) return false;

    RawTick missing_book = valid_last_price;
    missing_book.symbol[5] = '7';
    missing_book.px_milli = 0;
    missing_book.ap_milli[0] = 0;
    missing_book.bp_milli[0] = 0;
    QuoteState missing_book_state;
    auction_calculator.apply_tick(missing_book_state, missing_book, MarketPhase::Auction);
    if (!expect(missing_book_state.auction.a20_px_milli == 0 && missing_book_state.auction.match_amt_yuan == 0, "missing book remains invalid")) return false;

    RawTick continuous_book = valid_last_price;
    continuous_book.symbol[5] = '8';
    continuous_book.px_milli = 0;
    continuous_book.ap_milli[0] = 10000;
    continuous_book.bp_milli[0] = 10000;
    QuoteState continuous_book_state;
    if (!expect(!auction_calculator.apply_tick(continuous_book_state, continuous_book, MarketPhase::Intraday), "continuous book rejects auction fallback")) return false;
    if (!expect(continuous_book_state.auction.a20_px_milli == 0 && continuous_book_state.auction.match_amt_yuan == 0, "continuous book remains untouched")) return false;

    EngineCore phase_refresh_engine(config);
    if (!expect(phase_refresh_engine.initialize(), "phase refresh engine initialize")) return false;
    TickBatch phase_batch_auction;
    phase_batch_auction.logical_ts_ms = ts_0920;
    phase_batch_auction.ticks.push_back(make_tick("000002", ts_0920, 10000, 100000000));
    phase_refresh_engine.on_batch(phase_batch_auction);
    const int64_t ts_0930_phase = make_local_ts_ms(2026, 4, 29, 9, 30, 1);
    TickBatch phase_batch_intraday;
    phase_batch_intraday.logical_ts_ms = ts_0930_phase;
    phase_batch_intraday.ticks.push_back(make_tick("000003", ts_0930_phase, 10100, 110000000));
    phase_refresh_engine.on_batch(phase_batch_intraday);
    bool refreshed_phase = false;
    phase_refresh_engine.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "000002" &&
            quote.phase == MarketPhase::Intraday &&
            (quote.dirty_mask & DIRTY_RUNTIME) != 0) {
            refreshed_phase = true;
        }
    });
    if (!expect(refreshed_phase, "phase transition refreshes untouched quotes")) return false;

    EngineCore cross_day_engine(config);
    if (!expect(cross_day_engine.initialize(), "cross day engine initialize")) return false;
    const int64_t ts_1459 = make_local_ts_ms(2026, 4, 29, 14, 59, 0);
    const int64_t ts_1500 = make_local_ts_ms(2026, 4, 29, 15, 0, 0);
    const int64_t ts_0931_next = make_local_ts_ms(2026, 4, 30, 9, 31, 0);
    const int64_t ts_0934_next = make_local_ts_ms(2026, 4, 30, 9, 34, 0);
    TickBatch cross_day_batch1;
    cross_day_batch1.logical_ts_ms = ts_1459;
    cross_day_batch1.ticks.push_back(make_tick("000004", ts_1459, 10000, 100000000));
    cross_day_engine.on_batch(cross_day_batch1);
    TickBatch cross_day_batch2;
    cross_day_batch2.logical_ts_ms = ts_1500;
    cross_day_batch2.ticks.push_back(make_tick("000004", ts_1500, 10100, 120000000));
    cross_day_engine.on_batch(cross_day_batch2);
    TickBatch cross_day_batch3;
    cross_day_batch3.logical_ts_ms = ts_0931_next;
    cross_day_batch3.ticks.push_back(make_tick("000004", ts_0931_next, 11000, 150000000));
    cross_day_engine.on_batch(cross_day_batch3);
    TickBatch cross_day_batch4;
    cross_day_batch4.logical_ts_ms = ts_0934_next;
    cross_day_batch4.ticks.push_back(make_tick("000004", ts_0934_next, 11300, 180000000));
    cross_day_engine.on_batch(cross_day_batch4);
    bool cross_day_ok = false;
    cross_day_engine.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "000004" &&
            quote.vec3m_bp == 272 &&
            quote.spd1m_bp == 0) {
            cross_day_ok = true;
        }
    });
    if (!expect(cross_day_ok, "cross day minute ring keeps absolute minute history")) return false;

    // Cross-day regression: this intentionally bypasses Redis entirely.
    // A continuous Core instance must not carry AuctionState across the
    // trade-date boundary when the target symbol has no next-day auction tick.
    EngineCore cross_day_auction_leak(config);
    if (!expect(cross_day_auction_leak.initialize(), "cross day auction leak initialize")) return false;
    const int64_t ts_prev_auction = make_local_ts_ms(2026, 4, 29, 9, 25, 3);
    const int64_t ts_next_trigger = make_local_ts_ms(2026, 4, 30, 9, 20, 3);
    RawTick previous_day_auction = make_tick("688085", ts_prev_auction, 13190, 181200, "kc");
    previous_day_auction.pc_milli = 13190;
    previous_day_auction.bp_milli[1] = 13290;
    previous_day_auction.bv[1] = 35;
    previous_day_auction.ap_milli[1] = 13380;
    previous_day_auction.av[1] = 26;
    TickBatch previous_day_batch;
    previous_day_batch.logical_ts_ms = ts_prev_auction;
    previous_day_batch.ticks.push_back(previous_day_auction);
    cross_day_auction_leak.on_batch(previous_day_batch);

    // Only another symbol reaches the next-day snapshot trigger.  688085 has
    // no current-day auction tick and cannot refresh its AuctionState.
    TickBatch next_day_trigger_batch;
    next_day_trigger_batch.logical_ts_ms = ts_next_trigger;
    next_day_trigger_batch.ticks.push_back(make_tick("000002", ts_next_trigger, 10000, 100000));
    cross_day_auction_leak.on_batch(next_day_trigger_batch);
    bool stale_auction_reproduced = false;
    cross_day_auction_leak.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "688085") {
            stale_auction_reproduced =
                quote.auction.ts_ms == ts_prev_auction &&
                quote.auction.match_amt_yuan == 181200 &&
                quote.auction.rest_bid_amt_yuan == 46515 &&
                quote.auction.rest_ask_amt_yuan == 34788;
        }
    });
    if (!expect(!stale_auction_reproduced, "cross day auction state is cleared without Redis")) return false;

    // An old-session batch arriving after the transition must not repopulate
    // the cleared state. A same-day zero-valued Auction tick remains a real
    // current-day fact and is not confused with missing state.
    cross_day_auction_leak.on_batch(previous_day_batch);
    bool late_old_day_rejected = false;
    cross_day_auction_leak.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "688085") {
            late_old_day_rejected = quote.auction.ts_ms == 0;
        }
    });
    if (!expect(late_old_day_rejected, "late old-day auction tick cannot repopulate state")) return false;
    const int64_t ts_next_zero = make_local_ts_ms(2026, 4, 30, 9, 21, 0);
    RawTick current_day_zero = make_tick("688085", ts_next_zero, 13190, 0, "kc");
    current_day_zero.pc_milli = 13190;
    current_day_zero.vol_units = 0;
    current_day_zero.bp_milli[0] = 0;
    current_day_zero.ap_milli[0] = 0;
    current_day_zero.bp_milli[1] = 0;
    current_day_zero.ap_milli[1] = 0;
    current_day_zero.bv[0] = 0;
    current_day_zero.av[0] = 0;
    current_day_zero.bv[1] = 0;
    current_day_zero.av[1] = 0;
    TickBatch current_day_zero_batch;
    current_day_zero_batch.logical_ts_ms = ts_next_zero;
    current_day_zero_batch.ticks.push_back(current_day_zero);
    cross_day_auction_leak.on_batch(current_day_zero_batch);
    bool current_day_zero_preserved = false;
    cross_day_auction_leak.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "688085") {
            current_day_zero_preserved =
                quote.auction.ts_ms == ts_next_zero &&
                quote.auction.match_amt_yuan == 0;
        }
    });
    if (!expect(current_day_zero_preserved, "current-day explicit zero auction fact preserved")) return false;

    RedisV2Writer cross_day_q2_writer(config);
    if (!expect(cross_day_q2_writer.initialize(), "cross day q2 writer initialize")) return false;
    QuoteState* cross_day_q2_state = nullptr;
    cross_day_auction_leak.mutable_quote_store().for_each_active([&](QuoteState& quote) {
        if (std::string(quote.symbol) == "688085") {
            cross_day_q2_state = &quote;
        }
    });
    if (!expect(cross_day_q2_state != nullptr, "cross day q2 state exists")) return false;
    cross_day_q2_state->phase = MarketPhase::Intraday;
    cross_day_q2_state->auction_fields_reset_pending = true;
    const RedisFieldList cross_day_q2_fields = cross_day_q2_writer.build_q2_fields(*cross_day_q2_state);
    bool cross_day_q2_auction_fields_cleared = false;
    for (const auto& field : cross_day_q2_fields) {
        if (field.first == "a25" && field.second == "0") {
            cross_day_q2_auction_fields_cleared = true;
        }
    }
    if (!expect(cross_day_q2_auction_fields_cleared, "cross day q2 clears stale auction fields")) return false;

    EngineCore auction25_engine(config);
    if (!expect(auction25_engine.initialize(), "auction25 engine initialize")) return false;
    const int64_t ts_0925 = make_local_ts_ms(2026, 4, 29, 9, 25, 0);
    TickBatch auction25_batch;
    auction25_batch.logical_ts_ms = ts_0925;
    auction25_batch.ticks.push_back(make_tick("688820", ts_0925, 115000, 71536200, "kc"));
    auction25_engine.on_batch(auction25_batch);
    bool has_auction25_amount = false;
    auction25_engine.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "688820") {
            has_auction25_amount = quote.auction.match_amt_yuan == 71536200;
        }
    });
    if (!expect(has_auction25_amount, "0925 auction matched amount uses cumulative amount")) return false;

    TickBatch limit_batch;
    limit_batch.logical_ts_ms = ts_0922;
    limit_batch.ticks.push_back(make_tick("300001", ts_0922, 12000, 50000000));
    RawTick no_buy_seal_limit = make_tick("300002", ts_0922, 12000, 50000000);
    no_buy_seal_limit.bv[1] = 0;
    limit_batch.ticks.push_back(no_buy_seal_limit);
    limit_batch.ticks.push_back(make_tick("000003", ts_0922, 9000, 50000000));
    RawTick no_sell_seal_limit = make_tick("000004", ts_0922, 9000, 50000000);
    no_sell_seal_limit.av[1] = 0;
    limit_batch.ticks.push_back(no_sell_seal_limit);
    RawTick level1_limit_price = make_tick("000007", ts_0922, 10500, 50000000);
    level1_limit_price.bp_milli[0] = 11000;
    level1_limit_price.ap_milli[0] = 11000;
    level1_limit_price.bp_milli[1] = 0;
    level1_limit_price.ap_milli[1] = 0;
    level1_limit_price.bv[1] = 500;
    level1_limit_price.av[1] = 0;
    limit_batch.ticks.push_back(level1_limit_price);
    RawTick st_limit = make_tick("600005", ts_0922, 11000, 50000000, "sh");
    st_limit.is_st = true;
    limit_batch.ticks.push_back(st_limit);
    RawTick bj_limit = make_tick("830001", ts_0922, 13000, 50000000, "bj");
    limit_batch.ticks.push_back(bj_limit);
    RawTick no_limit_day = make_tick("000006", ts_0922, 11000, 50000000, "sz");
    no_limit_day.no_price_limit = true;
    limit_batch.ticks.push_back(no_limit_day);
    engine.on_batch(limit_batch);
    bool has_computed_limit_up = false;
    bool no_buy_seal_is_normal = false;
    bool has_computed_limit_down = false;
    bool no_sell_seal_is_normal = false;
    bool st_limit_up = false;
    bool bj_limit_up = false;
    bool no_limit_is_normal = false;
    bool level1_limit_up = false;
    engine.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "300001") {
            has_computed_limit_up = quote.limit_state == LimitState::Up;
        } else if (std::string(quote.symbol) == "300002") {
            no_buy_seal_is_normal = quote.limit_state == LimitState::Normal;
        } else if (std::string(quote.symbol) == "000003") {
            has_computed_limit_down = quote.limit_state == LimitState::Down;
        } else if (std::string(quote.symbol) == "000004") {
            no_sell_seal_is_normal = quote.limit_state == LimitState::Normal;
        } else if (std::string(quote.symbol) == "600005") {
            st_limit_up = quote.limit_state == LimitState::Up;
        } else if (std::string(quote.symbol) == "830001") {
            bj_limit_up = quote.limit_state == LimitState::Up;
        } else if (std::string(quote.symbol) == "000006") {
            no_limit_is_normal = quote.limit_state == LimitState::Normal;
        } else if (std::string(quote.symbol) == "000007") {
            level1_limit_up = quote.limit_state == LimitState::Up;
        }
    });
    if (!expect(has_computed_limit_up, "limit up requires price and buy seal")) return false;
    if (!expect(no_buy_seal_is_normal, "limit up rejected without buy seal")) return false;
    if (!expect(has_computed_limit_down, "limit down requires price and sell seal")) return false;
    if (!expect(no_sell_seal_is_normal, "limit down rejected without sell seal")) return false;
    if (!expect(st_limit_up, "ST flag does not override the standard 10pct limit band")) return false;
    if (!expect(bj_limit_up, "BJ market uses 30pct limit band")) return false;
    if (!expect(no_limit_is_normal, "no price-limit day never marks limit")) return false;
    if (!expect(level1_limit_up, "auction limit seal uses level-1 price and level-2 quantity")) return false;

    EngineCore stock_market_engine(config);
    if (!expect(stock_market_engine.initialize(), "stock market engine initialize")) return false;
    TickBatch stock_market_batch;
    stock_market_batch.logical_ts_ms = ts_0922;
    stock_market_batch.ticks.push_back(make_tick("000001", ts_0922, 11300, 200000000, "sz"));
    stock_market_engine.on_batch(stock_market_batch);
    bool has_sz_stock = false;
    stock_market_engine.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "000001" && std::string(quote.market) == "sz" && quote.px_milli == 11300) {
            has_sz_stock = true;
        }
    });
    if (!expect(stock_market_engine.quote_store().size() == 1 && has_sz_stock, "sz stock remains in stock path")) return false;

    const int64_t ts_0930 = make_local_ts_ms(2026, 4, 29, 9, 30, 5);
    const int64_t ts_0931 = make_local_ts_ms(2026, 4, 29, 9, 31, 5);
    TickBatch trade_batch1;
    trade_batch1.logical_ts_ms = ts_0930;
    trade_batch1.ticks.push_back(make_tick("000002", ts_0930, 10000, 200000000));
    engine.on_batch(trade_batch1);
    TickBatch trade_batch2;
    trade_batch2.logical_ts_ms = ts_0931;
    trade_batch2.ticks.push_back(make_tick("000002", ts_0931, 10300, 203000000));
    engine.on_batch(trade_batch2);

    const QuoteState* trade_state = nullptr;
    engine.quote_store().for_each_active([&](const QuoteState& quote) {
        if (std::string(quote.symbol) == "000002") {
            trade_state = &quote;
        }
    });
    if (!expect(trade_state != nullptr, "trade quote state exists")) return false;
    if (!expect(trade_state->inst_amt_yuan == 3000000, "trade inst amount diff")) return false;
    if (!expect(trade_state->large_net_yuan == 3000000, "trade large net signed positive")) return false;

    RedisV2Writer redis_writer(config);
    if (!expect(redis_writer.initialize(), "redis writer initialize")) return false;
    // C977: use the last real TD tick per symbol before the 0925 barrier.
    std::ifstream c977_file("t1_v2/tests/fixtures/t1v2/auction_full_2026-11-11.json");
    std::stringstream c977_stream; c977_stream << c977_file.rdbuf();
    const std::string c977_text = c977_stream.str();
    const std::regex c977_object_re(R"(\{[^}]+\})");
    auto c977_number = [](const std::string& object, const char* key) -> int64_t {
        const std::string marker = std::string("\"") + key + "\":";
        const auto pos = object.find(marker);
        return pos == std::string::npos ? -1 : std::stoll(object.substr(pos + marker.size()));
    };
    TickBatch c977_batch; c977_batch.logical_ts_ms = 1794360306000LL;
    for (auto it = std::sregex_iterator(c977_text.begin(), c977_text.end(), c977_object_re);
         it != std::sregex_iterator(); ++it) {
        const std::string object = it->str();
        if (object.find("\"capture_tag\":\"0925\"") == std::string::npos) continue;
        const auto pos = object.find("\"symbol\":\"");
        if (pos == std::string::npos) return false;
        RawTick tick{}; const std::string symbol = object.substr(pos + 10, 6);
        set_symbol(tick, symbol.c_str());
        tick.ts_ms = c977_number(object,"ts"); tick.px_milli = c977_number(object,"px_milli");
        tick.pc_milli = c977_number(object,"pc_milli"); tick.amt_yuan = c977_number(object,"amt_yuan");
        tick.vol_units = c977_number(object,"vol_units"); tick.bp_milli[0] = c977_number(object,"bp1_milli");
        tick.ap_milli[0] = c977_number(object,"ap1_milli"); tick.bv[0] = c977_number(object,"bv1");
        tick.av[0] = c977_number(object,"av1"); tick.bv[1] = c977_number(object,"bv2"); tick.av[1] = c977_number(object,"av2");
        c977_batch.ticks.push_back(tick);
    }
    if (!expect(c977_batch.ticks.size() == 5225, "C977 real TD candidate fixture")) return false;
    EngineCore c977_engine(config); c977_engine.initialize();
    const auto c977_stats = c977_engine.on_batch(c977_batch);
    const auto c977_commands = redis_writer.build_a2_commands(c977_engine.quote_store(), c977_stats.snapshot_trigger, c977_batch.logical_ts_ms);
    std::size_t c977_candidates = 0;
    c977_engine.quote_store().for_each_active([&](const QuoteState& state) {
        if (state.symbol[0] && state.auction.ts_ms > 0 && (state.auction.match_amt_yuan > 0 || state.auction.rest_bid_amt_yuan > 0 || state.auction.rest_ask_amt_yuan > 0)) ++c977_candidates;
    });
    bool c977_full_ok = false, c977_expiry_ok = false;
    std::vector<RedisCommand> c977_full_projections;
    const std::regex c977_symbol_re(R"re("[0-9]{6}":\{)re");
    for (const auto& cmd : c977_commands) {
        if (cmd.key != "core:auction:full:2026-11-11:0925") continue;
        const auto count = std::distance(std::sregex_iterator(cmd.value.begin(), cmd.value.end(), c977_symbol_re), std::sregex_iterator());
        c977_full_ok = cmd.type == RedisCommandType::SetString && static_cast<std::size_t>(count) == c977_candidates && count > 0;
        for (const char* key : {"price_milli", "pre_close_milli", "change_bp", "amount_yuan", "bid_amount_yuan", "rank_amount", "src"}) {
            const std::string marker = std::string("\"") + key + "\":";
            std::size_t occurrences = 0, pos = 0;
            while ((pos = cmd.value.find(marker, pos)) != std::string::npos) { ++occurrences; pos += marker.size(); }
            c977_full_ok = c977_full_ok && occurrences == static_cast<std::size_t>(count);
        }
        c977_expiry_ok = cmd.expire_at == 1794931200;
        c977_full_projections.push_back(cmd);
    }
    const bool c977_check_full = expect(c977_full_ok, "C977 full JSON has one object per candidate and required fields");
    const bool c977_check_expiry = expect(c977_expiry_ok, "C977 expiry 2026-11-18 00:00 Asia/Shanghai");
    if (!c977_check_full || !c977_check_expiry) return false;
    for (const auto& item : std::vector<std::pair<std::string, int64_t>>{{"0920", 1794360003000LL}, {"0924", 1794360250000LL}}) {
        TickBatch slice; slice.logical_ts_ms = item.second;
        for (auto it = std::sregex_iterator(c977_text.begin(), c977_text.end(), c977_object_re); it != std::sregex_iterator(); ++it) {
            const std::string obj = it->str();
            if (obj.find("\"capture_tag\":\"" + item.first + "\"") == std::string::npos) continue;
            const auto pos = obj.find("\"symbol\":\""); if (pos == std::string::npos) return false;
            RawTick tick{}; const std::string symbol = obj.substr(pos + 10, 6); set_symbol(tick, symbol.c_str());
            tick.ts_ms = c977_number(obj,"ts"); tick.px_milli = c977_number(obj,"px_milli");
            tick.pc_milli = c977_number(obj,"pc_milli"); tick.amt_yuan = c977_number(obj,"amt_yuan");
            tick.vol_units = c977_number(obj,"vol_units"); tick.bp_milli[0] = c977_number(obj,"bp1_milli");
            tick.ap_milli[0] = c977_number(obj,"ap1_milli"); tick.bv[0] = c977_number(obj,"bv1");
            tick.av[0] = c977_number(obj,"av1"); tick.bv[1] = c977_number(obj,"bv2"); tick.av[1] = c977_number(obj,"av2");
            slice.ticks.push_back(tick);
        }
        if (!expect(slice.ticks.size() == 5225, "C977 real TD earlier slice fixture")) return false;
        EngineCore slice_engine(config); slice_engine.initialize(); auto slice_stats = slice_engine.on_batch(slice);
        const auto commands = redis_writer.build_a2_commands(slice_engine.quote_store(), slice_stats.snapshot_trigger, slice.logical_ts_ms);
        bool found = false;
        for (const auto& cmd : commands) if (cmd.key == "core:auction:full:2026-11-11:" + item.first) {
            const auto rows = std::distance(std::sregex_iterator(cmd.value.begin(), cmd.value.end(), c977_symbol_re), std::sregex_iterator());
            found = rows > 0 && cmd.fields.size() == 2 && std::to_string(rows) == cmd.fields[0].second && cmd.expire_at == 1794931200;
            c977_full_projections.push_back(cmd);
        }
        if (!expect(found, "C977 real TD earlier slice full view")) return false;
    }
    for (const auto& cmd : c977_full_projections) {
        std::cout << "C977 " << cmd.key.substr(cmd.key.size() - 4) << " rows=" << cmd.fields[0].second << " json_bytes=" << cmd.value.size() << "\n";
        const auto pos = cmd.value.find("\"301718\":{");
        if (cmd.key.find(":0925") != std::string::npos && pos != std::string::npos) {
            const auto end = cmd.value.find('}', pos);
            std::cout << "C977 sample {" << cmd.value.substr(pos, end - pos + 1) << "}\n";
        }
    }
#if defined(T1_V2_ENABLE_REDIS)
    if (std::getenv("C977_LIVE_REDIS")) {
        ConfigV2 c977_redis_config = config; c977_redis_config.redis.host = "127.0.0.1"; c977_redis_config.redis.port = 6380; c977_redis_config.redis.db = 0;
        HiredisRedisCommandExecutor c977_executor(c977_redis_config);
        for (auto cmd : c977_full_projections) {
            const std::string tag = cmd.key.substr(cmd.key.size() - 4);
            cmd.key = "core:test:c977:full:2026-11-11:" + tag; cmd.secondary_key = cmd.key + ":meta";
            const auto result = c977_executor.execute({cmd});
            if (!expect(result.ok, (std::string("C977 guarded Redis publication: ") + result.error).c_str())) return false;
            std::cout << "C977 guarded Redis " << tag << " SET EXAT GET COMPLETE ok\n";
        }
    }
#endif
    const char* c977_root_env = std::getenv("CORE_DATA_ROOT");
    const std::string c977_root = c977_root_env ? c977_root_env : "";
    setenv("CORE_DATA_ROOT", "/nonexistent-c977-calendar", 1);
    const auto c977_missing_commands = redis_writer.build_a2_commands(c977_engine.quote_store(), c977_stats.snapshot_trigger, c977_batch.logical_ts_ms);
    if (c977_root_env) setenv("CORE_DATA_ROOT", c977_root.c_str(), 1); else unsetenv("CORE_DATA_ROOT");
    bool c977_missing_ok = true;
    for (const auto& cmd : c977_missing_commands) if (cmd.key.find("core:auction:full:") == 0) c977_missing_ok = false;
    if (!expect(c977_missing_ok, "C977 missing calendar suppresses full publication")) return false;
    // Real TD records for 301718 before and after the 0920 cutoff.
    const std::string& c977_cutoff_text = c977_text;
    TickBatch c977_cutoff_batch; c977_cutoff_batch.logical_ts_ms = 1794360018000LL;
    int c977_before_px = 0;
    for (auto it = std::sregex_iterator(c977_cutoff_text.begin(), c977_cutoff_text.end(), c977_object_re); it != std::sregex_iterator(); ++it) {
        const std::string obj = it->str();
        if (obj.find("\"301718\"") == std::string::npos ||
            (obj.find("\"capture_tag\":\"0920\"") == std::string::npos && obj.find("\"capture_tag\":\"cutoff_check\"") == std::string::npos)) continue;
        RawTick tick{}; set_symbol(tick, "301718");
        tick.ts_ms = c977_number(obj, "ts"); tick.px_milli = c977_number(obj, "px_milli");
        tick.pc_milli = c977_number(obj, "pc_milli"); tick.amt_yuan = c977_number(obj, "amt_yuan");
        tick.vol_units = c977_number(obj, "vol_units"); tick.bp_milli[0] = c977_number(obj, "bp1_milli");
        tick.ap_milli[0] = c977_number(obj, "ap1_milli"); tick.bv[0] = c977_number(obj, "bv1");
        tick.av[0] = c977_number(obj, "av1"); tick.bv[1] = c977_number(obj, "bv2"); tick.av[1] = c977_number(obj, "av2");
        if (tick.ts_ms == 1794360000000LL) c977_before_px = tick.bp_milli[0];
        c977_cutoff_batch.ticks.push_back(tick);
    }
    EngineCore c977_cutoff_engine(config); c977_cutoff_engine.initialize(); c977_cutoff_engine.on_batch(c977_cutoff_batch);
    SnapshotTriggerState c977_early_trigger{}; c977_early_trigger.emit_a20 = true;
    const auto c977_early = redis_writer.build_a2_commands(c977_cutoff_engine.quote_store(), c977_early_trigger, c977_cutoff_batch.logical_ts_ms);
    bool c977_cutoff_ok = false;
    for (const auto& cmd : c977_early) if (cmd.key == "core:auction:full:2026-11-11:0920")
        c977_cutoff_ok = c977_cutoff_batch.ticks.size() == 2 && c977_before_px > 0 && cmd.value.find("\"301718\":{\"price_milli\":" + std::to_string(c977_before_px)) != std::string::npos;
    if (!expect(c977_cutoff_ok, "C977 0920 retains real pre-cutoff price")) return false;
    // C976: TD rows are replayed individually so each price source is checked.
    std::ifstream c976_fixture("t1_v2/tests/fixtures/t1v2/tick_2026-11-11_auction.json");
    std::stringstream c976_stream;
    c976_stream << c976_fixture.rdbuf();
    const std::string c976_text = c976_stream.str();
    const std::regex c976_object_re(R"(\{[^}]+\})");
    auto c976_number = [](const std::string& object, const char* key) -> int64_t {
        std::smatch m;
        const std::regex re(std::string("\"") + key + "\"\\s*:\\s*([0-9]+)");
        return std::regex_search(object, m, re) ? std::stoll(m[1].str()) : -1;
    };
    std::vector<RawTick> c976_ticks;
    for (auto it = std::sregex_iterator(c976_text.begin(), c976_text.end(), c976_object_re);
         it != std::sregex_iterator(); ++it) {
        const std::string object = it->str();
        std::smatch sym;
        if (!expect(std::regex_search(object, sym, std::regex(R"re("symbol"\s*:\s*"([0-9]+)")re")), "C976 fixture symbol")) return false;
        RawTick tick;
        set_symbol(tick, sym[1].str().c_str());
        tick.ts_ms = c976_number(object, "ts"); tick.px_milli = c976_number(object, "px_milli");
        tick.pc_milli = c976_number(object, "pc_milli"); tick.amt_yuan = c976_number(object, "amt_yuan");
        tick.vol_units = c976_number(object, "vol_units"); tick.bp_milli[0] = c976_number(object, "bp1_milli");
        tick.ap_milli[0] = c976_number(object, "ap1_milli"); tick.bv[0] = c976_number(object, "bv1");
        tick.av[0] = c976_number(object, "av1"); tick.bv[1] = c976_number(object, "bv2"); tick.av[1] = c976_number(object, "av2");
        c976_ticks.push_back(tick);
    }
    if (!expect(c976_ticks.size() == 4, "C976 four TD fixture ticks")) return false;
    AuctionCalculator c976_calculator;
    QuoteStateStore c976_store;
    for (std::size_t i = 0; i < 3; ++i) {
        QuoteState& state = c976_store.get_or_create(c976_ticks[i]);
        c976_calculator.apply_tick(state, c976_ticks[i], MarketPhase::Auction);
    }
    QuoteState& c976_fallback = c976_store.get_or_create(c976_ticks[1]);
    c976_calculator.apply_tick(c976_fallback, c976_ticks[3], MarketPhase::Intraday, true, true);
    const auto c976_archive = [&](const RawTick& tick) {
        const QuoteState& state = c976_store.get_or_create(tick);
        return redis_writer.build_anchor_archive_json("0925", {&state});
    };
    const std::string c976_fb = c976_archive(c976_ticks[1]);
    if (!expect(c976_fb.find("\"a25_src\":\"indicative_bid_ask_mid\",\"a25_fb_ts\":1794360000000") != std::string::npos &&
                c976_fb.find("\"a25_px\":55000,\"a25_ts\":1794360000000") != std::string::npos &&
                c976_fb.find("\"a25_fb\":") == std::string::npos,
                "C976 fallback sole price and cutoff")) return false;
    const std::string c976_match = c976_archive(c976_ticks[0]);
    if (!expect(c976_match.find("\"a25_src\":\"auction_match\",\"a25_fb_ts\":null") != std::string::npos &&
                c976_match.find("\"a25_px\":19180,\"a25_ts\":1794360300000") != std::string::npos &&
                c976_match.find("\"a25_fb\":") == std::string::npos,
                "C976 matched sole price")) return false;
    const std::string c976_missing = c976_archive(c976_ticks[2]);
    if (!expect(c976_missing.find("\"a25_src\":\"missing\",\"a25_fb_ts\":null") != std::string::npos &&
                c976_missing.find("\"a25_px\":null,\"a25_ts\":null") != std::string::npos &&
                c976_missing.find("\"a25_fb\":") == std::string::npos,
                "C976 missing book has no price")) return false;
    // TD-stored upstream ticks: exercise the archive through its public writer.
    std::ifstream fixture("t1_v2/tests/fixtures/t1v2/tick_2026-11-10_auction.json");
    std::stringstream fixture_stream;
    fixture_stream << fixture.rdbuf();
    std::ifstream extra_fixture("t1_v2/tests/fixtures/t1v2/tick_2026-11-10_C958.json");
    fixture_stream << extra_fixture.rdbuf();
    const std::string fixture_text = fixture_stream.str();
    const std::regex object_re(R"(\{[^}]+\})");
    auto number = [](const std::string& object, const char* key) -> int64_t {
        std::smatch m;
        const std::regex re(std::string("\"") + key + "\"\\s*:\\s*([0-9]+)");
        return std::regex_search(object, m, re) ? std::stoll(m[1].str()) : -1;
    };
    QuoteStateStore fixture_store;
    AuctionCalculator fixture_calculator;
    int fixture_count = 0;
    std::vector<RawTick> fixture_ticks;
    for (auto it = std::sregex_iterator(fixture_text.begin(), fixture_text.end(), object_re);
         it != std::sregex_iterator(); ++it) {
        const std::string object = it->str();
        std::smatch sym;
        if (!expect(std::regex_search(object, sym, std::regex(R"re("symbol"\s*:\s*"([0-9]+)")re")), "fixture symbol")) return false;
        RawTick tick;
        set_symbol(tick, sym[1].str().c_str());
        tick.ts_ms = number(object, "ts"); tick.px_milli = number(object, "px_milli");
        tick.pc_milli = number(object, "pc_milli"); tick.amt_yuan = number(object, "amt_yuan");
        tick.vol_units = number(object, "vol_units"); tick.bp_milli[0] = number(object, "bp1_milli");
        tick.ap_milli[0] = number(object, "ap1_milli"); tick.bv[0] = number(object, "bv1");
        tick.av[0] = number(object, "av1"); tick.bv[1] = number(object, "bv2"); tick.av[1] = number(object, "av2");
        fixture_ticks.push_back(tick);
        if (fixture_count < 3) {
            QuoteState& state = fixture_store.get_or_create(tick);
            fixture_calculator.apply_tick(state, tick, MarketPhase::Auction);
        }
        ++fixture_count;
    }
    if (!expect(fixture_count == 6, "six TD fixture ticks")) return false;
    SnapshotTriggerState fixture_trigger; fixture_trigger.emit_a25 = true;
    const auto fixture_commands = redis_writer.build_a2_commands(fixture_store, fixture_trigger, 1794273906000LL);
    std::string archive;
    for (const auto& command : fixture_commands) if (command.key.find("market:auction:anchor:") == 0 && command.type == RedisCommandType::SetString) archive = command.value;
    if (!expect(archive.find("\"a25_px\":19160,\"a25_ts\":1794273900000") != std::string::npos, "TD fixture matched price and timestamp archived")) return false;
    if (!expect(archive.find("\"301718\":{") != std::string::npos && archive.find("\"a25_src\":\"indicative_bid_ask_mid\",\"a25_fb_ts\":1794273300000,\"a25_px\":68980,\"a25_ts\":1794273300000") != std::string::npos && archive.find("\"a25_fb\":") == std::string::npos, "TD fixture indicative midpoint fallback")) return false;
    if (!expect(archive.find("\"a25_src\":\"auction_match\"") != std::string::npos, "TD fixture matched auction")) return false;
    if (!expect(archive.find("\"601512\":{") == std::string::npos, "zero auction member excluded")) return false;
    bool missing_capture = false;
    fixture_store.for_each_active([&](const QuoteState& q) {
        if (std::string(q.symbol) == "601512") missing_capture = q.auction.a25_match_ts_ms == 0 && q.auction.a25_fb_ts_ms == 0;
    });
    if (!expect(missing_capture, "TD fixture missing book capture")) return false;
    // Order and logical clock are spec arrangements, not recorded arrivals.
    EngineCore c958_engine(config);
    if (!expect(c958_engine.initialize(), "C958 engine initialize")) return false;
    TickBatch c958_batch;
    c958_batch.logical_ts_ms = 1794273961000LL; // 09:26:01, first trigger batch
    c958_batch.ticks = {fixture_ticks[3]}; // prior session tick in current-day batch
    c958_engine.on_batch(c958_batch);
    bool old_rejected = false;
    c958_engine.quote_store().for_each_active([&](const QuoteState& q) {
        if (std::string(q.symbol) == "301569") old_rejected = q.auction.a25_match_ts_ms == 0 && q.auction.a25_fb_ts_ms == 0;
    });
    if (!expect(old_rejected, "C958 old-day capture rejected")) return false;
    c958_batch.ticks = {fixture_ticks[0], fixture_ticks[5], fixture_ticks[4]};
    c958_engine.on_batch(c958_batch);
    bool ordered_capture = false;
    c958_engine.quote_store().for_each_active([&](const QuoteState& q) {
        if (std::string(q.symbol) == "301569") ordered_capture = q.auction.a25_match_px_milli == 19160 && q.auction.a25_match_ts_ms == 1794273900000LL && q.auction.a25_match_pc_milli == 18830 && q.auction.a25_fb_ts_ms == 1794273900000LL;
    });
    if (!expect(ordered_capture, "C958 event time and current day capture")) return false;
    EngineCore first_save_engine(config);
    if (!expect(first_save_engine.initialize(), "C958 first save engine initialize")) return false;
    TickBatch first_save_batch;
    first_save_batch.logical_ts_ms = 1794273300000LL;
    first_save_batch.ticks = {fixture_ticks[5]};
    first_save_engine.on_batch(first_save_batch);
    first_save_batch.logical_ts_ms = 1794273961000LL;
    first_save_batch.ticks = {fixture_ticks[3], fixture_ticks[0], fixture_ticks[5], fixture_ticks[4]};
    const auto first_save_stats = first_save_engine.on_batch(first_save_batch);
    const auto first_save_commands = redis_writer.build_a2_commands(first_save_engine.quote_store(), first_save_stats.snapshot_trigger, first_save_batch.logical_ts_ms);
    std::string first_save_archive;
    for (const auto& command : first_save_commands) if (command.key.find("market:auction:anchor:") == 0 && command.type == RedisCommandType::SetString) first_save_archive = command.value;
    const auto matched_symbol = first_save_archive.find("\"301569\":{");
    if (!expect(matched_symbol != std::string::npos && first_save_archive.find("\"a25_src\":\"auction_match\"", matched_symbol) != std::string::npos && first_save_archive.find("\"a25_px\":19160,\"a25_ts\":1794273900000,\"a25_pc\":18830,\"a25_change_bp\":175", matched_symbol) != std::string::npos, "C958 first save serial archive")) return false;
    const auto no_revision = first_save_engine.on_batch(first_save_batch);
    if (!expect(!no_revision.snapshot_trigger.emit_a25, "C958 first save emits only once")) return false;
    const auto q2_commands = redis_writer.build_q2_commands(engine.quote_store(), ts_0922);
    if (!expect(!q2_commands.empty(), "q2 command generated")) return false;
    RedisArgvCommand first_redis_argv;
    if (!expect(RedisCommandFormatter::format(q2_commands.front(), first_redis_argv), "redis command formats")) return false;
    if (!expect(
            first_redis_argv.argv.size() == 2 + q2_commands.front().fields.size() * 2,
            "q2 hset argv count follows field list"
        )) return false;
    bool has_active_symbol_index = false;
    for (const RedisCommand& command : q2_commands) {
        if (command.type == RedisCommandType::SAdd && command.key.find("q2:active:") == 0) {
            RedisArgvCommand sadd_argv;
            if (!expect(RedisCommandFormatter::format(command, sadd_argv), "q2 active SADD formats")) return false;
            has_active_symbol_index = sadd_argv.argv.size() >= 3;
        }
    }
    if (!expect(has_active_symbol_index, "q2 active symbol index generated")) return false;
    const RedisCommandFormatStats redis_stats = RedisCommandFormatter::estimate(q2_commands);
    if (!expect(redis_stats.ok && redis_stats.command_count == q2_commands.size(), "redis command estimate")) return false;
    const auto stock_market_q2_commands = redis_writer.build_q2_commands(stock_market_engine.quote_store(), ts_0922);
    bool has_stock_alias_key = false;
    for (const RedisCommand& command : stock_market_q2_commands) {
        if (command.key == "q2:000001") {
            has_stock_alias_key = true;
        }
    }
    if (!expect(has_stock_alias_key, "sz stock writes q2 alias")) return false;

    QuoteState intraday_without_auction;
    std::memcpy(intraday_without_auction.symbol, "000007", 6);
    std::memcpy(intraday_without_auction.market, "sz", 2);
    intraday_without_auction.phase = MarketPhase::Intraday;
    intraday_without_auction.px_milli = 10100;
    intraday_without_auction.pc_milli = 10000;
    const RedisFieldList intraday_empty_auction_fields = redis_writer.build_q2_fields(intraday_without_auction);
    bool has_empty_a25_field = false;
    for (const auto& field : intraday_empty_auction_fields) {
        if (field.first == "a25" || field.first == "am" || field.first == "br" || field.first == "ar") {
            has_empty_a25_field = true;
        }
    }
    if (!expect(!has_empty_a25_field, "intraday q2 does not overwrite missing auction anchors")) return false;

    QuoteStateStore restarted_quote_store;
    RawTick restarted_intraday_tick = make_tick("688085", ts_0922, 13190, 0, "kc");
    QuoteState& restarted_quote = restarted_quote_store.get_or_create(restarted_intraday_tick);
    restarted_quote.phase = MarketPhase::Intraday;
    restarted_quote.ts_ms = ts_0922;
    const RedisFieldList restarted_q2_fields = redis_writer.build_q2_fields(restarted_quote);
    bool restarted_q2_auction_fields_cleared = false;
    for (const auto& field : restarted_q2_fields) {
        if (field.first == "a25" && field.second == "0") {
            restarted_q2_auction_fields_cleared = true;
        }
    }
    if (!expect(restarted_q2_auction_fields_cleared, "restarted q2 clears prior auction fields")) return false;
    if (!expect(redis_writer.should_write_q2(restarted_quote, ts_0922), "restarted q2 reset forces first write")) return false;
    if (!expect(redis_writer.commit_q2_writes(restarted_quote_store, ts_0922) == 1,
                "restarted q2 reset commit")) return false;
    if (!expect(!restarted_quote.auction_fields_reset_pending, "restarted q2 reset clears after commit")) return false;

    QuoteState intraday_with_auction = intraday_without_auction;
    intraday_with_auction.auction.a25_px_milli = 10500;
    intraday_with_auction.auction.match_amt_yuan = 12000000;
    const RedisFieldList intraday_kept_auction_fields = redis_writer.build_q2_fields(intraday_with_auction);
    bool has_kept_a25_field = false;
    for (const auto& field : intraday_kept_auction_fields) {
        if (field.first == "a25" && field.second == "10500") {
            has_kept_a25_field = true;
        }
    }
    if (!expect(has_kept_a25_field, "intraday q2 keeps known auction anchors")) return false;

    const RuntimeBatchStats runtime_stats = RuntimeBatchStatsBuilder::build(
        7,
        ts_0922,
        ts_0922 + 30,
        build_stats,
        stats3,
        redis_stats
    );
    if (!expect(runtime_stats.source_rejected == 3 && runtime_stats.redis_fields > 0, "runtime batch stats")) return false;
    if (!expect(runtime_stats.wall_ts_ms == ts_0922 + 30, "runtime wall timestamp stats")) return false;
    SnapshotTriggerState legacy_trigger;
    legacy_trigger.emit_a20 = true;
    const auto a2_commands_for_legacy = redis_writer.build_a2_commands(engine.quote_store(), legacy_trigger, ts_0922);
    bool has_legacy_auction_key = false;
    bool has_legacy_latest_key = false;
    bool has_a2_effective_price = false;
    bool has_a2_non_price_payload = false;
    for (const RedisCommand& command : a2_commands_for_legacy) {
        if (command.key.find("a2:") == 0) {
            for (const auto& field : command.fields) {
                if (field.first == "top_amt" && field.second.find("\"px\":10000") != std::string::npos) {
                    has_a2_effective_price = true;
                    has_a2_non_price_payload =
                        field.second.find("\"am\":816000") != std::string::npos &&
                        field.second.find("\"br\":3060000") != std::string::npos &&
                        field.second.find("\"ar\":2040000") != std::string::npos;
                }
            }
        }
        if (command.key.find("market:auction:") == 0 && command.key.find(":0920") != std::string::npos) {
            for (const auto& field : command.fields) {
                if (field.first == "top_amount" && field.second.find("\"symbol\"") != std::string::npos) {
                    has_legacy_auction_key = true;
                    if (!expect(field.second.find("\"price\":10") != std::string::npos,
                                "legacy auction uses virtual auction price")) return false;
                    if (!expect(field.second.find("\"change_pct\":0.000000") != std::string::npos,
                                "legacy auction change_pct uses virtual price")) return false;
                }
            }
        }
        if (command.key.find("market:auction:") == 0 && command.key.find(":latest") != std::string::npos) {
            has_legacy_latest_key = true;
        }
    }
    if (!expect(has_legacy_auction_key, "legacy market auction top_amount key")) return false;
    if (!expect(has_legacy_latest_key, "legacy market auction latest key")) return false;
    if (!expect(has_a2_effective_price, "a2 top amount uses effective auction price")) return false;
    if (!expect(has_a2_non_price_payload, "a2 non-price payload remains unchanged")) return false;
    SnapshotTriggerState anchor_trigger;
    anchor_trigger.emit_a25 = true;
    const auto a2_commands_for_anchor = redis_writer.build_a2_commands(engine.quote_store(), anchor_trigger, ts_0922);
    bool has_anchor_archive = false;
    for (const RedisCommand& command : a2_commands_for_anchor) {
        if (command.type == RedisCommandType::SetString &&
            command.key.find("market:auction:anchor:") == 0 &&
            command.value.find("\"redis_0925\"") != std::string::npos) {
            RedisArgvCommand set_argv;
            if (!expect(RedisCommandFormatter::format(command, set_argv), "anchor archive SET formats")) return false;
            has_anchor_archive = set_argv.argv.size() == 3;
        }
    }
    if (!expect(has_anchor_archive, "market auction anchor archive key")) return false;

    QuoteStateStore missing_anchor_store;
    RawTick missing_anchor_tick = make_tick("000012", ts_0922, 13000, 1000000, "sz");
    QuoteState& missing_anchor_quote = missing_anchor_store.get_or_create(missing_anchor_tick);
    missing_anchor_quote.phase = MarketPhase::Auction;
    missing_anchor_quote.ts_ms = ts_0922;
    missing_anchor_quote.auction.ts_ms = ts_0922;
    missing_anchor_quote.auction.match_amt_yuan = 1000000;
    missing_anchor_quote.auction.rest_bid_amt_yuan = 200000;
    missing_anchor_quote.auction.rest_ask_amt_yuan = 100000;
    SnapshotTriggerState missing_anchor_trigger;
    missing_anchor_trigger.emit_a24 = true;
    const auto missing_anchor_commands = redis_writer.build_a2_commands(
        missing_anchor_store,
        missing_anchor_trigger,
        ts_0922
    );
    bool missing_anchor_top_is_null = false;
    bool missing_anchor_summary_counts_unavailable = false;
    for (const RedisCommand& command : missing_anchor_commands) {
        for (const auto& field : command.fields) {
            if (field.first == "top_amt" && field.second.find("\"px\":null") != std::string::npos) {
                missing_anchor_top_is_null = true;
            }
            if (field.first == "summary" &&
                field.second.find("\"valid_stock_count\":0") != std::string::npos &&
                field.second.find("\"unavailable_stock_count\":1") != std::string::npos) {
                missing_anchor_summary_counts_unavailable = true;
            }
        }
    }
    if (!expect(missing_anchor_top_is_null, "redis formal snapshot missing price is null")) return false;
    if (!expect(missing_anchor_summary_counts_unavailable,
                "redis summary separates unavailable price count")) return false;
#if defined(T1_V2_ENABLE_REDIS)
    HiredisRedisCommandExecutor hiredis_executor(config);
    const RedisExecutionResult empty_redis_result = hiredis_executor.execute({});
    if (!expect(empty_redis_result.ok && empty_redis_result.command_count == 0, "hiredis empty execute")) return false;
    if (!expect(!hiredis_executor.is_connected(), "hiredis empty execute does not connect")) return false;
#endif
#if defined(T1_V2_ENABLE_RABBITMQ) && (!defined(T1_V2_ENABLE_PROTOBUF) || !defined(T1_V2_ENABLE_ZLIB))
    RabbitMqTickSource rabbit_without_decoder(config);
    if (!expect(!rabbit_without_decoder.start(), "rabbitmq source refuses to start without full live decoder")) return false;
#endif

    RuntimePipeline pipeline(config);
    if (!expect(pipeline.initialize(), "runtime pipeline initialize")) return false;
    RuntimePipelineResult pipeline_result = pipeline.process_batch(batch3, build_stats);
    if (!expect(pipeline_result.engine_stats.tick_count == 1, "runtime pipeline engine ticks")) return false;
    if (!expect(!pipeline_result.redis_commands.empty(), "runtime pipeline redis commands")) return false;
    if (!expect(pipeline_result.has_q2_commands, "runtime pipeline q2 command flag")) return false;
    bool has_runtime_command = false;
    for (const RedisCommand& command : pipeline_result.redis_commands) {
        if (command.key.find("m2:runtime:") == 0) {
            has_runtime_command = true;
        }
    }
    if (!expect(has_runtime_command, "runtime pipeline m2 command")) return false;
    if (!expect(pipeline_result.runtime_stats.source_rejected == 3, "runtime pipeline stats")) return false;
    if (!expect(count_dirty_quotes(pipeline.engine().quote_store()) > 0, "runtime pipeline dirty before commit")) return false;
    NullRedisCommandExecutor null_redis_executor;
    NullTDengineCommandExecutor null_td_executor_for_runtime;
    RuntimeExecutionCoordinator coordinator(null_redis_executor, null_td_executor_for_runtime);
    const RuntimeExecutionResult execution_result = coordinator.execute_and_commit(pipeline, pipeline_result);
    if (!expect(execution_result.ok, "runtime execution coordinator ok")) return false;
    if (!expect(execution_result.redis_committed_quotes > 0, "runtime execution coordinator commit count")) return false;
    if (!expect(count_dirty_quotes(pipeline.engine().quote_store()) == 0, "runtime pipeline dirty after commit")) return false;
    pipeline.shutdown();

    RuntimePipeline intraday_pipeline(config);
    if (!expect(intraday_pipeline.initialize(), "intraday runtime pipeline initialize")) return false;
    SourceTickBatchBuildStats intraday_build_stats = build_stats;
    RuntimePipelineResult intraday_pipeline_result1 = intraday_pipeline.process_batch(trade_batch1, intraday_build_stats);
    if (!expect(!intraday_pipeline_result1.redis_commands.empty(), "intraday runtime pipeline first batch redis commands")) return false;
    RuntimePipelineResult intraday_pipeline_result2 = intraday_pipeline.process_batch(trade_batch2, intraday_build_stats);
    bool has_open_2m_summary_command = false;
    for (const RedisCommand& command : intraday_pipeline_result2.redis_commands) {
        if (command.type == RedisCommandType::SetString &&
            command.key.find("market:open2m:summary:") == 0 &&
            command.value.find("\"top10_amount\":") != std::string::npos &&
            command.value.find("\"top20_amount\":") != std::string::npos) {
            RedisArgvCommand set_argv;
            if (!expect(RedisCommandFormatter::format(command, set_argv), "open2m summary SET formats")) return false;
            has_open_2m_summary_command = set_argv.argv.size() == 3;
        }
    }
    if (!expect(has_open_2m_summary_command, "intraday runtime pipeline open2m summary command")) return false;
    TickBatch tail_batch;
    const int64_t ts_1500_open2m_guard = make_local_ts_ms(2026, 4, 29, 15, 0, 0);
    tail_batch.logical_ts_ms = ts_1500_open2m_guard;
    tail_batch.ticks.push_back(make_tick("000002", ts_1500_open2m_guard, 10400, 260000000));
    RuntimePipelineResult tail_pipeline_result = intraday_pipeline.process_batch(tail_batch, intraday_build_stats);
    bool has_tail_open_2m_summary_command = false;
    for (const RedisCommand& command : tail_pipeline_result.redis_commands) {
        if (command.type == RedisCommandType::SetString &&
            command.key.find("market:open2m:summary:") == 0) {
            has_tail_open_2m_summary_command = true;
        }
    }
    if (!expect(!has_tail_open_2m_summary_command, "tail runtime does not overwrite open2m summary")) return false;
    intraday_pipeline.shutdown();

    TickSourceResult loop_source_result;
    loop_source_result.status = TickSourceStatus::Ok;
    loop_source_result.batch = batch3;
    loop_source_result.source_stats = build_stats;
    loop_source_result.requires_ack = true;
    loop_source_result.delivery_tag = 123;
    std::vector<TickSourceResult> loop_results;
    loop_results.push_back(loop_source_result);
    TickSourceResult loop_eos_result;
    loop_eos_result.status = TickSourceStatus::EndOfStream;
    loop_results.push_back(loop_eos_result);
    RuntimeLoopOptions loop_options;
    loop_options.max_empty_polls = 2;
    FakeTickSource::Counters ack_counters;
    RuntimeLoop loop(
        config,
        std::make_unique<FakeTickSource>(std::move(loop_results), &ack_counters),
        null_redis_executor,
        null_td_executor_for_runtime,
        loop_options
    );
    const RuntimeLoopStats loop_stats = loop.run();
    if (!expect(loop_stats.ok, "runtime loop ok")) return false;
    if (!expect(loop_stats.batches == 1 && loop_stats.ticks == 1, "runtime loop batch stats")) return false;
    if (!expect(loop_stats.source_input == build_stats.input_count, "runtime loop source input stats")) return false;
    if (!expect(loop_stats.source_rejected == build_stats.rejected_count, "runtime loop source rejected stats")) return false;
    if (!expect(loop_stats.redis_committed_quotes > 0, "runtime loop commits dirty quotes")) return false;
    if (!expect(loop_stats.source_acks == 1 && loop_stats.source_rejects == 0, "runtime loop ack stats")) return false;
    if (!expect(ack_counters.ack_count == 1 && ack_counters.last_delivery_tag == 123, "runtime loop source ack")) return false;

    const std::string progress_log_path = "t1_v2_self_test_progress.log";
    std::remove(progress_log_path.c_str());
    ConfigV2 progress_config = config;
    progress_config.logging.verbose = false;
    progress_config.logging.enable_file_log = true;
    progress_config.logging.file_path = progress_log_path;
    progress_config.processing.report_interval_seconds = 10;
    std::vector<TickSourceResult> progress_results;
    progress_results.push_back(loop_source_result);
    progress_results.push_back(loop_eos_result);
    RuntimeLoop progress_loop(
        progress_config,
        std::make_unique<FakeTickSource>(std::move(progress_results)),
        null_redis_executor,
        null_td_executor_for_runtime,
        loop_options
    );
    const RuntimeLoopStats progress_stats = progress_loop.run();
    std::ifstream progress_input(progress_log_path);
    std::string progress_line;
    std::getline(progress_input, progress_line);
    progress_input.close();
    std::remove(progress_log_path.c_str());
    if (!expect(progress_stats.ok &&
                progress_line.find("t1_v2 progress") != std::string::npos &&
                progress_line.find("batches=1") != std::string::npos &&
                progress_line.find("pipeline_ms=") != std::string::npos &&
                progress_line.find("commit_ms=") != std::string::npos &&
                progress_line.find("ack_ms=") != std::string::npos &&
                progress_line.find("wall_lag_ms=") != std::string::npos,
                "runtime progress log honors configured file sink")) return false;

    std::vector<TickSourceResult> reject_loop_results;
    reject_loop_results.push_back(loop_source_result);
    reject_loop_results.push_back(loop_eos_result);
    FakeTickSource::Counters reject_counters;
    FailingRedisCommandExecutor failing_redis_executor;
    ConfigV2 reject_loop_config = config;
    reject_loop_config.runtime_mode = RuntimeMode::Replay;
    RuntimeLoop reject_loop(
        reject_loop_config,
        std::make_unique<FakeTickSource>(std::move(reject_loop_results), &reject_counters),
        failing_redis_executor,
        null_td_executor_for_runtime,
        loop_options
    );
    const RuntimeLoopStats reject_loop_stats = reject_loop.run();
    if (!expect(!reject_loop_stats.ok, "runtime loop fails on redis failure")) return false;
    if (!expect(reject_loop_stats.source_acks == 0 && reject_loop_stats.source_rejects == 1, "runtime loop reject stats")) return false;
    if (!expect(reject_counters.reject_count == 1 && reject_counters.last_delivery_tag == 123, "runtime loop source reject")) return false;
    if (!expect(reject_counters.last_requeue, "runtime loop source reject requeue")) return false;

    ConfigV2 no_redis_replay_config = config;
    no_redis_replay_config.replay.write_redis = false;
    RuntimePipeline no_redis_replay_pipeline(no_redis_replay_config);
    if (!expect(no_redis_replay_pipeline.initialize(), "no-redis replay pipeline initialize")) return false;
    TickBatch replay_batch = batch3;
    replay_batch.mode = RuntimeMode::Replay;
    RuntimePipelineResult no_redis_result = no_redis_replay_pipeline.process_batch(replay_batch, build_stats);
    if (!expect(no_redis_result.engine_stats.tick_count == 1, "no-redis replay still processes ticks")) return false;
    if (!expect(no_redis_result.redis_commands.empty(), "no-redis replay suppresses redis commands")) return false;
    if (!expect(no_redis_result.tdengine_statements.empty(), "replay suppresses td statements by default")) return false;
    const RuntimeExecutionResult no_redis_execution = coordinator.execute_and_commit(
        no_redis_replay_pipeline,
        no_redis_result
    );
    if (!expect(no_redis_execution.ok, "no-redis replay execution ok")) return false;
    if (!expect(no_redis_execution.redis_committed_quotes == 0, "no-redis replay does not fake commit")) return false;
    no_redis_replay_pipeline.shutdown();

    ConfigV2 redis_only_replay_config = config;
    redis_only_replay_config.replay.write_redis = true;
    redis_only_replay_config.replay.write_tdengine = false;
    RuntimePipeline redis_only_replay_pipeline(redis_only_replay_config);
    if (!expect(redis_only_replay_pipeline.initialize(), "redis-only replay pipeline initialize")) return false;
    RuntimePipelineResult redis_only_replay_result = redis_only_replay_pipeline.process_batch(replay_batch, build_stats);
    if (!expect(!redis_only_replay_result.redis_commands.empty(), "redis-only replay emits redis commands")) return false;
    if (!expect(redis_only_replay_result.tdengine_statements.empty(), "redis-only replay suppresses td statements")) return false;
    redis_only_replay_pipeline.shutdown();

    ConfigV2 dry_run_config = config;
    dry_run_config.processing.dry_run = true;
    RuntimePipeline dry_run_pipeline(dry_run_config);
    if (!expect(dry_run_pipeline.initialize(), "dry-run pipeline initialize")) return false;
    RuntimePipelineResult dry_run_result = dry_run_pipeline.process_batch(batch3, build_stats);
    if (!expect(dry_run_result.engine_stats.tick_count == 1, "dry-run still processes ticks")) return false;
    if (!expect(dry_run_result.redis_commands.empty(), "dry-run suppresses redis commands")) return false;
    if (!expect(dry_run_result.tdengine_statements.empty(), "dry-run suppresses td statements")) return false;
    dry_run_pipeline.shutdown();

    // 落库时间闸门: 09:45 及之前写 TD, 09:45 之后不写。
    TickBatch before_cutoff = batch3;
    before_cutoff.logical_ts_ms = make_local_ts_ms(2026, 4, 29, 9, 45, 0);
    TickBatch after_cutoff = batch3;
    after_cutoff.logical_ts_ms = make_local_ts_ms(2026, 4, 29, 9, 46, 0);
    RuntimePipeline cutoff_pipeline(config);
    if (!expect(cutoff_pipeline.initialize(), "td cutoff pipeline initialize")) return false;
    if (!expect(!cutoff_pipeline.process_batch(before_cutoff, build_stats).tdengine_statements.empty(),
                "td statements kept at 09:45")) return false;
    if (!expect(cutoff_pipeline.process_batch(after_cutoff, build_stats).tdengine_statements.empty(),
                "td statements dropped after 09:45")) return false;
    cutoff_pipeline.shutdown();

    std::vector<TickSourceResult> dry_run_loop_results;
    dry_run_loop_results.push_back(loop_source_result);
    dry_run_loop_results.push_back(loop_eos_result);
    FakeTickSource::Counters dry_run_ack_counters;
    RuntimeLoop dry_run_loop(
        dry_run_config,
        std::make_unique<FakeTickSource>(std::move(dry_run_loop_results), &dry_run_ack_counters),
        null_redis_executor,
        null_td_executor_for_runtime,
        loop_options
    );
    const RuntimeLoopStats dry_run_loop_stats = dry_run_loop.run();
    if (!expect(dry_run_loop_stats.ok, "dry-run runtime loop ok")) return false;
    if (!expect(dry_run_loop_stats.source_acks == 0, "dry-run runtime loop suppresses ack")) return false;
    if (!expect(dry_run_loop_stats.source_ack_skipped == 1, "dry-run runtime loop tracks skipped ack")) return false;
    if (!expect(dry_run_ack_counters.ack_count == 0, "dry-run source ack not called")) return false;

    ConfigV2 dry_run_ack_config = dry_run_config;
    dry_run_ack_config.processing.ack_in_dry_run = true;
    std::vector<TickSourceResult> dry_run_ack_loop_results;
    dry_run_ack_loop_results.push_back(loop_source_result);
    dry_run_ack_loop_results.push_back(loop_eos_result);
    FakeTickSource::Counters dry_run_ack_enabled_counters;
    RuntimeLoop dry_run_ack_loop(
        dry_run_ack_config,
        std::make_unique<FakeTickSource>(std::move(dry_run_ack_loop_results), &dry_run_ack_enabled_counters),
        null_redis_executor,
        null_td_executor_for_runtime,
        loop_options
    );
    const RuntimeLoopStats dry_run_ack_loop_stats = dry_run_ack_loop.run();
    if (!expect(dry_run_ack_loop_stats.ok, "dry-run ack-enabled runtime loop ok")) return false;
    if (!expect(dry_run_ack_loop_stats.source_acks == 1, "dry-run ack-enabled runtime loop ack")) return false;
    if (!expect(dry_run_ack_enabled_counters.ack_count == 1, "dry-run ack-enabled source ack called")) return false;

    ConfigV2 clock_loop_config = config;
    clock_loop_config.runtime_mode = RuntimeMode::Replay;
    clock_loop_config.processing.dry_run = false;
    TickSourceResult clock_loop_result;
    clock_loop_result.status = TickSourceStatus::Clock;
    clock_loop_result.batch.mode = RuntimeMode::Replay;
    clock_loop_result.batch.logical_ts_ms = ts_0925_06;
    clock_loop_result.batch.wall_ts_ms = ts_0925_06;
    clock_loop_result.batch.seq_no = 1;
    TickSourceResult clock_loop_eos;
    clock_loop_eos.status = TickSourceStatus::EndOfStream;
    RuntimeLoop clock_loop(
        clock_loop_config,
        std::make_unique<FakeTickSource>(
            std::vector<TickSourceResult>{clock_loop_result, clock_loop_eos}
        ),
        null_redis_executor,
        null_td_executor_for_runtime,
        loop_options
    );
    const RuntimeLoopStats clock_loop_stats = clock_loop.run();
    if (!expect(clock_loop_stats.ok, "clock event runtime loop ok")) return false;
    if (!expect(clock_loop_stats.clock_events == 1 &&
                clock_loop_stats.batches == 0 &&
                clock_loop_stats.ticks == 0,
                "clock event does not count as market batch")) return false;

    // Live sources can return Empty while RabbitMQ has no message.  The
    // control-plane clock must still cross the delayed auction barrier without
    // creating a synthetic market tick.
    ConfigV2 live_clock_config = config;
    live_clock_config.runtime_mode = RuntimeMode::Live;
    live_clock_config.processing.dry_run = false;
    TickSourceResult live_clock_tick;
    live_clock_tick.status = TickSourceStatus::Ok;
    live_clock_tick.batch = last_auction_tick;
    TickSourceResult live_clock_empty;
    live_clock_empty.status = TickSourceStatus::Empty;
    TickSourceResult live_clock_eos;
    live_clock_eos.status = TickSourceStatus::EndOfStream;
    std::vector<int64_t> live_clock_times{ts_0925_05, ts_0925_06};
    std::size_t live_clock_time_index = 0;
    RuntimeLoopOptions live_clock_options;
    live_clock_options.wall_clock_ms = [&]() {
        if (live_clock_time_index >= live_clock_times.size()) {
            return live_clock_times.back();
        }
        return live_clock_times[live_clock_time_index++];
    };
    RecordingRedisCommandExecutor live_clock_redis;
    RuntimeLoop live_clock_loop(
        live_clock_config,
        std::make_unique<FakeTickSource>(std::vector<TickSourceResult>{
            live_clock_tick, live_clock_empty, live_clock_eos
        }),
        live_clock_redis,
        null_td_executor_for_runtime,
        live_clock_options
    );
    const RuntimeLoopStats live_clock_stats = live_clock_loop.run();
    if (!expect(live_clock_stats.ok && live_clock_stats.batches == 1 &&
                live_clock_stats.ticks == 1 && live_clock_stats.clock_events == 2,
                "live control clock advances across empty source poll")) return false;
    bool live_clock_emitted_a25 = false;
    for (const RedisCommand& command : live_clock_redis.executed) {
        if (command.key.find(":0925") != std::string::npos) {
            live_clock_emitted_a25 = true;
        }
    }
    if (!expect(live_clock_emitted_a25, "live control clock emits delayed a25")) return false;

    // A source message can contain the final cutoff tick and the first later
    // tick.  The RuntimeLoop must publish the cutoff from the <=09:32:10
    // partition, rather than applying the 09:32:11 update first and then
    // filtering an already-overwritten QuoteStateStore.
    ConfigV2 cutoff_partition_config = config;
    cutoff_partition_config.runtime_mode = RuntimeMode::Replay;
    cutoff_partition_config.processing.dry_run = false;
    cutoff_partition_config.replay.write_redis = true;
    cutoff_partition_config.replay.write_tdengine = false;
    const int64_t ts_0932_09 = make_local_ts_ms(2026, 4, 29, 9, 32, 9);
    const int64_t ts_0932_11_partition = make_local_ts_ms(2026, 4, 29, 9, 32, 11);
    TickSourceResult cutoff_seed_result;
    cutoff_seed_result.status = TickSourceStatus::Ok;
    cutoff_seed_result.batch.mode = RuntimeMode::Replay;
    cutoff_seed_result.batch.logical_ts_ms = ts_0932_09;
    cutoff_seed_result.batch.wall_ts_ms = ts_0932_09;
    cutoff_seed_result.batch.seq_no = 1;
    cutoff_seed_result.batch.ticks.push_back(make_tick("000007", ts_0932_09, 9900, 100000000, "sz"));
    cutoff_seed_result.source_stats.input_count = 1;
    cutoff_seed_result.source_stats.accepted_count = 1;
    cutoff_seed_result.source_stats.max_tick_ts_ms = ts_0932_09;

    TickSourceResult cutoff_crossing_result;
    cutoff_crossing_result.status = TickSourceStatus::Ok;
    cutoff_crossing_result.batch.mode = RuntimeMode::Replay;
    cutoff_crossing_result.batch.logical_ts_ms = ts_0932_11_partition;
    cutoff_crossing_result.batch.wall_ts_ms = ts_0932_11_partition;
    cutoff_crossing_result.batch.seq_no = 2;
    cutoff_crossing_result.batch.ticks.push_back(make_tick("000007", ts_0932_10, 10000, 110000000, "sz"));
    cutoff_crossing_result.batch.ticks.push_back(make_tick("000007", ts_0932_11_partition, 10100, 120000000, "sz"));
    cutoff_crossing_result.source_stats.input_count = 2;
    cutoff_crossing_result.source_stats.accepted_count = 2;
    cutoff_crossing_result.source_stats.max_tick_ts_ms = ts_0932_11_partition;

    RecordingRedisCommandExecutor cutoff_partition_redis;
    RuntimeLoop cutoff_partition_loop(
        cutoff_partition_config,
        std::make_unique<FakeTickSource>(std::vector<TickSourceResult>{
            cutoff_seed_result, cutoff_crossing_result, clock_loop_eos
        }),
        cutoff_partition_redis,
        null_td_executor_for_runtime,
        loop_options
    );
    const RuntimeLoopStats cutoff_partition_stats = cutoff_partition_loop.run();
    if (!expect(cutoff_partition_stats.ok && cutoff_partition_stats.batches == 2 &&
                cutoff_partition_stats.ticks == 3,
                "cutoff partition runtime loop processes each source tick once")) return false;
    int cutoff_publish_count = 0;
    bool cutoff_kept_exact_state = false;
    for (const RedisCommand& command : cutoff_partition_redis.executed) {
        if (command.type != RedisCommandType::AtomicCutoffPublish) {
            continue;
        }
        ++cutoff_publish_count;
        cutoff_kept_exact_state =
            command.value.find("\"symbol\":\"000007\",\"source_ts_ms\":" + std::to_string(ts_0932_10)) != std::string::npos &&
            command.value.find("\"px_milli\":10000") != std::string::npos &&
            command.value.find("\"source_ts_ms\":" + std::to_string(ts_0932_11_partition)) == std::string::npos;
    }
    if (!expect(cutoff_publish_count == 1 && cutoff_kept_exact_state,
                "cutoff partition publishes the exact pre-future QuoteState")) return false;

    std::vector<TickSourceResult> unbounded_dry_run_results;
    unbounded_dry_run_results.push_back(loop_source_result);
    RuntimeLoop unbounded_dry_run_loop(
        dry_run_config,
        std::make_unique<FakeTickSource>(std::move(unbounded_dry_run_results)),
        null_redis_executor,
        null_td_executor_for_runtime
    );
    const RuntimeLoopStats unbounded_dry_run_stats = unbounded_dry_run_loop.run();
    if (!expect(!unbounded_dry_run_stats.ok, "unbounded live dry-run is rejected")) return false;

    TDengineV2Writer td_writer(config);
    if (!expect(td_writer.initialize(), "td writer initialize")) return false;
    const std::string tick_sql = td_writer.build_stock_tick_insert_sql(batch3);
    if (!expect(tick_sql.find("stock_tick_v2") != std::string::npos, "stock tick sql")) return false;
    TickBatch td_filter_batch;
    td_filter_batch.logical_ts_ms = ts_0922;
    td_filter_batch.ticks.push_back(make_tick("000001", ts_0922, 4179952, 1000000000, "sh"));
    td_filter_batch.ticks.push_back(make_tick("000001", ts_0922, 11300, 200000000, "sz"));
    const std::string market_tick_sql = td_writer.build_stock_tick_insert_sql(td_filter_batch);
    if (!expect(market_tick_sql.find("t2_s_000001") != std::string::npos &&
                market_tick_sql.find("4179952") == std::string::npos,
                "td stock tick skips sh index and keeps sz stock")) return false;
    const auto schema_statements = td_writer.build_schema_statements();
    if (!expect(schema_statements.size() == 3, "td schema statements")) return false;
    NullTDengineCommandExecutor td_executor;
    if (!expect(td_executor.execute(schema_statements).statement_count == 3, "null td executor")) return false;
    const auto first_batch_td = td_writer.build_batch_statements(
        batch3,
        engine.quote_store(),
        stats3.snapshot_trigger,
        ts_0922
    );
    if (!expect(first_batch_td.size() >= 4, "first td batch prepends schema")) return false;
    if (!expect(first_batch_td.front().find("CREATE STABLE IF NOT EXISTS stock_tick_v2") != std::string::npos,
                "first td batch starts with stock schema")) return false;
    const auto second_batch_td = td_writer.build_batch_statements(
        batch3,
        engine.quote_store(),
        stats3.snapshot_trigger,
        ts_0922
    );
    if (!expect(!second_batch_td.empty() && second_batch_td.front().find("CREATE ") == std::string::npos,
                "second td batch skips schema")) return false;
#if defined(T1_V2_ENABLE_TDENGINE)
    TaosTDengineCommandExecutor taos_executor(config);
    const TDengineExecutionResult empty_td_result = taos_executor.execute({});
    if (!expect(empty_td_result.ok && empty_td_result.statement_count == 0, "taos empty execute")) return false;
    if (!expect(!taos_executor.is_connected(), "taos empty execute does not connect")) return false;
#endif
    const std::string summary_sql = td_writer.build_auction_summary_insert_sql(
        engine.quote_store(),
        stats3.snapshot_trigger,
        ts_0922
    );
    if (!expect(summary_sql.empty(), "no anchor summary before trigger")) return false;
    const std::string missing_anchor_sql = td_writer.build_auction_snapshot_insert_sql(
        missing_anchor_store,
        missing_anchor_trigger,
        ts_0922
    );
    if (!expect(missing_anchor_sql.find(",NULL,NULL,") != std::string::npos,
                "td formal snapshot preserves missing price and change as NULL")) return false;

    engine.shutdown();
    std::cout << "t1_v2 self-test passed" << std::endl;
    return true;
}

}  // namespace t1_v2
