# TASK-008 — Real opening validation (read-only)

## Scope

Validate the existing opening fact and public Engine shadow path with real
Redis Q2 observations. This is an evidence task, not a production migration:
it does not add a Redis writer, Rabbit consumer/ACK path, scheduler, recovery
owner, strategy decision, notification, or effect.

## Relationship to the 09:15–09:40 replay

The 09:15–09:40, 500-frame replay belongs to TASK-001/TASK-007 and consumes
TD `stock_tick_v2` (plus auction rows) through the cross-sectional replay path.
It proves the stock-tick replay timeline and deterministic Engine processing;
it does not prove that a Redis Q2 cohort was available at the 09:32:10 opening
cutoff.

TASK-008 is a separate Q2 opening validation. A stock-tick replay must not be
silently converted into Q2 values. The Q2 input must come from a real Redis
capture or an equivalent historical Q2 artifact with explicit source time and
`available_at` evidence.

The producer boundary is `t1-v2`, not Core. Its stateful quote calculator owns
Q2 derivation and its writer owns Redis serialization. Core must consume the
producer's Q2Frame output rather than recompute Q2 from TD rows. The replay
seam is deliberately time-agnostic: any positive monotonic producer source
timestamp is accepted, including premarket and postmarket times. Market-hours
classification, freshness, and historical `available_at` remain evidence or
consumer policies, not a Q2 computation gate.

The same-input validation path is:

```text
TD stock_tick_v2 (read-only)
    -> t1-v2 replay --dry-run --q2frame <validation file>
    -> Core Q2FrameReplaySource
```

A Core TD tick replay without the corresponding t1-v2 Q2Frame is tick-layer
evidence only and cannot close Q2-dependent opening acceptance.

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
