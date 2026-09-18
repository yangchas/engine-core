# Real live Core shadow — 2026-09-18 12:27–12:30 CST

## Scope

This is a read-only validation against the live cobra-ion Redis/TD services. It
does not restart `engine-next` or `t1-v2-live`, consume RabbitMQ, ACK messages,
write Redis/TD, repair caches, or emit notifications/effects.

Remote validation directory:

```text
/home/exedev/validation/engine-core-6511981-v1
```

Production services at capture time:

```text
engine-next  active, MainPID=1041872, NRestarts=0
t1-v2-live   active, MainPID=2878024, NRestarts=0
```

## Real Q2 observation

Command: `examples/run_live_q2_probe.py` with the deployed engine-next Python
environment and Redis read-only operations.

Artifact: `q2-live-20260918-1229.json`

```text
quote_count                 5224
row_coverage                1.0
missing_symbol_count        0
newest_source_lag_seconds   6676
stale_symbol_count          5224
status                      STALE
consistency                 BEST_EFFORT_STALE
same_observation_deterministic true
```

The source range and raw field diagnostics were preserved. `source_record_time_ms`
is used only for freshness/future-skew and range reporting; it is not treated as
Rabbit arrival time or exchange tick ordering.

The stale result is expected while t1-v2 catches up. It is a real-data safety
signal, not a Core failure and not permission to relax the freshness policy.

## Real reference-data readiness

Command: `examples/run_real_auction_reference_readiness.py`.

Artifact: `real-readiness-20260918-1227.json`

```text
trade_date                  2026-09-18
derived_previous_trade_date 2026-09-17
q2                          STALE, coverage=1.0, stale=5224
previous_day_stats          UNAVAILABLE
previous_day_limit_pool     UNAVAILABLE
hot_plates                  UNAVAILABLE
readiness                   PARTIAL / LUNCH_BREAK
```

The Redis payloads were present (50 hot-plate rows and 47 previous-session
limit-pool rows), but their historical `available_at_ms` is not proven. The
Core guard therefore keeps them `UNAVAILABLE`; `observed_at` is not used as a
replacement for `available_at`.

## Real Engine shadow

### Auction

Artifacts: `auction-engine-shadow-20260918-1230.json` for `600519` and
`auction-engine-shadow-20260918-1236-{000001,300750}.json` for two additional
bounded symbols.

```text
symbol   processed_signals   strategy_results   status    semantic_hash_equal
000001   6                    3                  PARTIAL   true
300750   6                    3                  PARTIAL   true
600519   6                    3                  PARTIAL   true
```

The 600519 direct/engine fact hash is
`3904bb7d4a331bdfff9e38c8d26f8d1871752954ec41c482f05bac5d3de9e276`; the
other two artifacts contain their own direct/engine matching hashes.

The public Engine path produces the same fact semantic hash as the direct
wheel. The three observed projection points are retained as separate
`0920/0924/0925` evidence; this does not claim that all reference data was
available at the historical cutoff.

### Opening

Artifact: `opening-engine-shadow-20260918-1230.json` for `600519`.

```text
processed_signals       2
projection_coverage     1.0
projection_status       STALE
stale_symbol_count      5224
fact_status              PARTIAL
read_only                true
```

The Core opening path is executable and deterministic, but the current Q2
observation is too stale for a production-equivalent opening claim.

## Startup readiness at 12:31

Artifact: `startup-readiness-20260918-1231.json`.

```text
phase          LUNCH_BREAK
q2_status      STALE
q2_coverage    1.0
due_timers     AUCTION_0926, OPENING_0932 (already elapsed)
actions        REFRESH_Q2 plus recovery dispatch markers
status         PARTIAL
```

The elapsed-node markers are recovery diagnostics from the bounded probe; they
do not re-run production timers and do not imply that a late shadow is a normal
09:26/09:32 execution.

## Current conclusion

```text
REAL_REDIS_READ_PATH          PASS (read-only)
REAL_TD_AUCTION_SHADOW        PASS (semantic parity, partial availability)
REFERENCE_TIME_SAFETY         PASS (unknown availability fails closed)
LIVE_Q2_FRESHNESS             WARN (source lag ~6676 s)
PRODUCTION_REPLACEMENT        NOT READY
```

The next migration step is not another provider or replay framework. It is a
bounded startup/node coordinator that reuses these already-verified adapters,
performs readiness checks before each node, and keeps `engine-next` as the
production owner until Q2 freshness and reference-data readiness are both
proven on a live session.
