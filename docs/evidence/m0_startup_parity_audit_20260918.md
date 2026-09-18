# M0 startup parity audit — 2026-09-18

## Evidence scope

This is a read-only source audit of the deployed
`engine-next@20260903_e272842` startup path and the current Core startup
modules. The legacy source was inspected on cobra-ion; no service, Redis,
TDengine, Rabbit, or effect path was changed.

## Capability comparison

| Capability | Legacy `engine-next` evidence | Current Core | Status |
|---|---|---|---|
| Trade-date and calendar identity | `app_main`/startup coordinator derive `trade_date` and `previous_trade_date`; legacy path still has its own date helper | `TradingCalendarSnapshot` and `SessionPlan` are explicit authorities | PARTIAL — Core authority exists, integration owner is not migrated |
| Phase classification | `startup_self_check.infer_run_phase` distinguishes PREMARKET/AUCTION/INTRADAY/POSTMARKET/NIGHT | `SessionPlan` maps the explicit day to PREMARKET/AUCTION/INTRADAY/LUNCH_BREAK/POSTMARKET | OBSERVED — boundary semantics need an intentional parity decision |
| 08:30 / 09:00 checkpoints | `app_main` declares startup audit checkpoints and the legacy coordinator can produce time-window actions | Generic `TimerSpec` can represent these times, but no replacement startup action owner exists | NOT_MIGRATED |
| Daily kline | Legacy startup self-check classifies missing/dead rows against formal offline date and emits repair actions | Core only has `PreviousDayStatsFunction`/provider slices; no full startup kline readiness owner | NOT_MIGRATED |
| Daily factors/chips/DDE | Legacy classifies watermark, structural, cache and current-trade gaps | No equivalent Core startup dependency contract or repair coordinator | NOT_MIGRATED |
| Yesterday limit pool / hot plates / stock-plate mapping | Legacy startup checks dated caches and may recommend bounded repair through existing runtime owners | Core has read-only functions and readiness results, but no replacement action owner; unknown historical `available_at` remains fail-closed | PARTIAL / BLOCKED |
| Auction 0920/0924/0925 | Legacy `t1-v2`/runtime owns source snapshots and writer/freeze behavior; `engine-next` consumes them | Core consumes captured/TD evidence and can dispatch facts; it does not own source freeze or writer | NOT_MIGRATED |
| Auction 0926 / opening 0932 | Legacy controllers run follow-up/opening assembly and report paths | Core `SessionRuntimeCoordinator`/Morning Shadow can produce bounded read-only node evidence | SHADOW_ONLY |
| Startup repair and persistence | Legacy coordinator can fetch/repair/cache according to phase and source rules | Core readiness is side-effect-free; no persistence or production repair | INTENTIONAL_DEFER |

## Important boundary

The presence of `StartupReadiness` and `SessionRuntimeCoordinator` proves only
that Core can evaluate already-observed inputs and calculate due timers. It does
not prove that Core can acquire, repair, persist, or own every dataset and
checkpoint required by `engine-next` startup.

The following legacy actions must not be imported into Core as-is because their
side effects and source ownership are not yet isolated:

```text
recover_auction_anchor
hot/yesterday-limit fetch-and-cache
mapping reload/writeback
market summary rebuild
large offline repair
```

## Current conclusion

```text
M0 source/action audit             PASS (evidence recorded)
Core startup self-check            PASS as pure wheel
Core startup replacement           NOT_READY
Reference-data action ownership    NOT_MIGRATED
08:30/09:00 startup parity         NOT_MIGRATED
0920/0924/0925 source ownership    EXTERNAL (t1-v2)
0926/0932 read-only shadow         OBSERVED/PASS where inputs are available
```

## Next bounded implementation

Do not add a generic workflow or repair engine. The next implementation slice
should be a thin M1 startup adapter that only:

1. obtains the already-verified calendar and real read-only provider results;
2. evaluates the existing Core readiness contract;
3. records explicit `WAIT/PREFETCH/REFRESH/DEFER` actions without executing
   side effects;
4. exposes the legacy 08:30/09:00/09:26/09:32 action differences in a
   capability-local trace.

Only after this trace matches a real `engine-next` startup observation should
any one reference-data action be considered for ownership migration.
