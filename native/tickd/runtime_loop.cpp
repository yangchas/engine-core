#include "runtime_loop.h"

#include <algorithm>
#include <chrono>
#include <fstream>
#include <iostream>
#include <sstream>
#include <thread>

#include "runtime_log.h"

namespace t1_v2 {
namespace {

int64_t system_wall_clock_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()
    ).count();
}

std::string build_progress_line(const RuntimeLoopStats& stats) {
    const int64_t wall_lag_ms = stats.last_batch_logical_ts_ms > 0
        ? std::max<int64_t>(0, system_wall_clock_ms() - stats.last_batch_logical_ts_ms)
        : 0;
    std::ostringstream line;
    line << runtime_log_ts()
         << " | t1_v2 progress"
         << " | batches=" << stats.batches
         << " | source_in=" << stats.source_input
         << " | source_reject=" << stats.source_rejected
         << " | ack=" << stats.source_acks
         << " | ack_fail=" << stats.source_ack_failures
         << " | reject=" << stats.source_rejects
         << " | ticks=" << stats.ticks
         << " | redis_cmds=" << stats.redis_commands
         << " | td_sql=" << stats.tdengine_statements
         << " | redis_committed=" << stats.redis_committed_quotes
         << " | last_in=" << stats.last_batch_source_input
         << " | last_reject=" << stats.last_batch_source_rejected
         << " | last_ticks=" << stats.last_batch_ticks
         << " | pipeline_ms=" << stats.last_batch_pipeline_ms
         << " | commit_ms=" << stats.last_batch_commit_ms
         << " | ack_ms=" << stats.last_batch_ack_ms
         << " | last_ts_ms=" << stats.last_batch_logical_ts_ms
         << " | wall_lag_ms=" << wall_lag_ms;
    return line.str();
}

void emit_progress_line(const ConfigV2& config, const RuntimeLoopStats& stats) {
    const std::string line = build_progress_line(stats);
    if (config.logging.verbose) {
        std::cout << line << std::endl;
    }
    if (config.logging.enable_file_log && !config.logging.file_path.empty()) {
        std::ofstream output(config.logging.file_path, std::ios::out | std::ios::app);
        if (output) {
            output << line << '\n';
        }
    }
}

}  // namespace

RuntimeLoop::RuntimeLoop(
    const ConfigV2& config,
    std::unique_ptr<ITickSource> source,
    IRedisCommandExecutor& redis_executor,
    ITDengineCommandExecutor& tdengine_executor,
    RuntimeLoopOptions options
) : config_(config),
    source_(std::move(source)),
    pipeline_(config),
    coordinator_(redis_executor, tdengine_executor),
    options_(options) {}

