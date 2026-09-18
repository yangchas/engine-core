# Real LIVE reference readiness after turnover canonicalization — 2026-09-18

## Runtime evidence

- Remote validation copy: `/home/exedev/validation/engine-core-6511981-v1`
- Artifact: `reference_readiness_live_turnover_20260918.json`
- Artifact SHA-256: `6ed6e2f5fe2b967367dd9b033d8e94f489f3e4c2ff35b29b126153355a19735b`
- Code commit under validation: `48b2d42` source changes synced to the isolated copy
- All tests: `468 passed in 1.63s`
- `compileall`: PASS
- Production services: `engine-next=active`, `t1-v2-live=active`, both `NRestarts=0`

## Read-only result

```text
trade_date: 2026-09-18
previous_trade_date: 2026-09-17
Q2: 5224/5224, coverage=1.0, status=STALE
previous_day_stats: READY (TD daily_kline)
previous_day_limit_pool: READY (Redis, 47 rows)
hot_plates: UNAVAILABLE (unit_unknown:hot/net_inflow_yi/strength)
temporal_live_readiness: PASS
temporal_historical_proof: UNAVAILABLE
overall readiness: PARTIAL
```

The live result proves that the legacy Redis raw field `turnover` is accepted
only through the Core canonical `turnover_yuan` mapping. It does not add a
historical `available_at_ms`, and it does not make the data Replay-ready.

The command used only bounded Redis reads (`TYPE`, `HLEN`, `HSCAN`, `GET`,
`HMGET`) and bounded TD SELECT. It did not add a Rabbit consumer, acknowledge
messages, write Redis/TD, repair caches, invoke network fallbacks, or trigger
effects.

## Follow-up after canonical-unit guard

- Code commit: `414dfe0`
- Linux suite: `469 passed in 1.61s`, `compileall PASS`
- Follow-up artifact: `reference_readiness_live_turnover_v2_20260918.json`
- Follow-up artifact SHA-256: `2957af09e890edda895f575e39b3f019fd6a3317edf30fef94f8a3f807a8e8de`
- Result unchanged: previous-day stats `READY`, previous-day limit pool
  `READY`, hot plates `UNAVAILABLE`, Q2 `STALE`, overall `PARTIAL`.
