# M3 09:20 NORMAL capture-window guard — 2026-09-18

## Change

`examples/run_m3_0920_shadow.py` now fails closed when a caller supplies
`origin=NORMAL` after the bounded 09:15–09:21 capture window. It returns a
`BLOCKED` evidence object without reading Q2 and without constructing or
dispatching an Engine. `RECOVERY_CATCHUP` remains the only allowed late-start
mode.

This prevents a post-market rerun from being mislabeled as a normal 09:20
observation. It does not change production services or timer ownership.

## Verification

Local:

```text
tests/test_m3_0920_shadow.py + tests/test_live_morning_shadow.py: 21 passed
full pytest: 526 passed
compileall: PASS
```

Cobra-ion isolated validation copy:

```text
path: /home/exedev/validation/engine-core-m3-window-20260918
Python: /home/exedev/services/engine-next/shared/venv/bin/python (3.12)
target tests: 21 passed
full pytest: 526 passed
compileall: PASS
```

The validation copy was created from the existing Core archive and only the
changed runner/test files were overlaid. No production service, Redis/TD
data, Rabbit consumer/ACK, or effect was changed.

## Decision

```text
NORMAL_POSTMARKET_MISLABEL_GUARD  PASS
REMOTE_EXACT_TESTS                PASS
PRODUCTION_OWNER                  UNCHANGED
```