RuntimeLoopStats RuntimeLoop::run() {
    RuntimeLoopStats stats;
    uint32_t consecutive_failures = 0;
    bool has_live_tick = false;
    int64_t last_control_clock_ms = 0;
    int64_t last_progress_logical_ts_ms = 0;
    // Tracks successfully committed logical progress.  It lets the runtime
    // insert the formal cutoff control event before a later market-data batch
    // can overwrite QuoteStateStore's last-known values.
    int64_t last_committed_logical_ts_ms = 0;
    bool boot_preflight_ok = false;
    bool boot_source_ok = false;
    if (!source_) {
        stats.ok = false;
        stats.failure_stage = "bootstrap.null_source";
        stats.error = "tick source is null";
        return stats;
    }
    if (config_.runtime_mode == RuntimeMode::Live && config_.processing.dry_run && !has_runtime_bound()) {
        stats.ok = false;
        stats.failure_stage = "bootstrap.invalid_live_dry_run";
        stats.error = "live dry-run requires --max-batches or --max-empty-polls";
        return stats;
    }
    if (!pipeline_.initialize()) {
        stats.ok = false;
        stats.failure_stage = "bootstrap.pipeline_initialize";
        stats.error = "runtime pipeline initialize failed";
        return stats;
    }
    const RuntimePreflightResult preflight = coordinator_.preflight(
        should_preflight_redis(),
        should_preflight_tdengine()
    );
    boot_preflight_ok = preflight.ok;
    if (!preflight.ok) {
        if (!should_tolerate_runtime_failure()) {
            pipeline_.shutdown();
            stats.ok = false;
            stats.failure_stage = "bootstrap.preflight";
            stats.error = preflight.error.empty() ? "runtime preflight failed" : preflight.error;
            return stats;
        }
        log_transient_failure(
            "bootstrap.preflight",
            preflight.error.empty() ? "runtime preflight failed" : preflight.error,
            ++consecutive_failures
        );
    }
    if (!source_->start()) {
        const std::string source_error = source_->error_message();
        if (!should_tolerate_runtime_failure()) {
            pipeline_.shutdown();
            stats.ok = false;
            stats.failure_stage = "bootstrap.source_start";
            stats.source_error = source_error;
            stats.error = source_error.empty() ? "tick source is not ready" : source_error;
            return stats;
        }
        log_transient_failure(
            "bootstrap.source_start",
            source_error.empty() ? "tick source is not ready" : source_error,
            ++consecutive_failures
        );
    } else {
        boot_source_ok = true;
    }
    if (config_.logging.verbose) {
        const bool local_q2frame_replay = config_.runtime_mode == RuntimeMode::Replay &&
            !config_.replay.q2frame_path.empty();
        std::cout << runtime_log_ts() << " | t1_v2 preflight | mode="
                  << (config_.runtime_mode == RuntimeMode::Replay ? "replay" : "live")
                  << " | redis=" << (local_q2frame_replay ? "skip" : (
                        preflight.redis_checked
                            ? (boot_preflight_ok || preflight.redis.ok ? "ok" : "degraded")
                            : "skip"
                     ))
                  << " | q2frame=" << (local_q2frame_replay
                        ? (boot_preflight_ok ? "ok" : "degraded")
                        : "skip")
                  << " | tdengine=" << (
                        preflight.tdengine_checked
                            ? (boot_preflight_ok || preflight.tdengine.ok ? "ok" : "degraded")
                            : "skip"
                     )
                  << " | source=" << (boot_source_ok ? "ok" : "degraded")
                  << std::endl;
    }

    while (true) {
        // RabbitMQ consume timeouts are data-plane Empty results.  A live
        // session still needs a control-plane clock so second-boundary
        // barriers (09:25:06 and the 09:32:10 cutoff) do not depend on a
        // coincident market tick.  Do not advance before the first live tick:
        // a restarted process must not publish an empty snapshot.
        if (config_.runtime_mode == RuntimeMode::Live && has_live_tick) {
            const int64_t wall_now_ms = control_wall_clock_ms();
            if (wall_now_ms > 0 &&
                (last_control_clock_ms <= 0 || wall_now_ms - last_control_clock_ms >= 250)) {
                last_control_clock_ms = wall_now_ms;
                RuntimePipelineResult clock_result = pipeline_.process_clock(
                    wall_now_ms,
                    static_cast<uint32_t>(stats.clock_events + 1)
                );
                stats.last_clock_logical_ts_ms = clock_result.runtime_stats.logical_ts_ms;
                stats.last_batch_redis_commands = clock_result.redis_commands.size();
                stats.last_batch_tdengine_statements = clock_result.tdengine_statements.size();
                RuntimeExecutionResult clock_execution = coordinator_.execute_and_commit(
                    pipeline_,
                    clock_result
                );
                if (!clock_execution.ok) {
                    const std::string execution_error = clock_execution.tdengine.ok
                        ? clock_execution.redis.error
                        : clock_execution.tdengine.error;
                    if (!should_tolerate_runtime_failure()) {
                        stats.ok = false;
                        stats.failure_stage = clock_execution.tdengine.ok
                            ? "commit.redis.live_clock"
                            : "commit.tdengine.live_clock";
                        stats.error = execution_error.empty()
                            ? "live clock runtime execution failed"
                            : execution_error;
                        break;
                    }
                    log_transient_failure(
                        clock_execution.tdengine.ok ? "commit.redis.live_clock" : "commit.tdengine.live_clock",
                        execution_error.empty() ? "live clock runtime execution failed" : execution_error,
                        ++consecutive_failures
                    );
                    try_full_recovery(consecutive_failures);
                    sleep_after_failure(consecutive_failures);
                } else {
                    ++stats.clock_events;
                    stats.redis_commands += clock_result.redis_commands.size();
                    stats.tdengine_statements += clock_result.tdengine_statements.size();
                    last_committed_logical_ts_ms = std::max(
                        last_committed_logical_ts_ms,
                        clock_result.runtime_stats.logical_ts_ms
                    );
                    consecutive_failures = 0;
                }
            }
        }
        TickSourceResult source_result = source_->next_batch();
        if (source_result.status == TickSourceStatus::EndOfStream) {
            break;
        }
        if (source_result.status == TickSourceStatus::Skipped) {
            stats.source_input += source_result.source_stats.input_count;
            stats.source_rejected += source_result.source_stats.rejected_count;
            if (source_result.requires_ack) {
                if (!should_ack_source()) {
                    ++stats.source_ack_skipped;
                } else if (source_->reject(source_result, false)) {
                    ++stats.source_rejects;
                } else {
                    ++stats.source_reject_failures;
                    if (!should_tolerate_runtime_failure()) {
                        stats.ok = false;
                        stats.failure_stage = "source.reject_after_skip";
                        stats.source_error = source_->error_message();
                        stats.error = "source reject failed";
                        break;
                    }
                    log_transient_failure(
                        "source.reject_after_skip",
                        source_->error_message().empty() ? "source reject failed" : source_->error_message(),
                        ++consecutive_failures
                    );
                    sleep_after_failure(consecutive_failures);
                    continue;
                }
            }
            consecutive_failures = 0;
            continue;
        }
        if (source_result.status == TickSourceStatus::Clock) {
            const uint32_t clock_seq_no = source_result.batch.seq_no;
            RuntimePipelineResult clock_result = pipeline_.process_clock(
                source_result.batch.logical_ts_ms,
                clock_seq_no
            );
            stats.last_clock_logical_ts_ms = clock_result.runtime_stats.logical_ts_ms;
            stats.last_batch_redis_commands = clock_result.redis_commands.size();
            stats.last_batch_tdengine_statements = clock_result.tdengine_statements.size();
            RuntimeExecutionResult execution_result = coordinator_.execute_and_commit(pipeline_, clock_result);
            if (!execution_result.ok) {
                stats.ok = false;
                stats.failure_stage = execution_result.tdengine.ok ? "commit.redis.clock" : "commit.tdengine.clock";
                stats.error = execution_result.tdengine.ok ? execution_result.redis.error : execution_result.tdengine.error;
                if (stats.error.empty()) {
                    stats.error = "clock runtime execution failed";
                }
                break;
            }
            ++stats.clock_events;
            stats.redis_commands += clock_result.redis_commands.size();
            stats.tdengine_statements += clock_result.tdengine_statements.size();
            last_committed_logical_ts_ms = std::max(
                last_committed_logical_ts_ms,
                clock_result.runtime_stats.logical_ts_ms
            );
            continue;
        }
        if (source_result.status == TickSourceStatus::Empty) {
            ++stats.empty_polls;
            if (should_stop_after_empty(stats)) {
                break;
            }
            sleep_after_empty();
            continue;
        }
        if (source_result.status == TickSourceStatus::Error) {
            ++stats.source_errors;
            if (source_result.requires_ack) {
                if (!should_ack_source()) {
                    ++stats.source_ack_skipped;
                } else if (source_->reject(source_result, source_result.requeue_on_error)) {
                    ++stats.source_rejects;
                } else {
                    ++stats.source_reject_failures;
                }
            }
            if (!should_tolerate_runtime_failure()) {
                stats.ok = false;
                stats.failure_stage = "source.batch_error";
                stats.source_error = source_->error_message();
                stats.error = source_result.error_msg ? source_result.error_msg : "tick source error";
                break;
            }
            log_transient_failure(
                "source.batch_error",
                source_result.error_msg ? source_result.error_msg : (source_->error_message().empty() ? "tick source error" : source_->error_message()),
                ++consecutive_failures
            );
            try_full_recovery(consecutive_failures);
            sleep_after_failure(consecutive_failures);
            continue;
        }

        // QuoteStateStore intentionally keeps only each symbol's newest
        // state.  A source message may span 09:32:10, so preserve every record
        // at or before the barrier before later records can overwrite it.  The
        // split is control-plane only: RawTick values and source acknowledgement
        // semantics remain unchanged.
        const int64_t batch_cutoff_ts_ms = opening_cutoff_timestamp_ms(source_result.batch.logical_ts_ms);
        const int64_t previous_cutoff_ts_ms = opening_cutoff_timestamp_ms(last_committed_logical_ts_ms);
        const bool crosses_opening_cutoff =
            batch_cutoff_ts_ms > 0 &&
            previous_cutoff_ts_ms == batch_cutoff_ts_ms &&
            last_committed_logical_ts_ms < batch_cutoff_ts_ms &&
            source_result.batch.logical_ts_ms > batch_cutoff_ts_ms;
        RuntimePipelineResult batch_result;
        RuntimeExecutionResult execution_result;
        uint64_t source_batch_ticks = 0;
        uint64_t source_batch_redis_commands = 0;
        uint64_t source_batch_tdengine_statements = 0;
        uint64_t source_batch_redis_committed_quotes = 0;
        uint64_t source_batch_pipeline_ms = 0;
        uint64_t source_batch_commit_ms = 0;
        uint64_t source_batch_ack_ms = 0;
        auto process_source_segment = [&](const TickBatch& segment, const SourceTickBatchBuildStats& segment_stats) {
            const auto pipeline_started_at = std::chrono::steady_clock::now();
            batch_result = pipeline_.process_batch(segment, segment_stats);
            source_batch_pipeline_ms += static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - pipeline_started_at
                ).count()
            );
            source_batch_ticks += batch_result.engine_stats.tick_count;
            source_batch_redis_commands += batch_result.redis_commands.size();
            source_batch_tdengine_statements += batch_result.tdengine_statements.size();
            const auto commit_started_at = std::chrono::steady_clock::now();
            execution_result = coordinator_.execute_and_commit(pipeline_, batch_result);
            source_batch_commit_ms += static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - commit_started_at
                ).count()
            );
            source_batch_redis_committed_quotes += static_cast<uint64_t>(execution_result.redis_committed_quotes);
            if (execution_result.ok) {
                last_committed_logical_ts_ms = std::max(
                    last_committed_logical_ts_ms,
                    batch_result.runtime_stats.logical_ts_ms
                );
            }
            return execution_result.ok;
        };

        bool source_batch_ok = true;
        if (crosses_opening_cutoff) {
            TickBatch before_cutoff = source_result.batch;
            TickBatch after_cutoff = source_result.batch;
            before_cutoff.ticks.clear();
            after_cutoff.ticks.clear();
            for (const RawTick& tick : source_result.batch.ticks) {
                if (tick.ts_ms > 0 && tick.ts_ms <= batch_cutoff_ts_ms) {
                    before_cutoff.ticks.push_back(tick);
                } else {
                    after_cutoff.ticks.push_back(tick);
                }
            }

            if (!before_cutoff.ticks.empty()) {
                before_cutoff.logical_ts_ms = batch_cutoff_ts_ms;
                before_cutoff.wall_ts_ms = batch_cutoff_ts_ms;
                source_batch_ok = process_source_segment(before_cutoff, source_result.source_stats);
            } else {
                RuntimePipelineResult cutoff_result = pipeline_.process_clock(
                    batch_cutoff_ts_ms,
                    static_cast<uint32_t>(stats.clock_events + 1)
                );
                stats.last_clock_logical_ts_ms = cutoff_result.runtime_stats.logical_ts_ms;
                RuntimeExecutionResult cutoff_execution = coordinator_.execute_and_commit(pipeline_, cutoff_result);
                if (!cutoff_execution.ok) {
                    execution_result = std::move(cutoff_execution);
                    source_batch_ok = false;
                } else {
                    ++stats.clock_events;
                    stats.redis_commands += cutoff_result.redis_commands.size();
                    stats.tdengine_statements += cutoff_result.tdengine_statements.size();
                    last_committed_logical_ts_ms = std::max(
                        last_committed_logical_ts_ms,
                        cutoff_result.runtime_stats.logical_ts_ms
                    );
                }
            }
            if (source_batch_ok && !after_cutoff.ticks.empty()) {
                const SourceTickBatchBuildStats empty_source_stats;
                source_batch_ok = process_source_segment(after_cutoff, empty_source_stats);
            }
        } else {
            source_batch_ok = process_source_segment(source_result.batch, source_result.source_stats);
        }

        if (!source_batch_ok) {
            if (source_result.requires_ack) {
                if (!should_ack_source()) {
                    ++stats.source_ack_skipped;
                } else if (source_->reject(source_result, source_result.requeue_on_error)) {
                    ++stats.source_rejects;
                } else {
                    ++stats.source_reject_failures;
                }
            }
            const std::string execution_error = execution_result.tdengine.ok
                ? execution_result.redis.error
                : execution_result.tdengine.error;
            if (!should_tolerate_runtime_failure()) {
                stats.ok = false;
                stats.failure_stage = execution_result.tdengine.ok ? "commit.redis" : "commit.tdengine";
                stats.source_error = source_->error_message();
                stats.error = execution_error;
                if (stats.error.empty()) {
                    stats.error = "runtime execution failed";
                }
                break;
            }
            log_transient_failure(
                execution_result.tdengine.ok ? "commit.redis" : "commit.tdengine",
                execution_error.empty() ? "runtime execution failed" : execution_error,
                ++consecutive_failures
            );
            try_full_recovery(consecutive_failures);
            sleep_after_failure(consecutive_failures);
            continue;
        }
        stats.last_batch_source_input = source_result.source_stats.input_count;
        stats.last_batch_source_rejected = source_result.source_stats.rejected_count;
        stats.last_batch_ticks = source_batch_ticks;
        stats.max_batch_ticks = std::max(stats.max_batch_ticks, stats.last_batch_ticks);
        stats.last_batch_redis_commands = source_batch_redis_commands;
        stats.last_batch_tdengine_statements = source_batch_tdengine_statements;
        stats.last_batch_pipeline_ms = source_batch_pipeline_ms;
        stats.last_batch_commit_ms = source_batch_commit_ms;
        stats.last_batch_logical_ts_ms = source_result.batch.logical_ts_ms;
        if (source_result.requires_ack) {
            if (!should_ack_source()) {
                ++stats.source_ack_skipped;
            } else {
                const auto ack_started_at = std::chrono::steady_clock::now();
                const bool ack_ok = source_->ack(source_result);
                source_batch_ack_ms += static_cast<uint64_t>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::steady_clock::now() - ack_started_at
                    ).count()
                );
                if (ack_ok) {
                    ++stats.source_acks;
                } else {
                    ++stats.source_ack_failures;
                    if (!should_tolerate_runtime_failure()) {
                        stats.ok = false;
                        stats.failure_stage = "source.ack";
                        stats.source_error = source_->error_message();
                        stats.error = "source ack failed";
                        break;
                    }
                    log_transient_failure(
                        "source.ack",
                        source_->error_message().empty() ? "source ack failed" : source_->error_message(),
                        ++consecutive_failures
                    );
                    try_full_recovery(consecutive_failures);
                    sleep_after_failure(consecutive_failures);
                    continue;
                }
            }
        }
        stats.last_batch_ack_ms = source_batch_ack_ms;

        if (consecutive_failures > 0 && config_.logging.verbose) {
            std::cout << runtime_log_ts()
                      << " | t1_v2 recovered"
                      << " | after_failures=" << consecutive_failures
                      << " | batches=" << stats.batches + 1
                      << std::endl;
        }
        consecutive_failures = 0;
        ++stats.batches;
        if (config_.runtime_mode == RuntimeMode::Live) {
            has_live_tick = true;
        }
        stats.source_input += source_result.source_stats.input_count;
        stats.source_rejected += source_result.source_stats.rejected_count;
        stats.ticks += source_batch_ticks;
        stats.redis_commands += source_batch_redis_commands;
        stats.tdengine_statements += source_batch_tdengine_statements;
        stats.redis_committed_quotes += source_batch_redis_committed_quotes;
        const int64_t report_interval_ms = static_cast<int64_t>(
            config_.processing.report_interval_seconds
        ) * 1000;
        if ((config_.logging.verbose || config_.logging.enable_file_log) &&
            report_interval_ms > 0 &&
            (last_progress_logical_ts_ms <= 0 ||
             stats.last_batch_logical_ts_ms - last_progress_logical_ts_ms >= report_interval_ms)) {
            emit_progress_line(config_, stats);
            last_progress_logical_ts_ms = stats.last_batch_logical_ts_ms;
        }
        if (options_.max_batches > 0 && stats.batches >= options_.max_batches) {
            break;
        }
    }

    source_->stop();
    pipeline_.shutdown();
    return stats;
}

