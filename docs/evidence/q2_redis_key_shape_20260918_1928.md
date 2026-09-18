# Redis Q2 key-shape evidence — 2026-09-18 19:28 CST

## Read-only probe

The production Q2 contract is a two-level Redis shape:

```text
q2:active:{trade_date}  SET of six-digit symbols
q2:{symbol}             HASH of quote fields
```

For `2026-09-18`, `q2:active:20260918` was a `SET` with `5224` members. The
members were symbol codes; they were not Redis keys themselves. `q2:000338` and
`q2:600519` were `HASH` values containing the observed fields (`px`, `pc`,
`amt`, `vol`, `ts`, `ph`, `am`, `br`, `ar`, and related fields).

## Contract consequence

Calling `HGET` on `q2:active:{date}` is a wrong-type access and must not be used
as a Q2 reader. The existing Core `RedisQ2ProjectionAdapter` correctly uses
`SMEMBERS` for membership and `HGETALL q2:{symbol}` for the per-symbol quote.

This probe confirms the Redis key dialect; it does not prove freshness, Rabbit
batch membership, or historical availability. No Redis/TD write, repair, or
Rabbit action was performed.
