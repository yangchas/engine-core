#pragma once

#include <memory>

#include "config_v2.h"
#include "tick_source.h"

namespace tickd {

class TickSourceFactory {
public:
    static std::unique_ptr<ITickSource> create(const ConfigV2& config);
};

}  // namespace tickd
