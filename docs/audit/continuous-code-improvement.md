# Continuous code improvement audit

## 2026-09-08 03:00 - Preserve Q2 rolling metric contracts

- Scope: `src/engine_core/q2.py`, Q2 contract tests, project knowledge and parity evidence.
- Why: production and legacy opening consumers use `spd1m/amt2m/amt5m/vec3m/vec5m`, but three fields were absent from the canonical model and all five were absent from the formal field contract.
- Change: added explicit basis-point/yuan contracts and optional canonical mappings without adding thresholds or fallback semantics.
- Verification: focused Q2/time tests 20 passed; full suite 114 passed; compileall, diff-check and UTF-8/mojibake scan passed locally; the identical files passed 114 tests and compileall in the cobra-ion Python 3.12 temporary directory.
- Residual risk: the upstream source timestamp definition remains UNKNOWN and is unrelated to these producer-computed rolling values.
- Next candidate: audit minute-wheel parity for same-minute cumulative amount handling before adding any more metrics.

## 2026-09-08 03:15 - Separate minute current amount from rolling reference

- Scope: `src/engine_core/time_windows.py`, minute-window tests and legacy parity evidence.
- Why: the minute bucket used one amount for both the latest observation and future lookback, while production keeps the maximum intra-minute cumulative amount as the later reference.
- Change: retained latest source-time price/current amount and separately stored the maximum intra-minute amount used by subsequent rolling deltas; older source-time updates remain unable to overwrite newer state.
- Verification: focused time/Q2 tests 22 passed; full suite 116 passed; compileall, diff-check and UTF-8/mojibake scan passed locally; byte-identical files passed 116 tests and compileall in the cobra-ion Python 3.12 temporary directory.
- Residual risk: same-timestamp rows have no source sequence. Conflicts at the retained latest timestamp fail closed; older same-millisecond cohorts remain without a claimed causal order because this bounded wheel is not an arrival journal.
- Next candidate: audit whether the new wheel should calculate five-minute/vector fields or only preserve producer-computed Q2 values.

## 2026-09-08 04:10 - Separate trading-day authority from session phase

- Scope: new pure `SessionPlanV1` wheel, boundary tests and wheel-local legacy parity evidence.
- Why: legacy `infer_run_phase()` classifies weekends as live market phases, mixes one right-closed 15:00 boundary into otherwise half-open windows, and exposes a contradictory one-second NIGHT phase.
- Change: required the frozen trading calendar to authorize the explicit trade date, then classified aware instants through contiguous half-open Asia/Shanghai intervals; kept producer snapshot gates outside the phase wheel.
- Verification: focused clock/calendar/window/session tests 33 passed; full suite 124 passed; compileall, diff-check and UTF-8 scan passed locally; byte-identical implementation and fixtures passed 124 tests and compileall in the cobra-ion Python 3.12 temporary directory.
- Residual risk: this wheel deliberately does not decide 09:20/09:24/09:25 source finalization or schedule timers; those remain separate evidence-backed contracts.
- Next candidate: audit the remaining local-time conversion helpers and auction anchor specifications before adding any scheduler integration.
