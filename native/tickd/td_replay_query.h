#pragma once

#include <cstdint>
#include <string>

#include "config_v2.h"

namespace tickd {

class TdReplayQueryBuilder {
public:
    explicit TdReplayQueryBuilder(const ConfigV2& config);

    int64_t align_to_tick_interval(int64_t ts_ms) const;
    std::string build_slice_query(int64_t slice_start_ms, int64_t slice_end_ms) const;

private:
    static std::string format_shanghai_timestamp(int64_t ts_ms);
    static bool is_safe_table_name(const std::string& table);

private:
    ConfigV2 config_;
};

}  // namespace tickd
