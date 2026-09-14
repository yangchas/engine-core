# t1-v2 runtime lag observation — 2026-09-14

This is a read-only extraction from the existing `t1_v2.log`; it does not
change the running service or its ACK/write path. The selected lines are
preserved in `tmp/runtime-lag-slice-20260914.txt` with SHA-256
`db2ea9fae5435f9a363ccd83c8db2efbfdb2bd10d6eb644be1471b58a2171eb`.

```text
09:25:02  ack=110953 ack_fail=0 ticks=81233126
          last_ts_ms=1789349100000 wall_lag_ms=2418

09:30:22  ack=111019 ack_fail=0 ticks=81282643
          last_ts_ms=1789349410000 wall_lag_ms=12222

15:23:51  ack=163679 ack_fail=0 ticks=120361558
          last_ts_ms=1789369203000 wall_lag_ms=1428792
```

The service remained active with `NRestarts=0`, and `ack_fail=0` in these
observations. `wall_lag_ms` nevertheless grew from about 2.4 seconds at 09:25
to about 23.8 minutes by 15:23, while the collector continued processing
batches. This is an operational backlog/performance risk for any Core
replacement and must be measured during the multi-day shadow gate; it is not
evidence that the producer should be changed during this trading day.

```text
runtime service state       OBSERVED/ACTIVE
ACK failures                0 in sampled lines
processing backlog risk     WARN
root cause                  UNKNOWN
producer change authorized  NO
```
