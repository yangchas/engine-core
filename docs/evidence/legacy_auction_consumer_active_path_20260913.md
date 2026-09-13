# Legacy auction consumer active-path audit — 2026-09-13

## Authority

The inspected source is the deployed Cobra release
`e272842c8f490f55a1b017badb71e71904ce008e` at
`/home/exedev/services/engine-next/releases/20260903_e272842`.
This is source evidence only; no legacy code was imported into
`engine_core`, and no production process or data was changed.

## Observed active call sites

The release's `auction_runtime_controller.py` calls the following helpers while
assembling the auction/opening reporting context:

```text
build_auction_plate_bucket_stats(...)
build_auction_snapshot_delta_stats(...)
build_opening_validation_bundle(...)
```

The local strategy path `local_strategy_framework.py` calls
`build_auction_plate_bucket_stats` from `build_auction_bucket_local_node` and
then applies the following observed branch thresholds:

```text
auction_amount > 0
and leader_count >= 2
and yest_limit_count >= 1
    → auction_bucket_concentrated / probe / strong

red_count >= green_count * 2
and leader_count >= 1
    → auction_bucket_breadth / probe / breadth

drift <= -0.015
and avg_open_pct > 0.01
    → auction_bucket_fade / avoid / risk
```

These branches are source-observed.  The audit did not prove that every branch
is reached by the currently deployed runtime for a real date, nor that the
threshold units, phase transitions, or cross-day state are stable contracts.

## Input capability boundary

The bucket helper consumes more than the currently verified core fact slice:

```text
plate mapping / theme weights
auction_amount
leader_rank_in_theme
yesterday-limit membership
open_pct / current_pct
lb_days
hot-plate fields
```

The delta helper additionally consumes 0924→0925 rows with amount, amount
delta, bid delta, change delta and amount ratio fields.  Current core only has
verified symbol-level P/M/RB/RA endpoint facts and pressure; it does not yet
have a verified mapping authority, historical availability contract for these
reference datasets, or a same-input legacy consumer oracle.

## Migration classification

| Capability | Source evidence | Core status |
| --- | --- | --- |
| endpoint price/amount/resting deltas | helper source + real TD 600519 | `MATCH` for shared fields |
| pressure proxy | verified producer formula + core wheel | `MATCH`, not net-flow |
| plate bucket aggregation | active source call observed | `UNKNOWN` / not migrated |
| concentrated/breadth/fade branches | thresholds observed in source | `UNKNOWN` / no real oracle |
| amount ratio/withdrawal/direction labels | helper source | `UNKNOWN` |
| formal strategy/report/effect owner | controller graph observed | `NOT_MIGRATED` |

No `TURN_STRONG`, `BUY`, `EV`, or `AVOID` conclusion is emitted by this audit.
The legacy branches cannot be promoted to an `AuctionShadowStrategy` until a
same-date/same-input real consumer output, unit contract, lifecycle matrix,
positive/negative fixtures, and missing/partial behavior are captured.

## Next bounded action

Use the next available real trading-day capture to compare one small plate
bucket through:

```text
legacy active consumer input
→ legacy output/oracle
→ core fact inputs
```

Until that evidence exists, retain `AuctionFactShadow` as
`FACT_ONLY/OBSERVE` and do not alter the production `engine-next` path.
