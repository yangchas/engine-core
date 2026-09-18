# 2026-09-18 real LIVE reference-readiness probe

## Scope

This was an isolated, read-only validation copy on `cobra-ion`:

`/home/exedev/validation/engine-core-6511981-v1`

Production `engine-next` and `t1-v2-live` were not restarted or modified. No
Rabbit consumer/ACK, Redis write, TD write, repair, network fallback, or effect
was used.

## Contract change exercised

Historical/replay requests still require verified `available_at_ms`. LIVE
requests may use `fetch_completed_at_ms` recorded by the provider at the end of
the read, but only when that timestamp is no later than the explicit
`knowledge_as_of_ms` cutoff. Fetch completion does not populate or imply
historical `available_at_ms`.

The first run deliberately used the observation instant as the cutoff. The
provider completed a few milliseconds later and all three references correctly
failed closed with `live_fetch_completion_after_cutoff`. This was not treated as
a data-source failure.

The accepted prefetch run supplied an explicit one-minute node/prefetch cutoff:

```text
trade_date              2026-09-18
previous_trade_date     2026-09-17
reference cutoff        1789710761762
calendar semantic hash  8f2a56c8dca12d7a37779fb14961ab5fb0bed21d03ef4aaf3a4c7b8d76c3b96f
```

## Real source result

| Function | Source | Status | Fetch completed | Historical `available_at` | Remaining issue |
|---|---|---:|---:|---:|---|
| `previous_day_stats` | TD `daily_kline` | READY | before cutoff | UNKNOWN | none for live use |
| `previous_day_limit_pool` | Redis `cache:yest_limit_pool:2026-09-17` | UNAVAILABLE | before cutoff | UNKNOWN | `turnover` unit unknown |
| `hot_plates` | Redis `cache:hot_plates:2026-09-18` | UNAVAILABLE | before cutoff | UNKNOWN | `hot`, `net_inflow_yi`, `strength` units unknown |

The Redis hashes were read with the existing bounded read path. HLEN/HSCAN
remained consistent: 47 rows for yesterday's limit pool and 50 rows for hot
plates. Q2 returned 5224/5224 rows, coverage 1.0, but remained STALE under the
60-second freshness policy; coverage was not promoted to freshness.

```text
temporal_live_readiness      PASS
temporal_historical_proof    UNAVAILABLE
startup readiness             PARTIAL
```

The `PARTIAL` readiness is due to stale Q2 and unresolved source field units,
not to a fabricated historical availability claim.

## Verification identity

```text
remote artifact SHA-256:
da8c7530c5d6c195de5e5648d11c58b2363f381e0cb8c4929eb3248283b80960

remote source SHA-256:
contracts.py                         55e1c720d476e92bc556b1063fbea4396309d1d1a76178bb9df3c7512c46191e
data.py                              b916ba77824b5f92b170435564cc63039245a12af666063745a2ec0d6e5c3178
run_real_auction_reference_readiness.py
                                    988626884fded35b45853985dfd6d8bf2067c6752249d215189f66e96e7accf6

server test suite                   468 passed
server compileall                   PASS
```

## Acceptance

```text
LIVE temporal gate                    PASS
Historical/replay proof               UNAVAILABLE (correctly fail-closed)
Real TD/Redis read path               PASS for observed sources
Reference semantic completeness       PARTIAL
Production side effects               0
Core replacement readiness            NOT READY
```

This closes the minimum live-only readiness contract. It does not authorize
relaxing replay time safety, changing producer behavior, or replacing
`engine-next`.
