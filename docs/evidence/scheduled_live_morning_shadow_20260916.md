# Scheduled live morning shadow — 2026-09-16

At `2026-09-15 11:08 CST`, Cobra-ion was scheduled to start the existing
bounded `run_live_morning_shadow.py` at `2026-09-16 09:14:50 CST` and capture
the normal-origin `09:26` and `09:32` Core-owned nodes through `09:33`.

```text
remote scheduler pid       233578
code archive                /home/exedev/validation/engine-core-81e0dd3
calendar                    /home/exedev/validation/cc-m0-calendar-20260911-v3.json
trade_date                  2026-09-16
symbols                    000001,000002,600519
stale_after_ms              60000
output_dir                  /home/exedev/validation/live-morning-shadow-20260916-0915
runner_log                 /home/exedev/validation/live-morning-shadow-20260916-0915.runner.log
```

The target output directory was absent at scheduling time, and the detached
process was confirmed alive. This is an independent validation process only:
`engine-next` and `t1-v2-live` remain production owners; no Rabbit consumer or
ACK changes, Redis/TD writes, recovery/fallback calls, notifications, or
effects are permitted.

The scheduled archive is an existing functional validation copy. The local
repository HEAD is tracked separately; this run is intended to produce real
in-session evidence and is not a current-HEAD exact verification.
