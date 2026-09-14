# Runtime log slice — 2026-09-14 09:24:50–09:26:30 CST

This is a read-only extraction from the existing production log files on
`cobra-ion`; no process was restarted and no producer/consumer was changed.
The extracted local evidence file contains the selected lines and has SHA-256
`f38a5594c3e65f7203d2aacabf82da2262dd3639d586a52f3da7ac027499365c`.

## t1-v2 progress evidence

```text
09:24:51 batches=110927 source_in=84889562 source_reject=3674983
         ack=110927 ack_fail=0 ticks=81214579 redis_cmds=218419348
         td_sql=110952 redis_committed=109064293 last_ts_ms=1789349090000
         wall_lag_ms=1540

09:25:02 batches=110953 source_in=84908111 source_reject=3674985
         ack=110953 ack_fail=0 ticks=81233126 redis_cmds=218455432
         td_sql=110978 redis_committed=109082286 last_ts_ms=1789349100000
         wall_lag_ms=2418
```

These lines prove the running collector was processing and acknowledging
batches around the boundary, and preserve source-time progress and lag. They
do not identify which exact batch contained a final auction tick.

## engine-next runtime evidence

```text
09:25:10.444 scheduled event execute | name=auction_finalize_0925
09:25:48.785 runtime prime | phase=auction | symbols=5219 | quotes=5219 | native=5219
09:25:49.982 runtime context build done | snapshots=5219
09:26:27.686 runtime notification delivered | channel=email
09:26:27.690 scheduled event execute | name=auction_followup_0926
```

The scheduled-event and context-build lines establish runtime timing and
population size. The notification line belongs to the existing production
owner; this Core audit did not send it. No matching `AuctionState` payload,
writer commit marker, or Rabbit batch identifier is present in this slice.

## Acceptance impact

```text
source/runtime progress observed       OBSERVED
scheduled 0925/0926 actions observed   OBSERVED
batch membership                       UNKNOWN
AuctionState final-tick inclusion      UNKNOWN
Redis/TD writer same-state proof       UNPROVEN
engine-next report/loader parity       UNPROVEN
```

The evidence narrows the remaining gap to batch/freeze and cross-layer
identity. It is not a justification to change the producer barrier or to
replay a notification.
