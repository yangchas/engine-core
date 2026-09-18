# Theme auction-delta aggregation audit (2026-09-18)

## Finding

The extracted `resolve_legacy_theme_weights` transform matches the active
legacy `_resolve_theme_weights` rules, but the legacy **aggregation** is not
the same as the current Core `ThemeAuctionDeltaFact` aggregation.

This is an audit finding, not a production change.

## Difference matrix

| field | legacy `build_auction_snapshot_delta_stats` | Core `ThemeAuctionDeltaFact` | status |
|---|---|---|---|
| `amount_0925` / `amount_yuan` | weighted sum | weighted sum | MATCH in formula |
| `amount_delta_24_25` | weighted sum | weighted sum | MATCH in formula |
| `bid_amount_delta_24_25` | weighted sum | weighted sum | MATCH in formula |
| `change_pct_delta_avg` | weighted contributions divided by symbol count | weighted average divided by total weight | DIFFERENT / no parity claim |
| `amount_ratio_avg` | positive values averaged without theme weight | positive weighted average | DIFFERENT / no parity claim |
| missing numeric values | `_safe_float(..., 0.0)` | missing remains `None`, theme becomes `PARTIAL` | INTENTIONAL semantic correction |
| strategy label | threshold-based signal string | no signal | NOT_MIGRATED |

## Consequence

`ThemeAuctionDeltaFactV1` is currently a conservative, explicit fact wheel. It
must not be wired into the legacy strategy/report path as a drop-in replacement
until one of these is explicitly chosen and tested:

1. an exact legacy-compatibility aggregation path, or
2. an intentional new aggregation contract with a strategy-level differential
   record explaining the changed behavior.

The current migration claim is therefore limited to:

```text
theme-name normalization/ordering/weight transform = MATCH (bounded)
theme numeric aggregation parity                 = NOT_PROVEN
theme strategy/report parity                     = NOT_MIGRATED
```
