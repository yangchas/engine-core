#pragma once

#include <cstdint>

namespace tickd {

enum class RuntimeMode : uint8_t {
    Live = 0,
    Replay = 1,
};

enum class MarketPhase : uint8_t {
    Premarket = 0,
    Auction = 1,
    Intraday = 2,
    Postmarket = 3,
};

enum class LimitState : int8_t {
    Down = -1,
    Normal = 0,
    Up = 1,
};

}  // namespace tickd
