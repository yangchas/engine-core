# 2026-09-10 opening read-only chain observation

## Scope and safety

This observation was collected on `cobra-ion` at approximately 09:35–09:36
Asia/Shanghai using the existing production Redis/TD clients and the deployed
`engine_next` release path.  It was strictly read-only:

```text
new Rabbit consumer       = 0
Rabbit ACK change         = 0
Redis writes              = 0
TD writes                 = 0
repair/backfill/recovery  = 0
notification/effect       = 0
service restart           = 0
```

Both `engine-next` and `t1-v2-live` were active with `NRestarts=0`; root disk
was 12G/19G used (68%).

## Real Redis Q2 → engine_core

The existing `RedisQ2ProjectionAdapter` read `q2:active:20260910` with one
`SMEMBERS` and 5,219 `HGETALL` operations:

```text
requested / received       = 5219 / 5219
coverage                   = 1.0
missing symbols            = 0
source phase               = 2 for all rows
newest source lag          = 137 seconds
stale symbols              = 13
projection status          = PARTIAL
consistency                = BEST_EFFORT_MIXED_FRESHNESS
same-observation hash      = true
```

The adapter preserved the oldest/newest source times and did not promote full
coverage to `READY` while 13 rows were stale.

## Redis auction projection ↔ TD auction_snapshot_v2

The bounded comparison used `000001`, `000002`, and `600519` for `0920`,
`0924`, and `0925`.  TD source timestamps and Redis snapshot timestamps matched
for every comparable row:

```text
field-level match (match amount)       = all comparable rows
field-level match (resting bid amount) = all comparable rows
timestamp mismatches                   = 0
value mismatches                       = 0
not comparable                         = 5
partial comparable                     = 4
```

The `0925` Redis anchor exposed amount and bid fields that matched TD for all
three samples.  The current Redis payload does not expose an ask field, and
`0920/0924` are Top-200 projections, so absent rows/ask values remain
`NOT_COMPARABLE`; they are not treated as zero or mismatch.

## Real engine_next read path

The existing release `20260903_e272842` was queried for the same three symbols
at `09:35:00`.  The read-only context probe returned three context rows and
three legacy fact rows:

```text
snapshot_count             = 3
future_source_timestamp    = false
latest_quote_age_seconds   = 84
guard_writes               = []
read_only                  = true
```

The legacy read path returned auction amounts for all three symbols.  Plate
text was intentionally left as observed bytes in the raw probe; this evidence
does not promote encoding or strategy labels to a new contract.

## Layered conclusion

```text
SOURCE_INGESTION            = OBSERVED
AUCTION_STATE               = OBSERVED (TD projection available)
STORAGE_SHARED_FIELDS       = PASS (amount/bid/timestamp samples)
ENGINE_NEXT_READ_PATH       = PASS (bounded read-only sample)
ENGINE_CORE_Q2_SHADOW       = PASS (deterministic, PARTIAL-aware)
JOINT_TRADING_DAY           = WARN
```

`WARN` remains correct because Redis Top-200/ask limitations and Rabbit/runtime
batch membership are not proven by these read-only observations.  No producer
or consumer change is authorized by this evidence.
