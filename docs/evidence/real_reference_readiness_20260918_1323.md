# 2026-09-18 real reference-data readiness probe (13:23 CST)

This was a bounded, read-only probe on `cobra-ion`. It reused the existing
Redis cache layouts and the verified TD daily-kline query boundary. It did not
repair caches, call network fallback, write Redis/TDengine, consume RabbitMQ,
send notifications, or run a strategy effect.

## Runtime identity

```text
trade_date             2026-09-18
calendar semantic hash 8f2a56c8dca12d7a37779fb14961ab5fb0bed21d03ef4aaf3a4c7b8d76c3b96f
previous_trade_date    2026-09-17
observed_at            2026-09-18T05:23:42.702959+00:00
Q2 records             5224 / 5224
Q2 coverage            1.0
Q2 status              STALE
Q2 stale count         5224
```

## Source observations

| Function | Source observation | Contract result |
|---|---|---|
| `previous_day_stats` | TD `daily_kline` query for `2026-09-17` | `UNAVAILABLE`, `available_at_unknown` |
| `previous_day_limit_pool` | Redis `cache:yest_limit_pool:2026-09-17`, 47 rows, HLEN/HSCAN consistent | `UNAVAILABLE`, `available_at_unknown` |
| `hot_plates` | Redis `cache:hot_plates:2026-09-18`, 50 rows, HLEN/HSCAN consistent | `UNAVAILABLE`, `available_at_unknown` |

The Redis metadata contains source/date/row-count/update information, but not a
verified historical availability timestamp or complete field-unit contract.
The probe therefore does not use `observed_at` as a substitute for
`available_at`.

## Readiness result

```text
status       PARTIAL
phase        INTRADAY
reasons      q2_status:STALE
             reference_status:previous_day_stats:UNAVAILABLE
             reference_status:previous_day_limit_pool:UNAVAILABLE
             reference_status:hot_plates:UNAVAILABLE
actions      REFRESH_Q2
             PREFETCH:previous_day_stats
             PREFETCH:previous_day_limit_pool
             PREFETCH:hot_plates
```

Preparation hash:

```text
66907d388b967b02977c05ab7b0e281256176bc3bf64b48eac0315bcc15c7b3e
```

Probe artifact SHA-256:

```text
90fad9e4dfb956b5f04299b692286c2b5bf25c7690a4092b69b3229c1b1fa962
```

## Acceptance

```text
real Redis reference reads       PASS
calendar/date derivation         PASS
metadata/date/row integrity      PASS for observed fields
historical availability contract BLOCKED/UNAVAILABLE
runtime reference readiness      NOT CLOSED
```

This is a successful fail-closed result. It does not justify relaxing the
TemporalDataGuard, inventing `available_at_ms`, or allowing these datasets into
historical replay. The next M2 action is producer metadata/availability
evidence or an explicitly approved live-only policy; no generic fallback engine
is introduced.
