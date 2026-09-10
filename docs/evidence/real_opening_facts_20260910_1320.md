# Real Redis Q2 opening facts after SemanticHashV1 fix — 2026-09-10 13:20 CST

`engine_core` commit `130ba28` was executed on `cobra-ion` using the shared
Python 3.12 runtime against the production Redis Q2 projection.  The probe
used the existing `RedisQ2ProjectionAdapter` and only `SMEMBERS/HGETALL`.

```text
trade_date: 2026-09-10
selected_symbols: 000001, 300750, 600519
projection_status: READY
quote_count / expected: 5219 / 5219
coverage: 1.0
freshness: FRESH
stale_symbols: []
projection_consistency: BEST_EFFORT
read_only: true
```

The selected facts were all `status=available` with independent valid
`limit_state_status`.  Reported source time range:

```text
oldest_source_time_ms: 1788969600000
newest_source_time_ms: 1789017448000
```

The output now uses the versioned `SemanticHashV1` implementation:

```text
archive_sha256: 1390F1CCB9C2907610B8EEBCAE13460BA2DB344A70765FA5B2AE9154E0E67FC8
probe_output_sha256: 3269577FF222004C2E33EABE87331772ED3E2F2780AC4E3720314E14B4FC8FC2
semantic_hash: 28afb1ead499d438e0a996c691719652bbd5396e8c525c40dd509347ac407276
hash_contract: SemanticHashV1
```

No Redis/TD write, Rabbit consume/ACK, repair, notification, or effect was
performed.  This confirms the live Redis input path and opening fact wheel;
it does not turn the rolling Q2 cohort into an immutable historical snapshot
or prove Rabbit batch membership.
