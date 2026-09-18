# Live Q2 readiness evidence — 2026-09-18

## Scope

This is a read-only production observation on `cobra-ion`. It did not restart
`engine-next` or `t1-v2-live`, add a Rabbit consumer, ACK/publish Rabbit
messages, write Redis/TDengine, send notifications, or trigger effects.

Code identity: `49924709236c4fdf6568f294ad7eb858413957a5`.

## Storage incident and recovery

At approximately 12:07 CST `t1-v2-live` reported repeated
`stage=commit.tdengine | error=No enough disk space`. The exact cleanup target
was the top-level temporary validation archives under
`/home/exedev/validation/*.tar`: 125 files, approximately 336 MB. No TD,
Redis, service, or current validation directory was removed.

After cleanup, root filesystem availability increased from approximately 1.0
GB (95% used) to approximately 3.3 GB (82% used). `engine-next` and
`t1-v2-live` remained active. Subsequent t1 progress records showed
`ack_fail=0`, increasing `td_sql`/`redis_committed`, and no further disk-space
errors in the observed interval.

## Real Redis Q2 probe

Observation time: `2026-09-18T12:10:48+08:00`.

| Field | Result |
|---|---|
| expected symbols | 5224 |
| Q2 rows | 5224 |
| row coverage | 1.0 |
| missing symbols | 0 |
| status | `STALE` |
| consistency | `BEST_EFFORT_STALE` |
| stale symbols | 5224 |
| newest source record | `1789698048000` |
| deterministic repeat | `true` |
| Redis operations | one `SMEMBERS` plus 5224 `HGETALL` |

The probe preserved `source_record_time_ms`; it did not interpret that field as
Rabbit arrival time or exchange tick time. Because all rows were stale under
the 600-second policy, this observation is not a fresh opening-data acceptance.

## Startup readiness probe

Observation time: `2026-09-18T12:13:22+08:00`.

The real read-only `StartupReadiness` probe returned:

```text
status = PARTIAL
phase = LUNCH_BREAK
q2_status = STALE
q2_consistency_status = BEST_EFFORT_STALE
q2_coverage = 1.0
actions = REFRESH_Q2, DISPATCH_TIMER:AUCTION_0926, DISPATCH_TIMER:OPENING_0932
side_effect_boundary = Redis SMEMBERS/HGETALL only
```

The due timers are recovery/catch-up evidence after a late observation; they
are not evidence that the Core executed the original 09:26/09:32 production
cutoffs. Core therefore remains a read-only shadow and `engine-next` remains
the production owner.

## Decision

The storage blocker was mitigated without touching the production data chain.
The next replacement-gate blocker is still source freshness: Core must not
promote the stale Q2 observation to a fresh opening fact. A new live shadow is
appropriate only after `newest_source_time_ms` is within the configured
freshness policy.
