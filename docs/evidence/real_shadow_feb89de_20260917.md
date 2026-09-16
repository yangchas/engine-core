# Real read-only shadow evidence: `feb89de`

## Engine auction path

On cobra-ion, the exact archive for commit `feb89de` queried TD
`market_data1.auction_snapshot_v2` through the existing read-only `taos`
path for `2026-09-16 / 600519`. The three auction anchors were submitted to
the public Engine signal path. The result was:

```text
processed_signals       = 6
strategy_result_count   = 3
engine_fact_status      = PARTIAL
engine_fact_only        = true
semantic_hash_equal     = true
read_only               = true
```

The source record times were preserved independently for 09:20, 09:24 and
09:25. The output artifact is retained locally at
`tmp/real-auction-engine-shadow-20260916-feb89de.json` with SHA-256
`374679d5b0276e236ea4f2e0df0c1544bd9f1ada6c000ad115f520b8f863887a`.

## Reference readiness path

At 2026-09-17 01:26 CST, the exact archive ran the real Redis/TD reference
readiness probe for the current trade date and bounded symbols
`000001,000002,600519`. The probe observed:

```text
Q2                    = MISSING (premarket; no active cohort)
previous_day_stats    = UNAVAILABLE (availability metadata unknown)
previous_day_limit    = UNAVAILABLE (availability metadata unknown)
hot_plates            = MISSING
readiness              = BLOCKED / WAIT_FOR_Q2
```

The TD read was empty, so the existing Redis kline view was selected by the
documented source-selection rule. No historical `available_at_ms` was
invented. The output artifact is retained locally at
`tmp/real-reference-readiness-20260917-feb89de.json` with SHA-256
`4e5e7b617a0e54d1d15a5ca281ecadd32766ec65d69c2e7c8780da7283ec37d1`.

## Prepared-reference Engine binding

Using the same read-only Redis/TD access paths and the exact archive, a
bounded 2026-09-16 run passed the prepared reference context into the public
Engine. All three reference results were truthfully `UNAVAILABLE` (the source
availability metadata is absent), but the Engine still accepted the explicit
degraded results through three owned `DATA_READY` evaluations:

```text
reference_binding      = ENGINE_DATA_READY
reference_bundle_count = 3
processed_signals      = 9
strategy_result_count  = 3
engine_fact_status     = PARTIAL
semantic_hash_equal    = true
read_only              = true
```

The bounded output artifact is retained locally at
`tmp/real-reference-engine-shadow-20260916-a266fef.json` with SHA-256
`6a1ffc89f7981eea2c6703c00c03826a4dba6dde805e4d865caabee17e682b1b`.

## Safety

Both runs were isolated, read-only validation. `engine-next` and `t1-v2-live`
remain active with zero restarts (`MainPID=657653` and `2878024`). No Rabbit
consumer or ACK behavior changed; no Redis/TD write, repair, notification,
order or effect was performed.
