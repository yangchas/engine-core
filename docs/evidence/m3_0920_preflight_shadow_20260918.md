# M3-1 09:20 preflight and node shadow — 2026-09-18

## Scope

This is the first minimal M3 composition slice. It reuses the existing
`TradingCalendarSnapshot`, `SessionPlan`, `SessionRuntimeCoordinator`, Redis
Q2 adapter and public `DeterministicEngine`; it does not add a scheduler,
provider framework, persistence, fallback or production ownership.

```text
one Q2 prefetch
→ calendar/session self-check
→ 09:20 timer identity
→ cutoff gate
→ one Engine instance (only if safe)
```

The entrypoint is
`examples/run_m3_0920_shadow.py`. A blocked preflight never dispatches the
Engine and never substitutes current data for the historical 09:20 cutoff.

## Verification

- Local suite: `525 passed`, compileall PASS, diff-check PASS.
- Cobra-ion Python 3.12.3 isolated suite: `525 passed`, compileall PASS.
- Unit coverage includes:
  - normal 09:20: one Q2 prefetch, one Engine instance and two signals;
  - missing Q2: blocked with no Engine dispatch;
  - recovery after 09:20: current Q2 cannot be retrofitted to the old anchor;
  - missing/invalid auction projection: blocked with no fallback.

## Real Cobra-ion run

The server clock was already after the 09:20 business anchor, so the run was
intentionally `RECOVERY_CATCHUP`, not normal-origin opening evidence:

- symbol: `000338`
- Q2: real Redis `SMEMBERS/HGETALL`, `5224`-symbol cohort,
  `STALE/BEST_EFFORT_STALE`, coverage `1.0`;
- Q2 prefetch calls: `1`;
- calendar/session identity: present and consistent;
- timer: due, scheduled 09:20, actual recovery observation 18:40;
- cutoff result: `BLOCKED` because the Q2 cohort was observed after the
  historical 09:20 anchor;
- Engine dispatch: `false`; no current state was used to invent a 09:20 node;
- `engine-next` and `t1-v2-live`: remained active;
- no Redis/TD write, Rabbit action, restart, notification or effect.

Artifact:
`/home/exedev/validation/m3-0920-shadow-20260918-recovery-000338-v4.json`

Artifact SHA-256:
`a6d0ab5d5948c664e7d0148326b848b1386780d42c097b18c4e5003fc207e570`

## Boundary

This closes the M3-1 code and fail-closed recovery contract, not normal-origin
09:20 acceptance. A normal-origin PASS still requires a future trading-day
run with Q2 and the 0920 auction projection observed at or before that node's
actual cutoff. 09:24 and 09:25 remain separate follow-up slices.
