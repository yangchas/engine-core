# Real Redis theme-weight probe (2026-09-18)

This is a bounded, read-only probe on cobra-ion using the exact Core commit
`16187c37029df0ea52798a64cfb59ad25dbcefa7`. It reads the existing Redis
hashes only:

```text
market:stock_plate
market:stock_reason
config:plate_mapping:s2p
```

No Redis/TD write, repair, Rabbit action, notification, or effect was used.

## Observed samples

| symbol | legacy plate | s2p / reason input | Core pure weights |
|---|---|---|---|
| 000001 | 银行 | `["银行"]` | 银行: 1.0 |
| 000002 | 地产链(房地产) | `["地产链(房地产)"]` | 地产链: 0.18 |
| 000338 | 柴油发电机 | `["柴油发电机", "氢燃料电池", "一季报增长"]` | 柴油发电机: 1.0; 氢燃料电池: 0.6 |
| 600519 | 国有企业 | no s2p value | 国有企业: 1.0 |

The output matches the extracted legacy `_resolve_theme_weights` rules. The
`600519` result is intentionally preserved as an observation: the legacy
generic-keyword set contains `国企` but not `国有企业`. This probe does not
declare that Redis mapping is a formal authority, does not prove historical
availability, and does not migrate theme labels or strategy signals.

## Status

```text
CONNECTIVITY             PASS
READ_ONLY_SAFETY         PASS
LEGACY_WEIGHT_PARITY     PASS (bounded samples)
MAPPING_AUTHORITY        UNKNOWN
HISTORICAL_AVAILABILITY  UNKNOWN
THEME_STRATEGY_MIGRATION NOT_MIGRATED
```
