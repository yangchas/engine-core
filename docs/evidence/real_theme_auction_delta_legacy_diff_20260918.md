# Real Redis theme delta legacy differential (2026-09-18)

## Same-input setup

On cobra-ion, the deployed `engine-next@20260903_e272842` pure
`build_auction_snapshot_delta_stats` helper and Core were given the same:

```text
Redis 0924/0925 TOP_AMOUNT rows
market:stock_plate
config:plate_mapping:s2p
symbol = 000338
```

The read path was Redis-only and read-only. The two implementations were not
allowed to query different providers or repair data.

## Result

The common 0924/0925 Redis projection contained 175 symbols. For `000338`,
both implementations produced these shared numeric facts:

| theme | amount delta | bid delta | change delta |
|---|---:|---:|---:|
| 柴油发电机 (weight 1.0) | 24,231,638.0 | 89,760.0 | 0.08 |
| 氢燃料电池 (weight 0.6) | 14,538,982.8 | 53,856.0 | 0.08 |

The legacy helper additionally emitted:

```text
amount_ratio_avg = 2.7798
signal            = 温和放量
```

Core emitted the same ratio before legacy rounding (`2.7798454320`) and no
signal. The signal remains intentionally outside the fact-only migration.

## Status

```text
REAL_SOURCE_SAME_INPUT       PASS
THEME_WEIGHT_PARITY          PASS
SHARED_NUMERIC_FACT_PARITY   PASS (bounded symbol)
LEGACY_ROUNDING_DIFFERENCE   OBSERVED
LEGACY_SIGNAL_MIGRATION      NOT_MIGRATED
FULL_UNIVERSE_PARITY         NOT_PROVEN
```
