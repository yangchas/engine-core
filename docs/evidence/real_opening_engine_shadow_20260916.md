# Real Redis Q2 through Opening Engine path — 2026-09-16

## Scope

This evidence covers the narrow Opening fact composition path. It proves that
one Redis Q2 observation can pass through the public Core Engine queue and
produce the existing `OpeningFactV1` calculation. It does not claim opening
strategy, state lifecycle, report parity, or production ownership.

## Path and safety

```text
Redis SMEMBERS/HGETALL
  -> RedisQ2ProjectionAdapter
  -> MARKET_UPDATE
  -> TIMER(OPENING_0932)
  -> DeterministicEngine
  -> OpeningShadowStrategy
  -> OpeningFactV1
```

The runner is bounded to one symbol and read-only. It does not consume Rabbit,
change ACKs, write Redis/TD, call recovery/fallback, send notifications, or
trigger effects.

## Contract boundary

Q2 `speed_1m_bp` is not mapped into the opening wheel's `speed_1m` field because
their unit equivalence is not proven. Missing and stale values remain visible
through the Q2 projection status; no value is filled.

The observed source timestamp is retained as the Q2 source-time range. It is
not interpreted as Rabbit arrival time or exchange tick ordering.

The same Engine path is covered by `tests/test_real_opening_engine_shadow.py`
with an injected client.

## Cobra-ion real run

```text
core commit: 803f7bf7d74113b0fac141b122c0bbaa3c79c12d
Python: 3.12.3
trade_date: 2026-09-16
symbol: 600519
observed_at: 2026-09-16T10:35:00+08:00
freshness budget: 60000 ms
Q2 symbols: 5221/5221
coverage: 1.0
projection status: PARTIAL
stale symbols: 5221
processed signals: 2
strategy: OBSERVE / FACT_ONLY / PARTIAL
```

The result is intentionally `PARTIAL`: full symbol coverage did not promote
stale source observations to fresh data. The run used the production Redis
connection convention (`REDIS_HOST/PORT/DB/PASSWORD`), exposed only
`SMEMBERS/HGETALL`, and produced no Redis/TD writes, Rabbit operations,
notifications or effects. The production `engine-next` and `t1-v2-live`
services remained active with `NRestarts=0`.
