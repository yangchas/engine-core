# Runtime storage pressure evidence — 2026-09-18 19:20 CST

## Scope

Read-only post-market inspection of `cobra-ion` after the trading session. No
service restart, data deletion, Redis/TD write, Rabbit consumer/ACK change, or
Core effect was performed.

## Observed runtime

```text
engine-next: active, MainPID=1041872, NRestarts=0
t1-v2-live: active, MainPID=2878024, NRestarts=0
root filesystem: 19G total, 16G used, 1.4G available, 93%
TDengine /var/lib/taos: 5.9G
TDengine /var/log/taos: 134M
Docker local volumes: 6.783GB, all active and non-reclaimable
TD market_data1: 135679 tables, keep=3650d,3650d,3650d
host /var/log/taos: approximately 760MB across taoslog files
```

The `t1-v2-live` journal contains repeated `stage=commit.tdengine` errors:

```text
error=No enough disk space
```

Observed repeatedly from `14:26:10` through `15:40:53` CST. The services being
`active` is therefore not sufficient evidence that the TD write path is
healthy or complete.

## Decision

```text
TD_WRITE_HEALTH          BLOCKED/WARN
REDIS_READ_ONLY_SHADOW   ALLOWED
TD_AS_GROUND_TRUTH       NOT_ALLOWED UNTIL CAPACITY IS RESOLVED
PRODUCTION_OWNER_CHANGE  NONE
```

The Core Redis-only shadow may continue with explicit stale/partial status.
TD-dependent replay, cross-source parity, and replacement acceptance remain
blocked until the storage owner supplies an approved capacity/retention action
and a clean post-action write-health observation.

## Safety boundary

No deletion or cleanup was attempted. Docker reports the TD volume as active;
the database retention is currently `3650d` and there is no approved change to
that retention in this evidence. The large TD log files are a separate log
rotation candidate, but they are also not deleted or rotated here. Any cleanup
requires a separate explicit change window, an exact target, a backup/restore
plan where applicable, and a post-change integrity check.
