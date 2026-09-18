# Real engine-next loader -> Core shadow — 2026-09-18

## Scope

Read-only cross-check of the current production Redis auction projection,
the exact legacy `engine-next` loader, and the Core public Engine path. No
Redis/TD writes, no Rabbit consumer or ACK changes, no recovery, notification,
or effect dispatch.

## Legacy loader result

The exact release `/home/exedev/services/engine-next/releases/20260903_e272842`
was loaded behind the Redis write guard.

```text
trade_date: 2026-09-18
requested symbols: 000001, 000338, 600519
rows: 600
rows by tag: 0920=200, 0924=200, 0925=200
guard writes: none
read_only: true
source: redis_snapshots
```

This current observation supersedes neither earlier captures nor the source
contract: it only proves that the current Redis projection has all three
200-row marginal-delta slots. It does not prove that the projection is a
full-market authority or that all rows came from one identical upstream
AuctionState.

For `000338` the loader returned:

| tag | price | auction amount (yuan) | source timestamp (ms) |
|---|---:|---:|---:|
| 0920 | 27.02 | 3,396,414 | 1789694403287 |
| 0924 | 27.18 | 13,614,462 | 1789694650292 |
| 0925 | 27.20 | 37,846,100 | 1789694706197 |

## Core shadow result

The same production-derived projection was submitted through the Core public
Engine queue. The run was repeated twice.

```text
symbol: 000338
processed signals: 6
strategy results: 3
fact status: PARTIAL / FACT_ONLY
direct fact hash: e1904a88aa98f5503865054b7388fcc56d14d1427e94c583308c1cee85496c7e
engine fact hash:  e1904a88aa98f5503865054b7388fcc56d14d1427e94c583308c1cee85496c7e
semantic hash equal: true
side effects: none
```

Observed fact values:

```text
amount_delta_yuan: 24231638
pressure_delta_yuan: 4930338
price_delta_milli: 20
rest_ask_delta_yuan: -4840578
rest_bid_delta_yuan: 89760
```

The fact remained `PARTIAL`: breadth and theme were unavailable, so no
strategy conclusion was inferred.

## engine-next context probe note

A separate guarded context probe using `--now 2026-09-18T09:25:10+08:00`
read the current Redis Q2 after the fact. It correctly exposed that the
current source timestamps were in the future relative to that historical
cutoff (`future_source_timestamp=true`). That result is audit evidence only;
the current Q2 must not be used as a 09:25 replay input.

## Decision

```text
LEGACY_REDIS_LOADER_READ_PATH      PASS
CURRENT_0920_0924_0925_PROJECTION  OBSERVED
CORE_ENGINE_PARITY                 PASS (000338, PARTIAL fact)
FULL_MARKET_AUTHORITY              NOT PROVEN
HISTORICAL_CUTOFF_REPLAY            NOT PROVEN
CORE_PRODUCTION_REPLACEMENT        NOT READY
```

The current evidence supports the next migration step: a bounded read-only
M1/M2 comparison for the three existing auction slots. It does not authorize
changing the production owner or treating the loader projection as raw Tick
or AuctionState evidence.
