# Engine integration audit

Audit date: 2026-09-04 (Asia/Shanghai)
Branch: `codex/feature-engine-integration`

## Scope

This stage only composes the already-tested Q2, state, window, bundle and Probe
contracts. It does not add a scheduler, workflow engine, replay adapter,
checkpoint store, RabbitMQ consumer or external effect.

## Implemented boundaries

- Signal payloads are recursively frozen before queueing; later caller mutation
  cannot change queued business input.
- `signal_id` is an idempotency key. Repeated identical submissions are ignored;
  a conflicting logical time, sequence, kind or payload is rejected.
- MARKET_UPDATE, PULSE, TIMER and RECOVERY_CATCHUP cannot move the market
  frontier backwards. DATA_READY is deliberately exempt because it completes
  its original frozen evaluation and does not mutate CurrentMarketState.
- RECOVERY_CATCHUP carries its origin into closed WindowView objects.
- DATA_READY validates the required `snapshot` and `bundle` envelope before
  invoking Strategy.
- Observable snapshots/results and the in-memory signal-id dedupe horizon are
  bounded by explicit Engine constructor limits; durable replay dedupe remains
  out of scope.

## Verification

```text
local Python: 66 passed
cobra-ion Python 3.12.3: 66 passed
local compileall: passed
remote compileall: passed
remote read-only live-Q2 smoke: 64 symbols -> 1 snapshot -> 1 Probe result
```

Remote validation used the non-production temporary copy:
`/home/exedev/tmp/engine_core_validation_20260904_1244`.
No production process, Redis/TD row, RabbitMQ delivery or external effect was
changed.

The live-Q2 smoke used an explicit temporary freshness policy and observed one
stale quote (`projection_status=PARTIAL`, `coverage=1.0`). Details are in
`docs/evidence/engine_integration_live_q2_smoke_20260904.md`.

The Foundation 600519 wheel-parity hashes and the full live Q2 + TD shadow
path are recorded in `docs/evidence/engine_integration_correctness_20260904.md`
and `docs/evidence/engine_integration_shadow_20260904.md`.

## Residual boundaries

- Signal ordering is deterministic within this in-memory queue; it is not a
  durable journal or Rabbit arrival-order replay.
- Signal-id idempotency is guaranteed only while the submitted signature stays
  in the bounded in-memory cache. Durable idempotency is deferred with the
  journal/checkpoint layer.
- DataFunction execution, EvaluationPlan, asynchronous collection and
  checkpoint/restart remain deferred.
- The engine does not claim production batch equivalence or recovery equivalence
  across an unobserved outage.
