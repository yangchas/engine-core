# Real TD auction shadow — 2026-09-19 02:48 CST

## Scope

This is a bounded, read-only post-market diagnostic executed on `cobra-ion`
from the isolated Core archive `a1259af14f95ac3103a4cda56f66a7aa89136be3`.
It queried TDengine `auction_snapshot_v2` for symbol `600519` and passed the
rows through the public in-memory Engine path. It did not read Rabbit, write
Redis/TD, send a notification, or change either production service.

## Evidence

| Item | Value |
|---|---|
| trade date | `2026-09-18` |
| symbol | `600519` |
| remote artifact | `/home/exedev/validation/real-td-auction-shadow-20260919-0248-600519.json` |
| artifact SHA-256 | `1ce43a09607613c7224e35d8b0b2fbd4d3c97f74338d05b1875c4a1f74a059a5` |
| engine fact status | `PARTIAL` |
| pure/Engine semantic hash equal | `true` |
| processed signals | `6` |
| strategy result count | `3` |
| side-effect boundary | `TD SELECT + in-memory Engine only` |

Observed source record times:

```text
AUCTION_0920  2026-09-18T09:20:03.287+08:00
AUCTION_0924  2026-09-18T09:24:10.292+08:00
AUCTION_0925  2026-09-18T09:25:06.197+08:00
```

The 0925 row is after the six-second source settling barrier. The diagnostic
keeps the business anchor at `09:25:00` and records the actual source time;
it does not claim normal finalization admission because this runner is marked
`ANCHOR_ALIGNED_DIAGNOSTIC` with `normal_finalization_evidence=false`.

## Interpretation

This closes a real-data TD projection → Engine semantic-parity observation for
one symbol. It does **not** prove Rabbit batch membership, producer freeze
ownership, Redis/TD writer equivalence, normal-origin timing, full-market
coverage, or `engine-next` replacement readiness.
