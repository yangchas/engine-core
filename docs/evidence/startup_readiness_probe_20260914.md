# Startup readiness probe — 2026-09-14

## Scope

This evidence covers the new `StartupReadinessV1` pure assessment and its
read-only Redis Q2 wrapper at commit `4e7b2f1`. It is not a production
coordinator and does not replace `engine-next`.

## Local and Cobra verification

| item | value |
|---|---|
| code commit | `4e7b2f1` |
| archive SHA-256 | `ab6ab6ee2a5cc0775d1d1f042474a840bbf0ef08d79b85144b5561d7397951e3` |
| Python | local project interpreter; Cobra `/home/exedev/services/engine-next/shared/venv/bin/python` 3.12.3 |
| tests | local `386 passed`; Cobra `386 passed` |
| compileall | PASS on both sides |
| side effects | no Redis/TD writes, no Rabbit consumer/ACK, no effect |

## Real Cobra read-only probe

The wrapper read the existing Redis Q2 path on `cobra-ion` for
`2026-09-14`. The output artifact was downloaded unchanged:

```text
remote: /home/exedev/validation/engine-core-4e7b2f1/startup-readiness-20260914.json
local:  tmp/startup-readiness-20260914.json
SHA-256: 575d9e5c152a39c023521210872f2824c335634915d71045df4888db20b711f5
```

Observed result:

```text
Q2 coverage             = 1.0
Q2 status               = STALE
Q2 consistency          = BEST_EFFORT_STALE
source time range       = 1789315200000 .. 1789369205000
readiness status        = PARTIAL
phase                   = POSTMARKET
due timers              = AUCTION_0926, OPENING_0932
actions                 = REFRESH_Q2, DISPATCH_TIMER:AUCTION_0926,
                           DISPATCH_TIMER:OPENING_0932
```

The probe deliberately does not turn `coverage=1.0` into `READY`; the
staleness policy remains visible. `engine-next` and `t1-v2-live` remained
`active`, with `NRestarts=0`, before and after the probe.

## Contract boundary

`StartupReadinessV1` only:

- validates Calendar/SessionPlan identity and trading-date authority;
- re-applies `TemporalDataGuard` to already-observed reference results;
- preserves Q2 status, coverage, source-time range and content identity;
- delegates due timer calculation to `SessionTimerV1`;
- returns `WAIT`, `PREFETCH`, `REFRESH` or timer dispatch/defer hints.

It never invokes a provider, prefetches data, starts a worker, mutates
`CurrentMarketState`, or writes an external store. A blocked Q2 produces
`DEFER_TIMER:*`, not a dispatch action. Q2 observed/source time after the
logical cutoff is `BLOCKED`/`UNAVAILABLE`.

## Remaining replacement gaps

This closes only the side-effect-free startup assessment contract. Core still
does not own the legacy startup lifecycle, 09:20/09:24/09:25 source freeze,
report/effect delivery, durable cross-restart identity, or multi-day
engine-next differential acceptance.
