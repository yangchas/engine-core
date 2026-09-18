# Real reference-data readiness — 2026-09-18

The read-only readiness probe was run on `cobra-ion` using commit
`1ebf866c6b9f8653ec1b3b2d1b28ebeb6c28a1c5` and the production shared Python
3.12 environment. It used the existing TD and Redis access paths and did not
write either store, consume RabbitMQ, or invoke effects.

## Important cutoff result

When the cutoff was omitted, the command used the observation instant before
provider reads. The providers completed after that instant, so LIVE readiness
was correctly marked unavailable rather than silently accepting late data.

With an explicit prefetch cutoff 60 seconds after the observation start:

```text
temporal_live_readiness = PASS
readiness.status         = PARTIAL
previous_day_stats       = READY
previous_day_limit_pool  = READY
hot_plates               = UNAVAILABLE
q2                       = STALE
```

The previous-day function derived `2026-09-17` from the calendar for the
requested trade date `2026-09-18`. TD returned the real `daily_kline` rows and
the Redis limit-pool cache passed its payload/metadata checks.

## Remaining real-data gap

The real `cache:hot_plates:2026-09-18` payload exists and is readable, but its
metadata does not prove units for `strength`, `hot`, and `net_inflow_yi`. Core
therefore keeps the result `UNAVAILABLE` with explicit `unit_unknown:*` reasons.
It must not guess or promote the values to READY. The next data-contract task
is to obtain the existing Kaipan writer/consumer unit evidence, not to change
the reader or add a fallback.

## Artifacts

```text
default-cutoff:
/home/exedev/validation/engine-core-1ebf866c6b9f8653ec1b3b2d1b28ebeb6c28a1c5/live-20260918/reference-readiness.json
sha256=20633115d4d10fa303d3de17a3ab35c01ed1535b27e3fa8ea4959313033fdb04

explicit-prefetch-cutoff:
/home/exedev/validation/engine-core-1ebf866c6b9f8653ec1b3b2d1b28ebeb6c28a1c5/live-20260918/reference-readiness-explicit-cutoff.json
sha256=d23aed95036e04ef48104c16c7ea5fa4b9a84508ee95d60ba90ca15608ac9e32
```
