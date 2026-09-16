# Cobra-ion exact verification — `528f5d0`

The verification baseline for this step was archived and extracted into a new
remote validation directory. The archive hash was checked locally and on
cobra-ion before running the tests.

```text
commit:          528f5d0ed9a5c1fac4327427be5e6173238aa2a9
archive SHA-256: f071934879ea4245b72fdaf45cea8945da6689ad5fa6e09ff76b10677c96851d
remote archive:  /home/exedev/validation/engine-core-528f5d0.tar
remote checkout: /home/exedev/validation/engine-core-528f5d0
```

```text
local pytest:      424 passed in 1.49s
local compileall:  PASS
local diff-check:  PASS
remote Python:     3.12.3
remote pytest:     424 passed in 2.04s
remote compileall: PASS
```

The exact archive includes the node-boundary Q2 readiness shadow changes and
the updated self-owned 2026-09-17 morning schedule pointer. Production
services remain outside the checkout and were not restarted or modified:

```text
engine-next: active, MainPID=379553, NRestarts=0
t1-v2-live:  active, MainPID=2878024, NRestarts=0
```

No Rabbit consumer or ACK change, Redis/TD write, notification/effect, or
production restart occurred.
