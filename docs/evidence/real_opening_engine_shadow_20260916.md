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
with an injected client. A real Cobra-ion Redis run must be recorded separately
with the explicit trade-date, observation time and freshness budget.
