# SessionPlan -> Engine integration evidence

Scope: phase selection for Engine market updates and trigger-time snapshots.
This change does not make `SessionPlan` a scheduler and does not connect
`EvaluationPlan` to execution.

## Contract

- An Engine uses exactly one phase authority:
  - `session_plan` derives phase from each signal's `logical_time_ms`; or
  - the compatibility `phase` string remains static when no plan is supplied.
- Supplying both a plan and a non-`UNKNOWN` static phase fails closed.
- A market update stores the phase corresponding to that observation time.
- TIMER/PULSE/RECOVERY snapshots use the phase corresponding to their own
  trigger time. This does not rewrite the phase attached to the latest market
  observation.
- A signal whose local date differs from the plan trade date fails closed.
- Trigger phase validation happens before window closure, so a rejected
  cross-date signal cannot partially mutate Engine-owned window state.
- Evaluation identity binds the frozen snapshot phase, not a constructor
  default that may be stale.

## Legacy comparison

| capability | legacy behavior | new behavior | status |
| --- | --- | --- | --- |
| Phase refresh | `engine_next` calls `infer_run_phase(now)` during runtime loops | Engine asks the immutable SessionPlan at signal consumption | MATCH at supported boundaries |
| Trading-day/date validation | `infer_run_phase` can classify a weekend or another date as live | SessionPlan rejects a signal outside its authorized trade date | INTENTIONAL_CHANGE |
| 09:30 transition | Runtime wall time can move from auction to intraday | Signal logical time moves snapshots from AUCTION to INTRADAY | MATCH |
| Static phase tests/fixtures | Callers inject a phase label | Kept only when no SessionPlan is configured | MATCH |

The legacy wall-clock helper is evidence for phase boundaries, not a runtime
dependency or new-code template. Source finalization gates such as 09:24:10
and 09:25:06 remain outside this contract.

## Verification

- plan and explicit phase conflict is rejected;
- a 09:24 market update is recorded as AUCTION;
- a 09:30 timer freezes an INTRADAY snapshot without rewriting the earlier
  market-observation phase;
- a cross-date signal is rejected;
- the legacy/static phase path remains supported;
- full local and cobra-ion Python 3.12 suites are required before commit.
