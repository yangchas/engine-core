#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>

#include "runtime_execution_coordinator.h"
#include "runtime_pipeline.h"
#include "tick_source.h"

namespace tickd {

struct RuntimeLoopOptions {
    // 0 means unlimited. Tests can set this to prevent an accidental busy loop.
    uint32_t max_empty_polls = 0;
    uint32_t max_batches = 0;
    // Optional wall-clock source for the live control-plane clock.  Replay
    // supplies Clock events from its historical source; live uses this hook
    // only to advance time when RabbitMQ has no market-data message.
    std::function<int64_t()> wall_clock_ms;
};

struct RuntimeLoopStats {
    bool ok = true;
    std::string error;
    std::string failure_stage;
    std::string source_error;
    uint32_t batches = 0;
    uint32_t empty_polls = 0;
    uint32_t source_errors = 0;
    uint32_t source_acks = 0;
    uint32_t source_rejects = 0;
    uint32_t source_ack_failures = 0;
    uint32_t source_reject_failures = 0;
    uint32_t source_ack_skipped = 0;
    uint32_t clock_events = 0;
    uint64_t source_input = 0;
    uint64_t source_rejected = 0;
    uint64_t ticks = 0;
    uint64_t redis_commands = 0;
    uint64_t tdengine_statements = 0;
    uint64_t redis_committed_quotes = 0;
    // In replay mode each processed batch is one complete timestamp group.
    // This is diagnostic evidence only; it is not a production batch-size contract.
    uint64_t max_batch_ticks = 0;
    uint64_t last_batch_source_input = 0;
    uint64_t last_batch_source_rejected = 0;
    uint64_t last_batch_ticks = 0;
    uint64_t last_batch_redis_commands = 0;
    uint64_t last_batch_tdengine_statements = 0;
    uint64_t last_batch_pipeline_ms = 0;
    uint64_t last_batch_commit_ms = 0;
    uint64_t last_batch_ack_ms = 0;
    int64_t last_batch_logical_ts_ms = 0;
    int64_t last_clock_logical_ts_ms = 0;
};

class RuntimeLoop {
public:
    RuntimeLoop(
        const ConfigV2& config,
        std::unique_ptr<ITickSource> source,
        IRedisCommandExecutor& redis_executor,
        ITDengineCommandExecutor& tdengine_executor,
        RuntimeLoopOptions options = {}
    );

    RuntimeLoopStats run();

private:
    bool should_preflight_redis() const;
    bool should_preflight_tdengine() const;
    bool should_stop_after_empty(RuntimeLoopStats& stats) const;
    bool should_ack_source() const;
    bool has_runtime_bound() const;
    bool should_tolerate_runtime_failure() const;
    void sleep_after_empty() const;
    void sleep_after_failure(uint32_t consecutive_failures) const;
    void log_transient_failure(const char* stage, const std::string& error, uint32_t consecutive_failures) const;
    uint32_t transient_reset_threshold() const;
    void try_full_recovery(uint32_t consecutive_failures);
    int64_t control_wall_clock_ms() const;

private:
    ConfigV2 config_;
    std::unique_ptr<ITickSource> source_;
    RuntimePipeline pipeline_;
    RuntimeExecutionCoordinator coordinator_;
    RuntimeLoopOptions options_;
};

}  // namespace tickd
