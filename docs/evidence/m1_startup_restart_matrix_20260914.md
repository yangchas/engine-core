# M1 Core-owned startup/restart matrix (2026-09-14)

## Scope

This evidence covers only the existing pure `SessionPlanV1` and
`SessionTimerV1` wheels.  It does not add a scheduler, startup-readiness
service, persistence layer, or production side effect.

The source freeze nodes at 09:20/09:24/09:25 remain owned by `t1-v2` and are
intentionally absent from the Core node list.  The matrix covers the Core
consumption checkpoints:

```text
AUCTION_0926  -> 09:26:00
OPENING_0932  -> 09:32:00
```

## Proven behavior

The tests in `tests/test_startup_restart_matrix.py` prove:

- before 09:26 no Core-owned node is due;
- a normal 09:26 run emits only `AUCTION_0926`;
- a cold start at 09:28 emits `AUCTION_0926` exactly once as
  `RECOVERY_CATCHUP` and does not repeat an already completed node;
- a cold start at 09:33 emits 09:26 and 09:32 in schedule order, with
  deterministic late-by durations;
- completed node identities are scoped to the explicit trade date and
  `SessionTimerV1` contract, while unknown or duplicate identities fail
  closed before they reach the timer wheel;
- non-trading dates and cross-date instants fail closed through the existing
  calendar/session authority;
- same-time timers are ordered deterministically by timer identity.

The caller-side identity notation used by this matrix is:

```text
(trade_date, timer_id, SessionTimerV1)
```

It is a test contract only; no second node object or runtime registry was
introduced.

## Not proven by this matrix

This is not a claim of cross-process exactly-once execution.  The current
timer wheel accepts an explicit completed-id projection but does not persist
it.  It also does not prove t1-v2 source batch membership, 09:25 finalization
ordering, Redis/TD writer equality, or engine-next report behavior.

The next implementation boundary may consume these firings, but must keep
source freeze ownership outside Core and preserve `NORMAL` versus
`RECOVERY_CATCHUP` origin.

## Verification

```text
focused Session/Timer/startup matrix: 22 passed
engine_core default suite: 329 passed
compileall: PASS
git diff --check: PASS
```

