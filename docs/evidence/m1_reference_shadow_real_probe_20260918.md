# M1 Real Reference Shadow Probe

## Observation

At `2026-09-18 17:32:44 CST` on `cobra-ion`, the isolated read-only
`run_real_auction_reference_readiness.py` probe used the existing source
paths for three bounded symbols (`600519`, `000001`, `000002`):

```text
previous_trade_date       2026-09-17
previous_day_stats source  td_daily_kline
Q2                         5224/5224, coverage=1.0, STALE
Redis yest-limit pool      47 rows, HLEN/HSCAN consistent
Redis hot plates            50 rows, HLEN/HSCAN consistent
readiness                  PARTIAL
```

The result deliberately kept all three reference `DataResult`s as
`UNAVAILABLE` because `available_at_ms` is unknown and the live fetch finished
after the probe's fixed `knowledge_as_of_ms`. This is the expected fail-closed
behavior for a same-instant probe; it does not mean the TD/Redis connection
failed.

The correct next test is a real pre-node prefetch: fetch before 09:20, retain
the actual fetch completion/observation evidence, and then validate reuse at
the 09:20 cutoff. No historical `available_at_ms` may be invented.

## Safety and evidence

The boundary used only bounded TD SELECT and Redis reads. No Redis/TD write,
Rabbit consumer/ACK, repair, network fallback, notification, or effect ran.

Remote artifact:

```text
/home/exedev/validation/m1-reference-shadow-20260918/reference-readiness.json
sha256=7852f547d22c054d525fa56f10a5df7f32e1b1e04c277dc15857be3088bcd5d0
```
