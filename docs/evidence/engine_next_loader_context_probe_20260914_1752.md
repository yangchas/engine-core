# engine-next loader/context read-only probes — 2026-09-14

## Scope

Two bounded probes were run on `cobra-ion` against the deployed release
`/home/exedev/services/engine-next/releases/20260903_e272842` using the
production shared Python 3.12.3 environment. Both used a Redis write guard and
were limited to three symbols (`000001`, `000002`, `600519`). No Rabbit
consumer, ACK, TD/Redis writer, recovery, network fallback, notification or
effect path was enabled.

## Loader probe

`IntradayDataHub.load_auction_snapshots()` returned the current Redis
projection at observation time `2026-09-14 17:51 CST`:

```text
trade_date: 20260914
tags: 0920,0924,0925
row_count: 600 (200 per tag; bounded Top-200 projections)
selected requested rows: 3 (only 600519 was in each Top-200)
guard_writes: []
read_only: true
artifact SHA-256: 033536e5efbfe810575dc350e291c84e3016d39c8d19b6f833aee8b6a2e28ebd
```

This is a current mutable Redis observation. It proves that the loader can
read a populated 0924 key at this observation time; it does not invalidate an
earlier empty 0924 capture, prove historical retention, or establish an
in-session batch/freeze boundary. The loader's `redis_keys_written` field is a
legacy name for keys read by this method; the guard observed no writes.

## Context/fact probe

The exact old context builder and its pure auction fact helper were then run at
a simulated `09:26:00` for the same trade date and previous trade date
`20260911`:

```text
snapshot_count: 3
phase: auction (legacy phase authority for the requested simulated time)
guard_writes: []
read_only: true
latest_quote_timestamp_ms: 0
future_source_timestamp: false
artifact SHA-256: 36e44f3ada5c74d755c87613ab6922406cf0bb84b941481b68135f25d835c3cc
```

The context rows and loader rows are not asserted equal: they are different
legacy projections with different coverage and source semantics. The context
probe returned plate strings and three requested symbols, while the loader is
bounded to the Redis Top-200 snapshot projections. No first divergence is
assigned because a common immutable AuctionState/source-time snapshot was not
observed.

## Acceptance impact

```text
ENGINE_NEXT_LOADER_READ_ONLY       PASS
ENGINE_NEXT_CONTEXT_READ_ONLY      PASS
ENGINE_NEXT_CONSUMPTION_PARITY     UNPROVEN
RABBIT_BATCH_MEMBERSHIP             UNKNOWN
AUCTIONSTATE_FREEZE_MEMBERSHIP     UNKNOWN
REDIS_TD_WRITER_PROJECTION_PARITY  UNPROVEN
```

The evidence is suitable for the M0/M1 read-path audit only. It does not
authorize producer changes, report/effect delivery, Redis/TD writes, or
replacement of `engine-next`. A normal in-session run started before 09:15 is
still required to close the 09:26/09:32 timing and lifecycle evidence.
