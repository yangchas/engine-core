# Exact cobra-ion verification: `ba309b8`

## Identity

| Item | Value |
| --- | --- |
| Commit | `ba309b8` (`test(shadow): cover explicit build identity`) |
| Archive | `engine-core-ba309b8.tar` |
| Archive SHA-256 | `af4ed8ce1ee0066ea2665c94353a59402334d7882c74d17f14152394a6d15cd8` |
| Remote path | `/home/exedev/validation/engine-core-ba309b8` |
| Python | `3.12.3` (production shared venv) |
| Source-tree build identity | `f8c88da7d633d27b57fd2249496d17f4d734c3cc6b31dfcb749c5ed6cc59db05` |

## Checks

The exact archive was extracted into a new validation directory on cobra-ion
and ran:

```text
python -m pytest -q -p no:cacheprovider  -> 421 passed in 2.11s
python -m compileall -q src tests examples -> PASS
```

Local Windows ran the same test selection and compile check with the same
source-tree build identity (`421 passed in 1.50s`). The local and remote
archive SHA-256 values match.

Production services were not changed:

```text
engine-next.service  active, MainPID=379553, NRestarts=0
t1-v2-live.service   active, MainPID=2878024, NRestarts=0
```

No Rabbit consumer or ACK change, Redis/TD write, notification, effect, or
restart was performed.

