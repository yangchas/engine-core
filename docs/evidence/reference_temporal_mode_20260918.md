# Reference temporal mode verification — 2026-09-18

## Scope

Commit `9dd529d` makes the real previous-day limit-pool probe accept an explicit
`HISTORICAL`, `REPLAY`, or `LIVE` temporal mode and preserves the provider's
`fetch_completed_at_ms` in the probe artifact. It does not change the
historical availability contract:

- `HISTORICAL`/`REPLAY` still require a verified `available_at_ms`.
- `LIVE` may pass with unknown `available_at_ms` only when the read completed
  no later than the request knowledge cutoff.
- `observed_at_ms` remains audit/provenance data and is never promoted to
  historical availability.

## Verification

Local `D:\work\Go\engine_core`:

```text
502 passed
compileall PASS
git diff --check PASS
```

Cobra-ion isolated copy:

```text
/home/exedev/validation/engine-core-6511981-v1
Python 3.12.3
502 passed
compileall PASS
```

The four changed source/test file hashes were compared after granular sync.
Production `engine-next` and `t1-v2-live` were not restarted or modified.

## Real read-only probe

Command mode: `HISTORICAL`.

Inputs were obtained through the existing read-only access paths:

- Redis `TYPE/HLEN/HSCAN/GET` for `cache:yest_limit_pool:2026-09-17`;
- TD `SELECT` for bounded `auction_snapshot_v2` rows for `600519,000001,000002`.

Observed result:

```text
trade_date=2026-09-18
previous_trade_date=2026-09-17
temporal_mode=HISTORICAL
current_0925_row_count=3
previous_result_status=UNAVAILABLE
fact.status=UNAVAILABLE
fact.records=[]
available_at_ms=null
read_only=true
```

The artifact was copied from the isolated server directory without editing:

```text
tmp/real-reference-20260918/limit-feedback-20260918-historical.json
SHA256=420827aa25a802ae175172074c2bf146dfdc42217097666618d0d79451fbe258
```

This is a real source read and a fail-closed temporal result. It is not proof
that the historical pool was available at the 2026-09-18 evaluation cutoff.
The corresponding `LIVE` mode is reserved for an actual pre-node prefetch; it
was not used after the market session to manufacture readiness.

## Boundary conclusion

```text
REAL_REDIS_READ_PATH       PASS
REAL_TD_READ_PATH          PASS
HISTORICAL_TIME_SAFETY     PASS
LIVE_PREFETCH_EVIDENCE     NOT YET OBSERVED FOR THIS RUN
PRODUCTION_REPLACEMENT    NOT READY
```
