# engine_next auction migration delta (2026-09-13)

## Evidence scope

The legacy source was read from the active Cobra release
`e272842c8f490f55a1b017badb71e71904ce008e` under
`engine_next/runtime`. This is a source/behavior audit, not a production
change and not a claim that the legacy report is a verified strategy oracle.

## Legacy pure fact behavior

`auction_shadow.build_anchor_delta_evidence(previous, current)` currently
computes the following from normalized 0924/0925 rows:

```text
amount_delta_yuan
price_delta_milli
rest_bid_delta_yuan
rest_ask_delta_yuan
pressure_delta_yuan
amount_ratio
withdrawal_yuan
auction_directional_pressure_yuan
direction
labels
amount_reference_bucket
```

The first five endpoint values and the pressure formula have a real TD
600519 source-formula parity record in core. `amount_ratio`, withdrawal,
direction labels and reference buckets do not yet have a verified active
consumer oracle in the new core and remain `UNKNOWN`, not migrated strategy
behavior.

## Legacy production assembly prerequisites

`production_fact_assembly.build_production_auction_facts` requires, before it
can produce a complete plate shadow:

```text
Redis 0925 summary
Redis 0925 anchor universe
TD auction_snapshot_v2 rows for 0920, 0924 and 0925
runtime-owned frozen stock->plate mapping
```

Missing mapping or an effective-universe mismatch makes the plate facts
unavailable/partial. The function also optionally resolves names, but names
are presentation-only and are not an auction availability prerequisite. It
does not perform network repair or fallback query in this path.

## Core comparison

| Capability | Legacy release | engine_core | Parity status |
| --- | --- | --- | --- |
| P/M/RB/RA endpoint deltas | pure helper | `SegmentFrame`/`SegmentComparison` | MATCH for verified source fields |
| Pressure formula | `(RB-RA)` delta | `compute_resting_order_pressure` | MATCH |
| Amount ratio | helper output | absent | UNKNOWN |
| Withdrawal | helper output | absent | UNKNOWN |
| Direction/labels | helper output | no strategy conclusion | UNKNOWN / intentionally deferred |
| Plate aggregation | mapping-dependent assembly | not in current minimal shadow | UNKNOWN |
| Redis summary/anchor reader | production assembly | external read-only evidence only | NOT_MIGRATED |
| TD three-anchor query | production assembly | bounded TD evidence runner | READ_ONLY_EVIDENCE |
| Email/report/effect | legacy owner | deny-all/no owner | NOT_MIGRATED |

The correct migration statement is therefore:

```text
core fact endpoint parity = proven for the sampled shared fields
full auction strategy/report parity = not proven
```

No strategy threshold, direction label or report owner may be enabled based on
this audit alone.
