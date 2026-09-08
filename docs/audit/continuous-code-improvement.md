# Continuous code improvement audit

## 2026-09-08 03:00 - Preserve Q2 rolling metric contracts

- Scope: `src/engine_core/q2.py`, Q2 contract tests, project knowledge and parity evidence.
- Why: production and legacy opening consumers use `spd1m/amt2m/amt5m/vec3m/vec5m`, but three fields were absent from the canonical model and all five were absent from the formal field contract.
- Change: added explicit basis-point/yuan contracts and optional canonical mappings without adding thresholds or fallback semantics.
- Verification: focused Q2/time tests 20 passed; full suite 114 passed; compileall, diff-check and UTF-8/mojibake scan passed locally; the identical files passed 114 tests and compileall in the cobra-ion Python 3.12 temporary directory.
- Residual risk: the upstream source timestamp definition remains UNKNOWN and is unrelated to these producer-computed rolling values.
- Next candidate: audit minute-wheel parity for same-minute cumulative amount handling before adding any more metrics.