int64_t RuntimeLoop::control_wall_clock_ms() const {
    if (options_.wall_clock_ms) {
        return options_.wall_clock_ms();
    }
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()
    ).count();
}

bool RuntimeLoop::should_preflight_redis() const {
    if (config_.processing.dry_run) {
        return false;
    }
    // A Q2Frame replay must preflight its local file executor, while keeping
    // external Redis writes disabled.
    return config_.runtime_mode != RuntimeMode::Replay ||
        config_.replay.write_redis || !config_.replay.q2frame_path.empty();
}

bool RuntimeLoop::should_preflight_tdengine() const {
    if (config_.processing.dry_run) {
        return false;
    }
    return config_.runtime_mode != RuntimeMode::Replay || config_.replay.write_tdengine;
}

bool RuntimeLoop::should_stop_after_empty(RuntimeLoopStats& stats) const {
    return options_.max_empty_polls > 0 && stats.empty_polls >= options_.max_empty_polls;
}

bool RuntimeLoop::should_ack_source() const {
    return !config_.processing.dry_run || config_.processing.ack_in_dry_run;
}

bool RuntimeLoop::has_runtime_bound() const {
    return options_.max_batches > 0 || options_.max_empty_polls > 0 ||
        config_.processing.max_batches > 0 || config_.processing.max_empty_polls > 0;
}

