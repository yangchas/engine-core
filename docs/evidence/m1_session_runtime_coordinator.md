# M1 minimal session runtime coordinator

## Scope

`src/engine_core/session_runtime.py` is the first Core-owned startup/node
coordination boundary. It intentionally composes existing pure wheels instead
of introducing a scheduler or workflow framework.

```text
TradingCalendarSnapshot
        +
SessionPlan / TimerSpec
        +
Q2ProjectionSnapshot / guarded DataResult
        ↓
SessionRuntimeCoordinator.poll()
        ↓
RuntimePoll
        ↓
caller submits TimerFiring to Engine
        ↓
SessionRuntimeCoordinator.acknowledge()
```

## Contract

- One coordinator is bound to one explicit trading date and matching calendar
  and session-plan identity.
- `poll()` performs no I/O and never advances a wall clock.
- `BLOCKED` readiness returns due timer ids as deferred and no dispatchable
  firing.
- `PARTIAL` readiness remains visible to the caller; it is not promoted to
  `READY`, and each node decides whether partial input is acceptable.
- A firing becomes completed only after the caller acknowledges that Engine
  accepted it. Duplicate acknowledgement with the same identity is harmless;
  a conflicting firing is rejected.
- The state is deliberately in-memory and diagnostic. It is not a checkpoint,
  durable exactly-once record, Rabbit offset, or effect ledger.

## Tests

`tests/test_session_runtime.py` covers:

```text
blocked Q2 → deferred timer → ready Q2 dispatch
normal once-only acknowledgement
RECOVERY_CATCHUP origin
PARTIAL/stale Q2 propagation
backward observation rejection
conflicting firing rejection
calendar/session trade-date identity
```

This is a code-level M1 gate only. It does not claim that the current live Q2
source is fresh or that Core has replaced `engine-next`.

