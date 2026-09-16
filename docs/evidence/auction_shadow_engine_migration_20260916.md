# Auction Shadow Core migration — 2026-09-16

## Scope

This change is the first production-code migration into `engine_core`. It adds
`AuctionShadowStrategy`, a fact-only Engine strategy that composes the existing
`AuctionFactShadow` and adjacent segment wheels. It does not migrate a trading
rule and does not replace `engine-next`.

The strategy accepts three explicit business anchors:

```text
PRE_AUCTION_0915 → AUCTION_0920 → AUCTION_0924
```

It emits `OBSERVE` / `FACT_ONLY` only. It does not emit BUY, PASS, EV, risk,
candidate, notification, order, or any other effect. Source observation times
are copied from snapshot metadata; they are never rewritten to the business
anchor time. A caller may explicitly mark a segment `PARTIAL` when its source
coverage is incomplete.

## Evidence boundary

The class is tested with the frozen 600519 fixture and the Core
`DeterministicEngine`. The Engine result is compared with a direct invocation
of the same foundation wheels; the resulting fact `content_hash` and
`evidence_hash` must match. Duplicate/conflicting anchor snapshots and
cross-session use fail closed.

This is not a live production cutover. The 2026-09-16 Cobra-ion run remains a
read-only normal-origin shadow: real Redis Q2, real TD auction rows, and the
existing guarded `engine-next` loader were observed without Rabbit consumption,
ACK changes, Redis/TD writes, notifications, or effects. The morning shadow
produced `AUCTION_0926` and `OPENING_0932`; Q2 coverage was 1.0 but freshness was
`PARTIAL`/stale at the later observation, and TD previous-day data was
`MISSING` with unknown historical `available_at`. These statuses remain
fail-closed and are not upgraded by this migration.

## Exit condition for this slice

The slice is complete only when local and cobra-ion run the same final commit,
fixture manifest, Python/dependency environment contract, and test collection.
The next migration is a verified shadow rule chosen from Gate B; no formal
strategy threshold is inferred from the fact labels in this change.

## Verification on cobra-ion

Final validation archive: `core-migration-c428748`, commit
`c428748ee620925058305ec22e999e016f0edf53`.

```text
Python 3.12.3 (/home/exedev/services/engine-next/shared/venv/bin/python)
pytest: 410 passed
compileall: PASS
real command: run_real_auction_strategy_shadow.py
trade_date: 2026-09-16
symbols: 600519
rows: 3 (0920/0924/0925)
anchor completeness: PARTIAL / READY / READY
strategy: OBSERVE / FACT_ONLY / PARTIAL
real artifact SHA-256: 4789B76F4645B2FEF8E9A72F20A4187F3741412B4CA904EA42BBE6509BACFF11
```

The real strategy run used TD `SELECT` only and preserved the observed
09:20/09:24/09:25 source timestamps. The 09:20 price was missing, therefore
the composed 09:20→09:24→09:25 fact remained `PARTIAL`; no value was filled and
no strategy conclusion was emitted. The validation directory was separate
from production and did not alter `engine-next`, `t1-v2-live`, RabbitMQ,
Redis, TD writers, notifications, or effects.
