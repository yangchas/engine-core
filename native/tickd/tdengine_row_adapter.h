#pragma once

#include <string>
#include <vector>

#include "td_replay_row_converter.h"

#if defined(TICKD_ENABLE_TDENGINE)
#include <taos.h>
#endif

namespace tickd {

struct TdengineRowAdapterResult {
    bool ok = false;
    bool unsupported = false;
    std::string error;
};

class TdengineRowAdapter {
public:
    static bool enabled();

#if defined(TICKD_ENABLE_TDENGINE)
    static std::vector<std::string> field_names(TAOS_FIELD* fields, int field_count);
    static TdengineRowAdapterResult cells_from_row(
        TAOS_ROW row,
        TAOS_FIELD* fields,
        int field_count,
        std::vector<TdReplayCell>& out
    );
#endif
};

}  // namespace tickd
