# Real auction reference readiness — 2026-09-16 / 8cff623

## Scope

The exact `8cff623` archive was verified on cobra-ion and then used for one
read-only reference preparation against the production Redis and TD paths.
No production service was restarted or changed.

```text
archive sha256  8e65c17f548248ac09096da87e0d47c3a82d3a71125ddb8111cf51982b0b91eb
python          3.12.3
tests           430 passed
artifact        /home/exedev/validation/real-auction-reference-readiness-20260916-8cff623.json
artifact sha256 dc7baf7e23061b909384f43f261942df1874735d902e1038004c3d30e500c6d5
```

## Real results

The fixed preparation order was:

```text
previous_day_stats
previous_day_limit_pool
hot_plates
```

The calendar uniquely derived `2026-09-15` as the previous trade date for
the `2026-09-16` auction session.

| Input | Real observation | Core result |
|---|---:|---|
| Redis Q2 | 5221 expected / 5221 read; 18 stale | `PARTIAL`, coverage `1.0` |
| TD previous-day stats | 0 rows for 000001/000002/600519 | `MISSING` |
| Redis previous-day limit pool | 32 rows, scan consistent, 0 decode errors | `UNAVAILABLE` |
| Redis current hot plates | 50 rows, scan consistent, 0 decode errors | `UNAVAILABLE` |

The Redis datasets are real and non-empty.  They remain `UNAVAILABLE`
because the production metadata is still the old shape: it contains
`updated_at*`, source, phase, and row counts, but not the verified
`schema_version`, `available_at_ms`, and `field_units` contract.  Core does
not reinterpret `updated_at` as historical availability.

The resulting startup readiness was `PARTIAL`.  This is a truthful migration
blocker, not a connectivity failure and not permission to invent readiness.

## Boundary proved

`prepare_auction_references()` is a bounded composition, not a registry or
workflow.  It calls the three existing DataFunctions in fixed business order,
keeps the Calendar as the only previous-trade-date authority, and can feed
the existing `StartupReadinessV1` assessment.  Provider I/O remains outside
the Engine reducer.

The production side-effect boundary remained:

```text
Redis SMEMBERS/HGETALL/TYPE/HLEN/HSCAN/GET
bounded TD SELECT
no Redis/TD write
no Rabbit consumer/ACK change
no repair/network fallback
no notification/order/effect
```

At evidence time both `engine-next` and `t1-v2-live` were `active`; root disk
usage was 88% with about 2.2 GiB available.

## Replacement conclusion

This closes the missing Core composition boundary, but it does not make Core
production-ready.  The next blocking work is source-side readiness evidence:

1. publish the already-audited metadata contract for current hot plates and
   previous-day limit pool, without changing their business payload;
2. explain/fill the real TD `daily_kline` absence for the exact previous trade
   date through the existing offline sync owner, not through intraday fallback;
3. repeat this same read-only probe before the auction node and require the
   resulting DataResults to be cutoff-safe.

