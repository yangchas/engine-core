# Calendar probe loader compatibility evidence — 2026-09-14

## Finding

The scheduled read-only morning shadow used
`/home/exedev/validation/cc-m0-calendar-20260911-v3.json`. That artifact is
`RealCalendarProbeV1` evidence: it contains `query_start/query_end` and
canonical `trading_dates`, but does not contain the directly consumable
`calendar_id/timezone/source_guard_*` fields. The previous loader therefore
failed closed with:

```text
calendar evidence missing fields: calendar_id,timezone,source_guard_valid_from,source_guard_valid_to
```

The file is not corrupt; it is a valid probe artifact with a different
contract shape.

## Fix

Code commit `6baba2d4c713943428a9dd435d284552923a0884` updates the read-only
morning loader to accept both `RealCalendarProbeV1` and the existing
`TradingCalendarSnapshotV1` fixture shape. For probe evidence it derives only
the explicit compatibility defaults (`CN_A_SHARE`, `Asia/Shanghai`) and the
guard interval from `query_start/query_end`, rebuilds an immutable calendar
snapshot, and verifies a supplied `calendar_semantic_hash` before accepting
the trade date. No producer, Redis, TD, RabbitMQ, or calendar source data was
changed.

The new tests cover probe-shape loading and semantic-hash mismatch rejection.

## Cobra-ion verification

The exact fix archive was uploaded as:

```text
/home/exedev/validation/engine-core-6baba2d.tar
```

Archive SHA-256:

```text
1a1b507a2d99903681d9f23fde33af66b20f894d4dd99185a850daf26f85b9c4
```

Using Cobra-ion Python `3.12.3`:

```text
calendar_load=PASS
calendar_id=CN_A_SHARE
timezone=Asia/Shanghai
source_guard=2023-12-01..2026-12-31
trade_date=2026-09-15: present
calendar_semantic_hash=a64a5dfa7a9e256c500799c2fa646f9e92b0a3d21d9b45c9f68d01ee1655610e
pytest: 395 passed in 2.05s
compileall: PASS
```

The input probe artifact SHA-256 remains:

```text
b62dbb28a0a945297e4035627d137e51229e7528d8ac2e9b8ca37d41f28debe5
```

## Scheduled validation process

The previous self-owned waiting process was removed after the failed loader
check. A later docs-only evidence commit produced the current exact-HEAD
archive `engine-core-8036d9c`; its executable content includes the same code
fix as `6baba2d`. The current self-owned process uses that archive and writes
only to the validation area:

```text
PID=46465
archive=engine-core-8036d9c
trade_date=20260915
start=09:15:00
stop=09:33:00
symbols=000001,000002,600519
output=/home/exedev/validation/live-morning-shadow-20260915-0915
log=/home/exedev/validation/live-morning-shadow-20260915-0915-8036d9c.log
```

At reschedule time `engine-next` and `t1-v2-live` were both `active`. The
process has no Rabbit consumer, no ACK/publish, no Redis/TD write, no
notification/effect, and does not restart production services.

This closes the calendar-loader execution blocker only. It does not prove
Core replacement, in-session data freshness, or historical 09:20/09:24/09:25
source freeze semantics.
