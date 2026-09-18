# Cobra-ion exact code verification — 2026-09-18 19:35 CST

## Scope

The remote validation archive `/home/exedev/validation/engine-core-6b4f726-v1`
was run with the existing engine-next Python 3.12.3 shared virtual environment.
The archive is the exact functional Core code baseline; local commits after
`6b4f726` contain documentation/evidence/runbook changes only.

## Commands

```text
python -m pytest -q -p no:cacheprovider
python -m compileall -q src tests
```

## Result

```text
pytest: 525 passed in 1.78s
compileall: PASS
```

This confirms the functional Core baseline under the formal Linux Python
runtime. It does not prove fresh production data, TD write health, or
engine-next replacement acceptance; those remain governed by the separate
real-source evidence and storage gates.
