# Runtime/storage lag observation — 2026-09-18 14:34–14:38 CST

## Scope

Read-only operational evidence collected from `cobra-ion`. No service was
restarted, no Rabbit consumer or ACK behavior was changed, and no Redis/TD
write was issued by the Core validation commands.

## Production runtime

```text
t1-v2-live: active, MainPID=2878024
engine-next: active, MainPID=1041872
```

The `t1-v2-live` progress logs continued to show increasing `batches`,
`source_in`, `ticks`, `redis_committed`, and `ack`, with `ack_fail=0`.
However, repeated transient entries reported:

```text
stage=commit.tdengine
error=No enough disk space
```

At 14:33:37 the log still showed this error while the process remained active.
The progress lines around the same period reported `wall_lag_ms` around
1,562,000–1,627,000 ms (roughly 26–27 minutes).

## Real Q2 observation

The bounded read-only Core probe completed at 14:38 CST:

```text
artifact: q2-1434.json
SHA-256: 0F1E43992713AC02FE46B08CF0F7327EF3FE8DB77000DB1B8D4CC8DD1DF680F8
rows / expected: 5224 / 5224
coverage: 1.0
stale symbols: 5224
status: STALE
consistency: BEST_EFFORT_STALE
newest_source_time_ms: 1789711885000
newest_source_lag_seconds: 1615
same_observation_engine_deterministic: true
```

Coverage is therefore complete as a row read but not fresh enough for a live
decision. The Core freshness gate remains closed; no value was substituted and
no stale projection was promoted to READY.

## Storage evidence

```text
/dev/root: 19G total, 17G used, 1.2G available, 94%
Docker active volumes: about 7.05G
infra_tdengine-data: about 6.1G
TD market_data1: 135679 tables, keep=3650d
```

The large active TD volume is production data. It is not safe to delete or
prune it as part of a Core validation turn. The storage warning is a production
capacity incident requiring an explicitly approved retention/cleanup action,
not an engine_core code workaround.

## Decision

Core migration can continue in read-only Shadow, but current Q2 freshness and
TD write capacity are not replacement-ready. The next operational decision is
to inspect and approve a narrowly scoped storage-retention action (or add
capacity) before treating fresh Q2/TD evidence as available. No producer,
Rabbit, or TD retention change was made in this turn.
