# Legacy auction fact parity audit (2026-09-13)

## Authority and scope

The authority for this audit is the deployed `engine_next` release
`e272842c8f490f55a1b017badb71e71904ce008e` on `cobra-ion`, not the local
parent repository. The inspected module is:

```text
engine_next/runtime/auction_shadow.py
```

This is a wheel-local audit of the pure auction fact helpers. It does not
claim that every helper is on the active production report path, and it does
not treat old threshold labels as a verified strategy oracle.

## Capability parity table

| Legacy capability | Legacy function/behavior | Core counterpart | Status | Evidence/constraint |
| --- | --- | --- | --- | --- |
| Pair two anchor rows by symbol and tag | `build_anchor_shadow_evidence` groups rows and builds a pair | `build_segment_frame` + `compare_adjacent_segments` | `INTENTIONAL_CHANGE` | Core requires explicit adjacent snapshots and does not silently choose a duplicate row. |
| Compute amount/price/resting bid/resting ask deltas | `build_anchor_delta_evidence` | `SegmentFrame`/`SegmentComparison` and `AuctionFactShadow` | `MATCH` for required endpoint facts | Verified on real TD 0920/0924/0925; missing fields remain PARTIAL/UNAVAILABLE. |
| Pressure formula | `(bid - ask)` and endpoint delta | `compute_resting_order_pressure` and order-book facts | `MATCH` | Pressure is a directional book proxy, not net capital flow. |
| Amount ratio and withdrawal amount | `amount_ratio`, `withdrawal_yuan` | none | `UNKNOWN` | Requires an active consumer oracle and lifecycle audit before migration. |
| Direction labels | `positive`, `negative`, `unresolved` | none | `UNKNOWN` | Old labels mix amount change, price change and pressure; not promoted to a core fact contract yet. |
| Reference amount buckets | `amount_reference_bucket` | none | `NOT_APPLICABLE` for first symbol-only slice | Display/threshold context is not required by the current 09:20→09:24 wheel case. |
| Redis/row field aliases | `price/px`, `amount/am`, `bid_amount/br`, `ask_amount/ar` | source-specific adapter boundary | `INTENTIONAL_CHANGE` | Core canonical fields are explicit; no alias fallback or zero-fill is allowed inside facts. |
| Price conversion | `price` is multiplied by `1000` when `price_milli` is absent | source adapter only | `UNKNOWN` | Must be proven per source; never apply generically in the core fact wheel. |
| Missing ask handling | `ask_amount_present=False` makes pair unavailable | `OrderBookFacts` status and lineage | `MATCH` | Captured Redis rows with absent ask remain not comparable/partial. |
| Duplicate `(symbol, tag)` handling | dictionary overwrite in grouping | strict duplicate rejection in probes/TD helper | `INTENTIONAL_CHANGE` | Silent last-write-wins would hide source corruption. |
| Plate aggregation | `build_plate_shadow_from_snapshot_rows` aggregates by `stock_plate` | none | `UNKNOWN` | Needs verified mapping authority, full consumer path and state lifecycle. |
| Multi-theme conflict accounting | `multi_theme_conflict_count` | none | `UNKNOWN` | Audit-only legacy behavior; no current core theme source. |
| Limit-up/down seal aggregation | `limit_state` plus `br/ar` aliases | none | `UNKNOWN` | Requires source field authority and active report consumer parity. |

## Core boundary confirmed

The current core intentionally supports only the following verified slice:

```text
source-aligned price_milli / auction_amount_yuan /
auction_bid_amount_yuan / auction_ask_amount_yuan
→ adjacent SegmentFrame
→ endpoint pressure and deltas
→ fact-only AuctionFactShadow
```

It does not carry forward the legacy helper's implicit alias fallback,
zero-fill behavior, threshold labels, plate aggregation, or strategy wording.
Those are differences recorded as `INTENTIONAL_CHANGE` or `UNKNOWN`, not
silently treated as parity failures or copied as contracts.

## Status

```text
CORE_ENDPOINT_FACT_PARITY = MATCH
CORE_PRESSURE_FORMULA = MATCH
LEGACY_DIRECTION_LABEL_PARITY = UNKNOWN
LEGACY_PLATE_AGGREGATION_PARITY = UNKNOWN
FORMAL_AUCTION_STRATEGY_MIGRATION = NOT_STARTED
```
