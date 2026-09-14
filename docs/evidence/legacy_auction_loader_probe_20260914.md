# Legacy auction loader / Core read-only comparison — 2026-09-14

## Capture

The exact deployed `engine-next` read path
`IntradayDataHub.load_auction_snapshots()` was called through
`run_engine_next_auction_loader_probe.py` on `cobra-ion` with no recovery or
writer path enabled:

```text
release: /home/exedev/services/engine-next/releases/20260903_e272842
trade_date: 2026-09-14
tags: 0920,0924,0925
symbols requested: 600519,000001,000002
row_count: 600 (200 per tag)
selected_rows: 3 (only 600519 was present in the Top-200 projections)
guard_writes: []
artifact SHA-256: 2dbdb285aef39e689c1d78838ca5dc256c2b337065e522d7c349ae405b1f5043
```

This disproves neither the earlier empty-0924 observation nor the current
presence: the Redis keys are mutable projections whose state depends on the
observation time. It does show that `0924` can be present and that the
legacy loader returns only bounded Top-200 rows, not a full market universe.

## Selected 600519 projection

| tag | source | source timestamp | price | auction amount | bid amount | ask amount |
|---|---|---:|---:|---:|---:|---:|
| 0920 | `redis_0920` | `1789348803146` | `0.0` | `2679600` | `2807200` | `0` |
| 0924 | `redis_0924` | `1789349050162` | `1276.0` | `7783600` | `3445200` | `0` |
| 0925 | `redis_0925` | `1789349106156` | `0.0` | `16984233` | `383103` | `0` |

The source timestamps are preserved as observed projection timestamps; they
are not Rabbit arrival times or batch boundaries. `ask_amount_present=false`
is retained as a projection field and is not converted to a verified zero
semantic.

## Core fact reconstruction

The selected rows were mapped into the existing Core auction fact helper as a
bounded read-only comparison. The result was:

```text
source table: redis:auction_projection
0920→0924: READY endpoints, price semantic UNKNOWN because 0920 price=0
0924→0925: READY endpoints, price semantic UNKNOWN because 0925 price=0
shadow quality: PARTIAL
shadow decision: FACT_ONLY / OBSERVE
semantic hash: 87dea7036f0dacca7677eed54f075132e0bf19780652ea1b74733459d0b98730
evidence hash: 53205bf3528b8e97b07864bef75fc7d92a4340256a586a625de549eb7caed5af
```

The derived pressure/amount changes are observations of the Redis projection
fields only. They are not a strategy conclusion and are not actual net-flow
claims.

## Boundary and next action

- `FIRST_DIVERGENCE` remains `UNPROVEN`: no direct AuctionState evidence was
  available to prove whether Redis and another writer diverged upstream.
- Redis projection rows are not a substitute for raw TD tick or runtime batch
  membership evidence.
- The result is safe for Core shadow and field/lineage audit only. It does not
  authorize producer changes, Redis writes, report delivery, or engine-next
  replacement.
