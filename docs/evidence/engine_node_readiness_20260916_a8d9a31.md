# Engine node-boundary Q2 readiness — 2026-09-16

## Change

Commit `a8d9a31` adds one bounded Q2 readiness observation at each Core-owned
morning node in `examples/run_live_morning_shadow.py`:

```text
startup self-check
    + AUCTION_0926 node-boundary Q2 recheck
    + OPENING_0932 node-boundary Q2 recheck
```

The recheck is read-only evidence. It is not a second scheduler, a hot-path
poll, or a replacement for the auction TD input. If the Q2 probe fails at
`AUCTION_0926`, the TD-owned auction fact still runs and the failure is
recorded. `OPENING_0932` continues to fail closed when Q2 cannot be observed.

## Local verification

```text
commit: a8d9a31
local pytest: 424 passed in 1.53s
local compileall: PASS
local git diff --check: PASS
```

## Cobra-ion exact verification

The exact `git archive` was copied to:

```text
/home/exedev/validation/engine-core-a8d9a31.tar
/home/exedev/validation/engine-core-a8d9a31
```

```text
archive SHA-256: bb0bcdccd829a5fc97b8159770592fed3aa1d23ea31df5ef323ffaedb51bc272
Python:          3.12.3
remote pytest:   424 passed in 2.13s
remote compileall: PASS
engine-next:     active, NRestarts=0, MainPID=379553
t1-v2-live:      active, NRestarts=0, MainPID=2878024
```

The remote checkout has no Git metadata by design; archive identity was
verified locally and remotely before running the suite.

## Safety and residual scope

```text
new Rabbit consumer: 0
Rabbit ACK/publish change: 0
Redis write: 0
TD write: 0
notification/effect: 0
production restart: 0
```

This closes node-boundary readiness evidence only. It does not prove Rabbit
batch membership, t1-v2 source-freeze ownership, historical availability of
reference data, or Core replacement of `engine-next`.
