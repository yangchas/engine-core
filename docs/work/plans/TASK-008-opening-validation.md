# TASK-008 — Real opening validation (read-only)

## Scope

Validate the existing opening fact and public Engine shadow path with real
Redis Q2 observations. This is an evidence task, not a production migration:
it does not add a Redis writer, Rabbit consumer/ACK path, scheduler, recovery
owner, strategy decision, notification, or effect.

The canonical path is:

```text
Redis SMEMBERS/HGETALL (read-only)
    -> RedisQ2ProjectionAdapter
    -> OpeningFactV1
    -> one in-memory DeterministicEngine
    -> FACT_ONLY / OBSERVE evidence
```

## Acceptance gates

- Real evidence records trade date, observation time, source time range,
  expected/observed/missing/stale counts, coverage, and projection hash.
- `Missing != Zero`; `STALE`, `PARTIAL`, and `UNAVAILABLE` remain explicit.
- Opening Engine path processes the observed projection in one in-memory
  Engine and returns `FACT_ONLY`/`OBSERVE` only.
- No Redis/TD writes, Rabbit consume/ACK, service restart, notification, or
  effect occurs.
- A postmarket observation is never promoted to NORMAL 09:32 acceptance.
- A true controlled 09:32:10 observation or equivalent historical
  `available_at` evidence is required before claiming a NORMAL opening pass.

## Current run

The first bounded real run is recorded in:

```text
/home/exedev/validation/task008-opening-validation-20260920T183000+0800/
```

It is intentionally `PARTIAL` evidence, not a completed NORMAL acceptance:
the historical Redis cohort contained 1,000 of 5,224 symbols, all observed
symbols were stale under the 60-second policy, and only `000001` was present
among the bounded facts. The Engine shadow still produced two signals and
`FACT_ONLY`/`OBSERVE` without side effects.

## Out of scope

M3-1 NORMAL, TD health proof, Wencai recovery, Rabbit arrival/batch
equivalence, producer changes, Redis writes, and strategy migration.
