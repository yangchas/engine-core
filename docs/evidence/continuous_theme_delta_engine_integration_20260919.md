# Continuous theme-delta wiring evidence — 2026-09-19

## Scope

This change makes the already extracted `theme_auction_delta_compat` rule
reachable from the existing one-Engine continuous shadow path. It is wiring
only; the compatibility fact builder and the legacy threshold rule were not
changed.

The optional input is an existing frozen `DataResult`:

```text
theme_delta_result(function_id=theme_auction_delta_compat)
```

At the formal `AUCTION_0925` evaluation target (`09:25:06`), the runner adds
that result to the same pending evaluation as the existing three auction
reference functions. One `DATA_READY` completes the evaluation. The strategy
recomputes the signal from the numeric compatibility facts; it does not trust
an already supplied label and it does not read Redis/TD/network data.

The business anchor remains `09:25:00`; source record timestamps are not
rewritten. No producer, coordinator default, Rabbit consumer/ACK path, Redis
write, TD write, report, notification, or effect was changed.

## Code and verification

| item | result |
| --- | --- |
| implementation commit | `42553a60d7a05bdf07ef9a18c67295761c42a206` |
| local targeted tests | `27 passed` |
| local full suite | `590 passed in 1.91s` |
| local compileall | PASS |
| remote archive SHA-256 | `921429050902c68f99ea01b6a7ddac2bb1903c290e2daa38e32e100ff89485d1` |
| cobra-ion full suite | `590 passed in 3.13s` |
| cobra-ion compileall | PASS |
| production services during verification | `engine-next=active`, `t1-v2-live=active` |
| production writes / new Rabbit consumer | `0 / 0` |

## Contract tests

The continuous-path tests cover:

```text
direct compatibility trace == Engine child trace for the same frozen facts
theme rule appears once, at AUCTION_0925
theme-only and reference+theme bundle combinations
UNAVAILABLE is not promoted to a shadow fact
wrong function identity is rejected
future available_at is rejected by the existing Engine bundle gate
reference-only behavior remains unchanged
```

The direct-vs-Engine assertion compares the canonical trace, not a
machine-specific evidence path. Existing auction/opening fact subtrees and
signal order remain covered by the continuous-session suite.

## Real-data boundary

The existing real Redis/TD continuous composition evidence remains valid and
unchanged:

```text
/home/exedev/validation/engine_core-ecc-e5326cd/real_redis_continuous_shadow_20260918.json
sha256=2a0b6243637f38f5fc82cc19062e6e2371e64abe87539acac0c90f0d15bd8076
```

The real theme mapping/aggregation evidence still has no historical
`available_at` proof. Therefore this commit does **not** promote a postmarket
Redis observation into a historical `09:25:06` runtime input. Real theme
execution remains `NOT_VERIFIED` until a same-session frozen compatibility
`DataResult` with cutoff-safe availability evidence exists. A postmarket read
that is observed after the business date is correctly rejected/deferred by the
existing temporal/session gates; timestamps are not rewritten to make it pass.

## Decision

```text
continuous theme rule reachability        PASS
direct-vs-Engine semantic parity          PASS (frozen contract input)
real historical theme authority           UNKNOWN / NOT_VERIFIED
production strategy migration             NOT AUTHORIZED
```

Next work may use this building block for the first Auction Shadow
differential, but must keep the compatibility input and its availability
evidence explicit. No new provider, workflow, replay, checkpoint, or effect
layer is justified by this change.
