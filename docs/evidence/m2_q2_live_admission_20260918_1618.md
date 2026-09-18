# M2-1 Q2 live admission audit — 2026-09-18 16:18 CST

## Scope

This is a read-only M2-1 admission check against the existing Redis Q2 path.
It does not add a consumer, change ACK behavior, write Redis/TD, restart a
service, or trigger an effect.

The probe ran from the isolated Core copy with the production Python 3.12.3
environment:

```text
/home/exedev/validation/engine-core-6511981-v1
```

## Real observation

```text
trade_date                    2026-09-18
requested symbols             5224
returned symbols              5224
coverage                      1.0
missing symbols               0
stale symbols                 5224
status                        STALE
consistency                   BEST_EFFORT_STALE
newest source lag             4826 seconds
same observation engine hash  PASS
read operations               1 SMEMBERS + 5224 HGETALL
```

The source time range and field-presence/volume diagnostics were preserved in
the raw artifact. The result is not a fresh runtime admission: coverage does
not promote stale data to READY.

Artifact copied without editing:

```text
tmp/real-reference-20260918/q2-m2-20260918-1618.json
SHA256=39631e949cb6b28b4906589a55e83a3dc2aeb55e7841cd7c4187d38977e5ee9e
projection_hash=8103f3c9801eef42402e43478b0cde634d983ced240f4d0f0e53a657cbcc96e9
engine_probe_hash=1374994dabe1b86f15c0c88b3c2e818c2d565ad7eeaf954ecc2b5e275b069d6b
```

## Gate conclusion

```text
REAL_REDIS_Q2_READ_PATH             PASS
Q2_TRADE_DATE                       PASS
Q2_COVERAGE                         PASS
Q2_FRESHNESS                        BLOCKED (STALE)
M2_LIVE_TEMPORAL_ADMISSION          BLOCKED
CORE_REPLACEMENT                    NOT READY
```

The next valid M2-1 attempt must happen while the source is fresh (or after
the upstream TD/storage lag is fixed). No freshness threshold was relaxed to
obtain a pass.
