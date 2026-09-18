# Real Redis theme auction-delta shadow (2026-09-18)

## Runtime

The exact Core archive `48fcfc645357c35fd56cd4fa00b4cbca62dc17ca` ran on
cobra-ion with the shared Python 3.12.3 environment. It read only:

```text
market:auction:20260918:0924
market:auction:20260918:0925
market:stock_plate
config:plate_mapping:s2p
```

No TD query, network fallback, Redis/TD write, repair, Rabbit action,
notification, or effect was used.

## Result

```text
projection 0924             READY
projection 0925             READY
scope                       REDIS_TOP_AMOUNT_INTERSECTION
normalized delta rows       1
rows with theme mapping     1
rows without mapping        0
fact status                 OBSERVED
artifact SHA-256            1634acd2f178e934b41a9888e8ce3a0a148deda340e0a422f444e9f2cf84335c
```

The single observed symbol was `000338`. Core produced:

| theme | weight path | amount delta (yuan) | bid delta (yuan) | change delta (pp) | amount ratio |
|---|---:|---:|---:|---:|---:|
| 柴油发电机 | 1.0 | 24,231,638.0 | 89,760.0 | 0.08 | 2.7798454320 |
| 氢燃料电池 | 0.6 | 14,538,982.8 | 53,856.0 | 0.08 | 2.7798454320 |

Values are facts from the two Redis projections, not a strategy signal and not
actual net capital inflow. The runner preserves missing values and does not
pretend that the TopN intersection is the full market universe.

## Acceptance status

```text
REAL_REDIS_THEME_PATH        PASS
THEME_WEIGHT_TRANSFORM       PASS (bounded, legacy rule parity)
THEME_FACT_CONSTRUCTION       PASS (bounded)
FULL_UNIVERSE_COVERAGE        NOT_PROVEN
HISTORICAL_AVAILABILITY       NOT_PROVEN
LEGACY_THEME_NUMERIC_PARITY   NOT_PROVEN
THEME_STRATEGY_PARITY         NOT_MIGRATED
PRODUCTION_OWNER_TRANSFER     NO
```
