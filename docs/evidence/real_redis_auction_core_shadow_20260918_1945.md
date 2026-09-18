# Real Redis auction projection → Core shadow — 2026-09-18 19:45 CST

## Scope

The Core Redis auction adapter read the existing production projection keys
for `0920`, `0924`, and `0925`, then submitted the three projections through
one in-memory Core Engine. The run was repeated. No Redis/TD writes, Rabbit
consumer/ACK changes, recovery, notification, or effect dispatch occurred.

## Result

```text
trade_date: 2026-09-18
symbol: 000338
projection statuses: 0920=PARTIAL, 0924=PARTIAL, 0925=PARTIAL
processed signals: 6
strategy results: 3
decision status: FACT_ONLY
fact status: PARTIAL
```

The business hashes were identical across the two runs:

```text
fact_content_hash: bd7660727cbe9967248b99c2ff1ad4bc502d0c5c9a044e55d1efd14379d1582a
comparison_hash:  b6b622a1081dfda5b04807f25b9a41108f5ac5aea634506d37d1ef886085aac5
```

Observed source time range:

```text
0920: 1789694403287
0924: 1789694650292
0925: 1789694706197
```

Observed fact output:

```text
amount_delta_yuan: 24231638
rest_bid_delta_yuan: 89760
pressure_delta_yuan: UNKNOWN
price_delta_milli: UNKNOWN
rest_ask_delta_yuan: UNKNOWN
breadth: UNAVAILABLE
theme: UNAVAILABLE
state: OBSERVE
```

The differing evidence hash between runs is expected because observation
context is evidence, not business semantic identity.

## Boundary

This proves a real Redis projection can enter the Core Engine deterministically
and preserve missing semantics. It does not prove that the Top-Amount
projection is a full-market authority, that Redis/TD share one immutable
AuctionState, or that Core owns the 09:20/09:24/09:25 freeze. `engine-next`
and `t1-v2-live` remain production owners.

## Decision

```text
REAL_REDIS_AUCTION_READ_PATH     PASS
CORE_REDIS_ENGINE_SHADOW         PASS (PARTIAL / FACT_ONLY)
CORE_AUCTION_SOURCE_OWNERSHIP    NOT READY
PRODUCTION_REPLACEMENT           NOT READY
```
