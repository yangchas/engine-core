# Continuous real-input composition — 2026-09-18 / e5326cd

## Scope

This is a bounded, read-only composition check of the current continuous
session entry point. It is not a production handover and it does not claim
normal intraday availability. The production owners remain `engine-next` and
`t1-v2-live`.

The isolated Cobra-ion run used the existing access paths:

```text
Redis market:auction:{date}:{0920,0924,0925}
Redis q2:active:{date} + q2:{symbol}
TD/Redis reference-readiness path
        ↓
SessionRuntimeCoordinator
        ↓
one DeterministicEngine
        ↓
ContinuousSessionShadowStrategy
```

No Rabbit consumer or ACK path was added. No Redis/TD write, repair, report
delivery, notification, or effect was performed.

## Evidence

| Item | Result |
| --- | --- |
| Core commit | `e5326cd` |
| Trade date | `2026-09-18` |
| Symbol | `000338` |
| Auction 0920/0924/0925 | `READY`, 200-row `TOP_AMOUNT` projections each |
| Q2 | real Redis read, `READY`, coverage `1.0`, stale count `0` under the diagnostic no-stale policy |
| Q2 source range | `1789660800000` → `1789714805000` |
| Reference preparation | executed through existing TD/Redis read paths |
| Reference statuses | `previous_day_stats=UNAVAILABLE`, `previous_day_limit_pool=UNAVAILABLE`, `hot_plates=UNAVAILABLE` |
| Engine | one instance |
| Coordinator | one instance |
| Completed timers | `AUCTION_0920`, `AUCTION_0924`, `AUCTION_0925`, `OPENING_0932` |
| Processed signals | `9` |
| Strategy result count | `4` |
| Read-only | `true` |
| Artifact SHA-256 | `2a0b6243637f38f5fc82cc19062e6e2371e64abe87539acac0c90f0d15bd8076` |

Raw JSON is retained on Cobra-ion at:

```text
/home/exedev/validation/engine_core-ecc-e5326cd/real_redis_continuous_shadow_20260918.json
```

## Time and availability boundary

The run uses `POSTMARKET_DIAGNOSTIC` with a same-day catch-up evaluation
sequence after the real observation clock. It deliberately does not claim
that the current postmarket read was known at 09:20, 09:24, or 09:25:06.
The reference functions remain `UNAVAILABLE` because historical
`available_at` evidence is absent. `observed_at` was not promoted to
`available_at`.

The production 09:25 contract remains unchanged:

```text
business anchor = 09:25:00
formal finalization/evaluation target = 09:25:06
an actually delayed dispatch is recorded as observation evidence; it does not change the formal target or prove cohort completeness
```

## Conclusion

The composition path is executable with real saved Redis/TD inputs and keeps
the one-engine/one-coordinator boundary deterministic. This closes only the
real-input composition seam. It does not close normal live freshness,
reference prefetch readiness, Rabbit batch membership, source-freeze ownership,
full-universe authority, or `engine-next` replacement.