bool RuntimeLoop::should_tolerate_runtime_failure() const {
    return config_.runtime_mode == RuntimeMode::Live;
}

void RuntimeLoop::sleep_after_empty() const {
    if (!config_.processing.enable_rate_limiting || config_.processing.processing_delay_ms <= 0) {
        return;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(config_.processing.processing_delay_ms));
}

void RuntimeLoop::sleep_after_failure(uint32_t consecutive_failures) const {
    int base_delay_ms = config_.processing.retry_delay_ms > 0 ? config_.processing.retry_delay_ms : 1000;
    const uint32_t multiplier = std::min<uint32_t>(consecutive_failures == 0 ? 1 : consecutive_failures, 5);
    std::this_thread::sleep_for(std::chrono::milliseconds(base_delay_ms * static_cast<int>(multiplier)));
}

void RuntimeLoop::log_transient_failure(const char* stage, const std::string& error, uint32_t consecutive_failures) const {
    std::cerr << runtime_log_ts()
              << " | t1_v2 transient"
              << " | stage=" << (stage ? stage : "-")
              << " | consecutive=" << consecutive_failures
              << " | error=" << (error.empty() ? "-" : error)
              << std::endl;
}

uint32_t RuntimeLoop::transient_reset_threshold() const {
    return static_cast<uint32_t>(
        config_.processing.transient_failures_before_reset > 0
            ? config_.processing.transient_failures_before_reset
            : 3
    );
}

void RuntimeLoop::try_full_recovery(uint32_t consecutive_failures) {
    if (consecutive_failures < transient_reset_threshold()) {
        return;
    }
    const RuntimePreflightResult recheck = coordinator_.recheck_after_reset(
        should_preflight_redis(),
        should_preflight_tdengine()
    );
    std::cerr << runtime_log_ts()
              << " | t1_v2 reset"
              << " | threshold=" << transient_reset_threshold()
              << " | consecutive=" << consecutive_failures
              << " | redis=" << (recheck.redis_checked ? (recheck.redis.ok ? "ok" : "fail") : "skip")
              << " | tdengine=" << (recheck.tdengine_checked ? (recheck.tdengine.ok ? "ok" : "fail") : "skip")
              << " | error=" << (recheck.error.empty() ? "-" : recheck.error)
              << std::endl;
    source_->stop();
    const bool restarted = source_->start();
    std::cerr << runtime_log_ts()
              << " | t1_v2 source_restart"
              << " | ok=" << (restarted ? "yes" : "no")
              << " | error=" << (source_->error_message().empty() ? "-" : source_->error_message())
              << std::endl;
}

}  // namespace t1_v2
