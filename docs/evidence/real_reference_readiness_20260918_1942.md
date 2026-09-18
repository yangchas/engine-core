# Real reference-data readiness probe — 2026-09-18 19:42 CST

## Scope

The existing bounded Core runner read the real TD daily-kline path and Redis
reference caches for two symbols. It used the production calendar snapshot and
the existing provider query paths. The run was read-only: no Redis/TD writes,
no repair, no network fallback, no Rabbit consumer/ACK, and no effect.

## Observed values

```text
trade_date: 2026-09-18
previous_trade_date: 2026-09-17
symbols: 000338, 600519
calendar semantic hash: a64a5dfa7a9e256c500799c2fa646f9e92b0a3d21d9b45c9f68d01ee1655610e
Q2 records: 5224 / 5224
Q2 coverage: 1.0
Q2 stale: 5224
Q2 status: STALE
```

The provider paths returned real rows and cache metadata:

```text
TD daily_kline rows: 2
previous_day_limit_pool rows: 47
hot_plates rows: 50
```

However, the reference sources have no verified historical `available_at_ms`.
The runner therefore returned:

```text
previous_day_stats: UNAVAILABLE
previous_day_limit_pool: UNAVAILABLE
hot_plates: UNAVAILABLE
temporal_historical_proof: UNAVAILABLE
temporal_live_readiness: PARTIAL
overall readiness: PARTIAL
actions: REFRESH_Q2, PREFETCH:previous_day_stats,
         PREFETCH:previous_day_limit_pool, PREFETCH:hot_plates
```

This is a correct fail-closed result for a post-market probe. The existence of
rows and a current `observed_at` does not prove that the values were known at a
historical opening cutoff.

## Decision

```text
REAL_TD_REFERENCE_READ_PATH       PASS
REAL_REDIS_REFERENCE_READ_PATH    PASS
TEMPORAL_HISTORICAL_PROOF         UNAVAILABLE
LIVE_PREFETCH_READINESS           PARTIAL (must be captured before node)
CORE_REPLACEMENT                  NOT READY
```

For the next live session, prefetch must be captured before the node cutoff
and then bound to the Engine evaluation. The current post-market result must
not be promoted to a 09:20 runtime input.
