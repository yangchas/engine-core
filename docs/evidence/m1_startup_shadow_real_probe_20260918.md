# M1 Real Redis Startup Probe

## Observation

At `2026-09-18 17:29:22.851 CST` (the probe reports UTC
`2026-09-18T09:29:22.851241+00:00`), the isolated Core startup probe used the
existing read-only Redis Q2 path on `cobra-ion`:

```text
trade_date                 2026-09-18
q2 symbols / coverage      5224 / 1.0
q2 status                  STALE
q2 consistency             BEST_EFFORT_STALE
readiness                  PARTIAL
phase                      POSTMARKET
actions                    REFRESH_Q2, DISPATCH_TIMER:AUCTION_0926,
                           DISPATCH_TIMER:OPENING_0932
```

The source-time range was preserved as returned by Redis. The result was not
used as a normal opening-time acceptance: it is a post-market observation and
does not prove 09:20/09:24/09:25 cutoff readiness.

## Safety and evidence

The probe boundary was `Redis SMEMBERS/HGETALL only; no TD/Rabbit/write/effect`.
`engine-next` and `t1-v2-live` were not restarted or modified.

Remote artifact:

```text
/home/exedev/validation/m1-startup-shadow-20260918/startup-readiness.json
sha256=a60b45b06488db67f5938285907f8b71eb9d172408a417583ab6b5b13e21dded
```

This closes only the real Redis connectivity/readiness observation for the
new startup trace path. It does not close live freshness, reference-data
prefetch, source freeze, or Core replacement acceptance.
