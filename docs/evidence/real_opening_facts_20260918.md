# Real opening facts evidence — 2026-09-18

## Scope

The Core opening fact wheel read the live Redis Q2 projection on `cobra-ion`
for three bounded symbols: `000001`, `300750`, and `600519`. The command used
the existing `RedisQ2ProjectionAdapter` and only `SMEMBERS/HGETALL`; it did not
write Redis/TD, consume Rabbit, repair data, send notifications, or invoke
effects.

Artifact: `opening-facts-20260918-1220.json`  
SHA-256:
`e4633fe72cb4b63a4a767e690b009eec00984705620c1e2adba27520bb361684`.

## Result

```text
expected_symbol_count = 5224
quote_count = 5224
coverage = 1.0
missing_symbols = 0
projection_status = STALE
freshness_status = STALE_OR_MIXED
consistency = BEST_EFFORT_STALE
```

The selected symbols all produced normalized opening facts, including price,
previous close, two-minute amount, limit state, and source record time. The
fact wheel preserved `speed_1m` as unavailable because the observed source
unit is not proven equivalent to the wheel's generic speed unit. Source field
lineage and raw field names were retained.

The result is a successful real-consumer/read-path execution but **not** a
fresh opening acceptance: all Q2 rows were stale under the explicit 600-second
policy. The stale status was not downgraded merely because coverage was 1.0.

## Migration meaning

This closes a real Redis → Core opening-fact consumer path for a bounded
sample. It does not prove the 09:32 production cutoff, does not replace
`engine-next`, and does not justify replaying the current Q2 as historical
opening data. A fresh source-time observation is still required for the
production-shadow gate.
