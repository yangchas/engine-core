# Real LIVE reference-readiness probe — 2026-09-18 14:28 CST

## Scope

This was a bounded, read-only observation on `cobra-ion` during the live
session. It did not restart `engine-next` or `t1-v2-live`, add a RabbitMQ
consumer, acknowledge messages, write Redis/TD, repair caches, call network
fallbacks, or dispatch effects.

Remote validation copy:

```text
/home/exedev/validation/engine-core-6511981-v1
```

Artifacts:

```text
reference-1429-prefetch-cutoff.json
SHA-256 4298053F0AFB1FDA5703A84925111C9241A8EAC2C3530F2E9BF40831D798C6F6

q2-1427.json
SHA-256 6DEBB8474664FDC5A731D61088AF95B1AAA8B1A2285FEDC98CA6A1D28A29D917
```

The reference run used an explicit one-minute future node cutoff to model a
prefetch completed before the next evaluation. This is a LIVE readiness
observation, not historical availability proof.

## Observed results

| Area | Result |
|---|---|
| Trade date | `2026-09-18` |
| Previous trade date | `2026-09-17`, derived by the calendar authority |
| Q2 rows / expected | `5224 / 5224` |
| Q2 coverage | `1.0` |
| Q2 status | `STALE` / `BEST_EFFORT_STALE` |
| Q2 newest source time | `1789711401000` ms |
| Q2 stale symbols | `5224` |
| Previous-day stats | `READY`, TD `daily_kline`, bounded symbols |
| Previous-day limit pool | `READY`, Redis, 47 rows |
| Hot plates | `UNAVAILABLE`; `hot`, `strength`, `net_inflow_yi` units remain unverified |
| LIVE temporal readiness | `PASS` for the explicit prefetch cutoff |
| Historical temporal proof | `UNAVAILABLE` |
| Overall readiness | `PARTIAL` |

`coverage=1.0` is retained separately from freshness/completeness. The stale
Q2 projection is not promoted to READY. The reference results are usable for
the LIVE prefetch contract only because their fetch completion occurred before
the explicit node cutoff; `observed_at` is not promoted to historical
`available_at`.

## Determinism and safety

The Q2 probe executed the same real Redis observation twice through the Core
engine path and produced equal snapshot/probe hashes. The read boundary was
limited to Redis reads and bounded TD SELECTs. Production service state at the
time of the observation remained active with zero restarts.

## Decision

Core can continue as a read-only Shadow and migration evidence producer. It is
not replacement-ready: Q2 freshness is stale, hot-plate units are unresolved,
and the production owner remains `engine-next`.

The next safe work is to keep the current live path fail-closed and complete
the hot-plate raw-shape/consumer parity audit in its permitted postmarket
window. No intraday network hot-plate discovery or producer change is justified
by this observation.
