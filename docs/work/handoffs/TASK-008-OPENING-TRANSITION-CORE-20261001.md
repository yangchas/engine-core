# TASK-008 handoff: 09:25 anchor → opening transition facts

Date: 2026-10-01 (Asia/Shanghai)

## Scope

Add a descriptive Core summary for the transition from the frozen 09:25 auction
anchor to the 09:32 Q2 opening observation. This closes a feature-specific
reporting seam; it does not claim TASK-008 is complete, change a production
consumer, add a strategy decision, or relax the separate M3-1 gate.

The summary uses the existing `OpeningTransitionFactV1` per-symbol calculation
and a new `OpeningTransitionSummaryV1` wrapper. It compares the frozen Core
09:25 anchor price against each opening Q2 row's previous close, then compares
that auction change with the opening Q2 price change. Unavailable anchors or
required prices remain unavailable; no zero fill is introduced. Scope is the
Q2Frame input cohort, not independently proven full-market coverage.

At the 09:32 opening barrier, the runner now:

- reads the already captured/frozen 09:25 Core anchor facts;
- derives opening rows from the latest event-time Q2 updates at the cutoff;
- records baseline and opening evaluation/source metadata;
- retains the existing stale-observation count and records Rabbit arrival and
  historical `available_at` as unknown;
- marks the output `FACT_ONLY` and does not submit it as a strategy signal.

## Real-data audit

Audit result: `PASS_WITH_LIMITS`.

Inputs were frozen existing replay evidence, not generated test rows and not a
new live-source query:

- Q2Frame: `/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl`
  - SHA-256: `5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9`
- Existing integrated Core report:
  `/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T1142+0800/integrated_core_q2frame_report.json`
  - SHA-256: `fe1b04b0956dff3593ec6b157c60a68bd23004c1bd9a14e853940f8432bb921b`
- Read-only legacy helper source:
  `/home/exedev/services/engine-next/releases/20260903_e272842/engine_next/runtime/open_confirmation.py`
  - SHA-256: `73453f5caaf27636c9ad6aa57b2e609da0ad1867ebc1ff06906eca46921405b6`

The audit streamed the pinned Q2Frame through the 09:32:10 cutoff and checked
the new pure Core transition output against both the prior Core opening-change
report and the legacy pure transition helpers:

- 5,223 observed Q2Frame symbols;
- 5,070 frozen anchor prices and comparable transitions;
- 153 transitions unavailable because the frozen anchor was unavailable;
- opening-change parity against the existing Core report: 5,223 equal, 0 mismatch;
- transition facts against the legacy helper formulas: 5,223 equal, 0 mismatch;
- 12 stale opening observations retained and identified, without changing the
  existing freshness policy.

The detailed machine-readable evidence is in
`/home/exedev/validation/task008-opening-transition-20261001T201840+0800/`:

- `opening_transition_summary.json`
- `audit_summary.json`
- `sha256sums.txt`

Summary hashes:

- facts-by-symbol: `b1d6443d7dfe987dbf84de55121d38d0e2585bcb03fae5e3517207cdc53bda0b`
- content: `10dff1148b319b119e9852cda27500b700fde0acb0d8fe1c09dd6efdce200eb0`

## Limits and interpretation

- This is event-time replay, not original Rabbit arrival/delivery order.
- Historical `available_at` is unknown; no historical cutoff-equivalence claim
  is made.
- The cohort is the Q2Frame input cohort; full-market coverage is unproven.
- The auction percentage is derived from the frozen Core 09:25 anchor and Q2
  previous-close field. It is not asserted equal to TD `chg_bp` or to the
  legacy frozen auction report; the source-specific comparison showed those
  are not interchangeable for every symbol.
- Stale observations and unavailable anchor facts are carried in the result;
  they do not stop the replay or suppress other symbols.
- The existing 09:25/09:32 artifacts are sufficient for this bounded feature
  audit; the expensive full Q2Frame engine replay was not repeated.
- No Redis, TDengine, RabbitMQ, service, or production write path was touched
  by this audit. Side effects: `NONE`.

## Verification

- Targeted tests: `40 passed`.
- Full suite: `793 passed`; 3 upstream protobuf deprecation warnings.
- `compileall`: passed.
- `git diff --check`: passed.

## State and next alignment

- TASK-008 remains `PARTIAL_EVIDENCE`; this handoff does not promote it to
  complete or merged.
- `M3_1_NORMAL=BLOCKED` and `TD_WRITE_HEALTH=UNPROVEN` remain separate and
  unchanged.
- No next task was auto-promoted. Before another feature slice, compare this
  result with the user's original replay objective and the current task plan.
