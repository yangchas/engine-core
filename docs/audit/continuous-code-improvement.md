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

## 2026-09-08 04:35 - Unify strict local clock conversion

- Scope: `clock.py`, Window compatibility helper, Session intervals and clock tests.
- Why: Window and Session parsed `HH:MM:SS` independently, creating two authorities for strict formatting, day-end handling and timezone conversion.
- Change: centralized strict clock parsing and Asia/Shanghai local datetime conversion in pure clock functions; Window and Session now delegate to the same implementation.
- Verification: focused clock/window/session tests 29 passed; full suite 126 passed; compileall, diff-check and UTF-8 scan passed locally; byte-identical implementation passed 126 tests and compileall in the cobra-ion Python 3.12 temporary directory.
- Residual risk: source timestamps still carry upstream semantics documented separately; these functions only convert explicit civil-time anchors.
- Next candidate: audit and implement the minimal once-only catch-up timer wheel without importing t1-v2 source freeze gates.

## 2026-09-08 05:00 - Add pure once-only catch-up timer calculations

- Scope: `SessionTimerV1`, timer boundary tests and wheel-local legacy parity evidence.
- Why: legacy scheduled events only match the current minute and remember the immediately previous token, so missed nodes and restart duplicates have no general deterministic contract.
- Change: separated scheduled/fired time, accepted an explicit fired-id set, made the lower frontier inclusive for crash safety and required callers to label recovery catch-up; kept C++ source-publication gates outside the Engine timer model.
- Verification: focused timer/session/clock tests 18 passed; full suite 132 passed; compileall, diff-check and UTF-8 scan passed locally; byte-identical implementation passed 132 tests and compileall in the cobra-ion Python 3.12 temporary directory.
- Residual risk: without a future checkpoint the caller cannot durably preserve fired identities; this pure wheel intentionally makes no persistence claim.
- Next candidate: audit whether the first EvaluationPlan can now be expressed as plain data over verified Session/Timer/Data/Fact wheels without adding workflow behavior.

## 2026-09-08 05:25 - Freeze EvaluationPlan as plain data

- Scope: immutable `EvaluationPlanV1`, contract tests and boundary evidence.
- Why: trigger payloads currently carry ad-hoc data requirement names while the frozen architecture requires one explicit place to state what a node evaluates.
- Change: added exact trigger lookup plus ordered Data/Fact/Strategy identities and versioned semantic hashes; explicitly omitted execution, conditions, DAGs, retries, fallback and callable registration.
- Verification: focused Evaluation/Timer/Session tests 19 passed; full suite 137 passed; compileall, diff-check and UTF-8 scan passed locally; byte-identical implementation passed 137 tests and compileall in the cobra-ion Python 3.12 temporary directory.
- Residual risk: Engine does not consume EvaluationPlan yet; integration should happen only after a real node can preserve existing Wheel/Engine parity.
- Next candidate: audit minimal Engine composition for one plan node without adding a generic fact/strategy plugin platform.

## 2026-09-08 06:10 - Bind Engine phase to SessionPlan logical time

- Scope: optional `SessionPlanV1` phase authority in `DeterministicEngine`, trigger-time snapshot phase override, focused boundary tests and legacy comparison evidence.
- Why: Engine previously stored one constructor phase forever, so a 09:30 Timer following the last 09:24 market update could incorrectly freeze another auction-phase snapshot.
- Change: made SessionPlan and an explicit static phase mutually exclusive; derived market-observation and trigger-snapshot phases from each signal logical time; validated trigger phase before any window closure; bound evaluation identity to the frozen snapshot phase; preserved the static phase compatibility path without connecting EvaluationPlan or adding scheduler behavior.
- Verification: focused Engine/Session tests 22 passed; full local suite 142 passed; compileall and diff-check passed; byte-identical files passed the same 142-test suite and compileall in the cobra-ion Python 3.12 temporary verification directory.
- Residual risk: CurrentMarketState retains the phase of its latest market observation until another market update; this is intentional, while trigger-time phase belongs to EngineSnapshot. SessionPlan still does not decide source publication/finalization gates.
- Next candidate: audit the smallest possible EvaluationPlan-to-Engine boundary using one existing trigger, without adding registries, dynamic dispatch, retry, fallback or a generic plugin platform.
