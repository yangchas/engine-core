# Reference-data readiness read-only evidence — 2026-09-14

## Scope and safety

This is a read-only run of the exact `engine_core` commit
`541383d` on `cobra-ion` using the production shared Python 3.12.3
environment. It did not restart `engine-next` or `t1-v2-live`, add a Rabbit
consumer, acknowledge messages, write Redis/TD, repair caches, send mail, or
invoke an effect.

Initial observation window: `2026-09-14 10:56–10:58 Asia/Shanghai`.
The same probes were rerun from commit `541383d` at approximately
`2026-09-14 11:03 Asia/Shanghai`; the values below use that exact-commit run.

## Service and storage observation

```text
engine-next = active, MainPID=4022407, NRestarts=0
t1-v2-live  = active, MainPID=2878024, NRestarts=0
/dev/root   = 19G total, 15G used, 2.7G available, 85%
memory      = 7.2Gi total, 4.3Gi available, swap=0
```

The t1-v2 progress log continued to advance and reported ACK failures of
zero, but its source timestamp was approximately 27 minutes behind wall
time at the observation. This is a production-chain lag observation, not a
claim that either service is down.

## Real Redis reference-data probes

### Hot plates

Command: `examples/run_real_hot_plates.py --trade-date 2026-09-14`.

```text
redis key                 = cache:hot_plates:2026-09-14
redis type                = hash
rows / HLEN               = 50 / 50
scan_consistent            = true
payload_value_sha256       = d366f7b44d0dd8933d98da124be888f5d20cc6f5a982e74aa86993769fa28720
meta updated_at            = 2026-09-14 09:27:04
meta source                = kaipan
meta schema_version        = absent
meta available_at_ms       = absent
meta field_units           = absent
core result                = UNAVAILABLE
missing_fields             = [available_at_unknown]
core content_hash          = 6fc40f13489a26a81f52cb598660c02ac68f9f9be025d447efaf56c78772f45d
artifact sha256             = 52b965e541f97d437b69d6862ea81a998337ed7c7783f5cf845c21a86c7d4f82
```

The 50 rows are real and date-matching, but the legacy metadata only carries
update/attempt fields. `updated_at` is not promoted to historical
`available_at_ms`.

### Previous-day limit pool

Command: `examples/run_real_previous_day_limit_pool.py --trade-date 2026-09-14 --previous-trade-date 2026-09-11`.

```text
redis key                 = cache:yest_limit_pool:2026-09-11
redis type                = hash
rows / HLEN               = 40 / 40
scan_consistent            = true
payload_value_sha256       = 45c00adf268e89b711b96404c969850c6ef0d824ed4fa3e6df234942f98fa3ab
meta updated_at            = 2026-09-14 09:26:31
meta source                = kaipan
meta schema_version        = absent
meta available_at_ms       = absent
meta field_units           = absent
turnover unit              = UNKNOWN
core result                = UNAVAILABLE
missing_fields             = [available_at_unknown]
core content_hash          = e5bcaae8f91e0fb86095acc3ec3ba7ee25b954556129875712dd7058677b3f92
artifact sha256             = e0c05ec1986527982c07cf0fe0c775f327864cc192a7174d80bb63619d95b8ce
```

The function derived `2026-09-11` from the supplied trading-day calendar and
passed exactly that date to the provider. The physical read succeeded; the
readiness result is unavailable because historical availability and the
turnover unit are not proven.

## Real Redis Q2 probe

Command: `examples/run_live_q2_probe.py --trade-date 2026-09-14 --stale-after-ms 60000`.

```text
expected symbols            = 5220
quotes                      = 5220
missing symbols             = 0
coverage                    = 1.0
status                      = STALE
consistency                 = BEST_EFFORT_STALE
stale symbols               = 5220
oldest source_record_time   = 1789315200000
newest source_record_time   = 1789353028000
projection_hash             = 2240f7dcee2c64081b7b02ff7c36989601ba41629e50010a1071d976d26cfbfb
same observation deterministic= true
read operations             = smembers=1, hgetall=5220
input canonical sha256       = 873d6e60a92607245811612f06e3ff6d68747ba15b0fd5d4fe30dd4f3d6c3d72
artifact sha256              = d0430c6fb3af66e945f862710dc800adb118467ceca9d6b3429cc135d3e755f5
```

`coverage=1.0` means every member in the observed active cohort was read. It
does not imply freshness or completeness. `source_record_time_ms` is retained
as the source record timestamp only; it is not Rabbit arrival time or an
exchange tick-order guarantee.

The bounded volume diagnostics for `000001`, `300750`, and `600519` are
consistent with a lots-scale `vol` interpretation, but they remain diagnostic
and do not freeze a producer contract for the separate `iv` field.

## Readiness decision

```text
hot_plates_prefetch_read                 OBSERVED physical read
hot_plates_runtime_ready                 BLOCKED (available_at_unknown)
previous_day_limit_prefetch_read         OBSERVED physical read
previous_day_limit_runtime_ready         BLOCKED (available_at_unknown, turnover_unit_unknown)
q2_read                                  OBSERVED physical read
q2_fresh_runtime_input                    BLOCKED (all observed rows stale under 60s policy)
```

This is a successful fail-closed readiness audit, not a provider-connectivity
failure. No fallback source was substituted and no metadata was invented.

## Next bounded action

Do not add a provider hierarchy or relax `TemporalDataGuard`. The next
production-side prerequisite is a controlled writer contract that emits
verified `schema_version`, `available_at_ms`, and field units for the datasets
that a node actually needs. Until a later read observes those fields, the
reference data must remain `UNAVAILABLE` for historical cutoff evaluation.

The Q2 lag should be investigated through the existing t1-v2 backlog/lag
observability, but it does not authorize changing the core freshness policy or
restarting the production chain during this audit.
