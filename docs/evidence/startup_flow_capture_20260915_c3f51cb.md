# Startup/readiness flow over real captured Q2 — 2026-09-15 / c3f51cb

## Scope

This evidence runs the existing pure `build_q2_projection`, calendar/session,
`assess_startup_readiness`, timer and `SessionRuntimeCoordinator` components
over a real production capture. It is not a live service run and does not
pretend that a later capture was available at an earlier node.

Input:

```text
/home/exedev/validation/production-ground-truth-20260915/q2_093210.jsonl
```

Input SHA-256:

```text
ba38a9d66d49c0c07c8d1934e7755f68575a9e0484b84dd65e07f97bf19bd0e0
```

The capture contains 5,220 real Q2 rows. The isolated run artifact is stored
on Cobra-ion at:

```text
/home/exedev/validation/engine_core-ecc-c3f51cb/startup_flow_capture_20260915.json
```

Artifact SHA-256:

```text
7fa8c9308cb8b87f152a7ddc6826249dee0cbb4982ee767f37f7337576ff0d32
```

## Results

| Scenario | Readiness | Evidence |
| --- | --- | --- |
| startup before 09:20 with a Q2 capture observed at 09:32:10 | `BLOCKED` | `q2_observation_after_cutoff`; `AUCTION_0920` deferred |
| 09:32 node using the captured Q2 | `PARTIAL` | coverage `1.0`, 5,220 stale under the explicit 300-second policy; timers remain observable and no readiness upgrade occurs |
| late recovery at 17:32:44 | `PARTIAL` | same source range; timers dispatch as recovery observations, not normal historical proof |

The coordinator output shows the explicit node identities and the distinction
between `dispatchable` and `deferred`. No provider call, cache repair,
Redis/TD/Rabbit operation, notification, or effect was performed by this
verification script.

## Boundaries

- `coverage=1.0` does not mean fresh or complete; this capture is explicitly
  `PARTIAL` under the selected freshness policy.
- The capture does not contain historical `available_at` evidence for the
  reference functions. Existing real reference evidence therefore remains
  `UNAVAILABLE` when used for a historical cutoff.
- This closes the pure startup/readiness/late-recovery composition over a real
  payload. It does not prove a normal 09:20 live run, Rabbit batch membership,
  source-freeze ownership, full-universe auction authority, or replacement of
  `engine-next`.
