# Scheduled normal-origin Core morning shadow — 2026-09-17

At `2026-09-16 12:20 CST`, cobra-ion scheduled one bounded validation process
for the next trading day. It is isolated from production systemd units and
uses the exact validation archive `engine-core-0421726` with an explicit build
identity.

```text
runner script:  /home/exedev/validation/schedule-live-shadow-20260917.sh
runner pid:     519924 (verified alive at scheduling time)
code archive:   /home/exedev/validation/engine-core-0421726
build id:       0421726
calendar:       /home/exedev/validation/calendar-probe-20260916/calendar.json
trade_date:     2026-09-17
symbols:        000001,000002,600519
stale_after_ms: 60000
start_at:       09:15:00
stop_at:        09:33:00
output_dir:     /home/exedev/validation/live-morning-shadow-20260917-0915
runner_log:     /home/exedev/validation/live-morning-shadow-20260917-0915.runner.log
```

The output directory was absent at scheduling time. The script waits for the
absolute Asia/Shanghai target `09:14:50` and then runs the existing
`run_live_morning_shadow.py`; it does not use a relative sleep as a business
timer. The runner will capture startup, `AUCTION_0926`, and `OPENING_0932`
using actual observation times and write each output once.

Safety boundary:

```text
new Rabbit consumer       0
Rabbit ACK/publish        0
Redis/TD write            0
production restart       0
notification/effect       0
```

`engine-next.service` and `t1-v2-live.service` were active when scheduled and
remain the production owners. This schedule is evidence collection only; it
does not imply Core replacement readiness.

