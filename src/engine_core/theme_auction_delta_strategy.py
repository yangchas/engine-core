"""The first deliberately small Auction Shadow rule.

This is an exact, pure extraction of the deployed ``engine-next``
``_infer_snapshot_delta_signal`` rule.  It consumes only the explicit legacy
compatibility numeric fact; it does not read Redis/TD, infer missing values,
emit effects, or claim that the labels are the final Core strategy contract.
"""

from __future__ import annotations

import math


LEGACY_THEME_AUCTION_DELTA_STRATEGY_CONTRACT_VERSION = "LegacyThemeAuctionDeltaStrategyV1"


def infer_legacy_theme_delta_signal(
    *,
    amount_delta_24_25: float,
    bid_amount_delta_24_25: float,
    change_pct_delta_avg: float,
    amount_ratio_avg: float,
) -> str:
    """Return the legacy signal using the original precedence and thresholds.

    The caller must provide numeric values from the compatibility fact.  This
    function intentionally does not coerce missing values to zero; the
    compatibility builder owns that historical behavior before this rule is
    called.
    """

    values = {
        "amount_delta_24_25": amount_delta_24_25,
        "bid_amount_delta_24_25": bid_amount_delta_24_25,
        "change_pct_delta_avg": change_pct_delta_avg,
        "amount_ratio_avg": amount_ratio_avg,
    }
    for field_name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(field_name + " must be finite")

    # Keep the deployed order exactly: strong/retracing first, then bid,
    # cooling, mild expansion, and the fallback label.
    if amount_delta_24_25 >= 50_000_000 and change_pct_delta_avg >= 1.0:
        return "增量转强"
    if amount_delta_24_25 >= 50_000_000 and change_pct_delta_avg <= -2.0:
        return "放量回落"
    if bid_amount_delta_24_25 >= 10_000_000 and amount_delta_24_25 >= 0:
        return "封单增强"
    if amount_delta_24_25 <= -20_000_000:
        return "竞价降温"
    if amount_ratio_avg >= 1.5 and amount_delta_24_25 > 0:
        return "温和放量"
    return "平稳"


__all__ = [
    "LEGACY_THEME_AUCTION_DELTA_STRATEGY_CONTRACT_VERSION",
    "infer_legacy_theme_delta_signal",
]
